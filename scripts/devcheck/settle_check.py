#!/usr/bin/env python3
"""结算试算本地一体化检查（devcheck）。

把三件事串成一条可重复跑的流程：
  1. 准备结算示例数据（fixtures/settle-cases.json，唯一数据源）
  2. 固定入口的金额试算（POST /api/settle/trial）
  3. 前后端启动后的可用性检查（健康检查、页面与代理、接口/页面通道结果一致性）

用法：
  python3 scripts/devcheck/settle_check.py            # 自己拉起前后端，跑完自动清理
  python3 scripts/devcheck/settle_check.py --keep     # 失败时保留进程与日志便于排查

约定：
  - 只使用标准库，克隆下来即可跑；后端依赖走 backend/.venv（make install 准备）。
  - 服务跑在临时端口上，绝不影响你已经启动的 dev 服务；脚本只清理自己拉起的进程。
  - 数据写在临时服务的内存里，进程退出即消失，不留下上一次的中间数据。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "settle-cases.json"
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python"

STARTUP_TIMEOUT = 45.0
POLL_INTERVAL = 0.5
# 金额只比对到分，接口经过 JSON 浮点传输，允许 1 厘以内误差。
MONEY_EPS = 0.001


class CheckFailed(Exception):
    """某一步检查失败；step 记录卡在哪一步，detail 给出可读原因。"""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"[{step}] {detail}")
        self.step = step
        self.detail = detail


class Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def step(self, title: str) -> None:
        line = f"▶ {title}"
        print(line, flush=True)
        self.lines.append(line)

    def ok(self, message: str) -> None:
        line = f"  ✅ {message}"
        print(line, flush=True)
        self.lines.append(line)

    def info(self, message: str) -> None:
        line = f"  · {message}"
        print(line, flush=True)
        self.lines.append(line)

    def fail(self, message: str) -> None:
        line = f"  ❌ {message}"
        print(line, flush=True)
        self.lines.append(line)


def pick_free_port() -> int:
    """让操作系统给一个空闲端口，避免和已在跑的 8000/5173 冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_request(method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 10.0) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}
    try:
        return 200, json.loads(body)
    except json.JSONDecodeError:
        return 200, {"raw": body}


def wait_until_ready(url: str, what: str, log_tail: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = str(exc)
        time.sleep(POLL_INTERVAL)
    raise CheckFailed("启动服务", f"{what} 在 {STARTUP_TIMEOUT:.0f}s 内未就绪（{url}）：{last_error}\n{log_tail}")


def tail_of(path: Path, limit: int = 15) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "（日志不可读）"
    lines = text.strip().splitlines()
    return "\n".join(lines[-limit:])


def start_process(cmd: list[str], cwd: Path, log_path: Path, env: dict[str, str]) -> subprocess.Popen:
    log = log_path.open("w", encoding="utf-8")
    # 新开进程组，清理时可以整组杀掉，npm/vite 这种父生子的也不会漏。
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=5)


def assert_expected(case_id: str, expected: dict[str, float], actual: dict[str, Any]) -> list[str]:
    diffs: list[str] = []
    for field, want in expected.items():
        got = actual.get(field)
        if not isinstance(got, (int, float)) or abs(float(got) - float(want)) > MONEY_EPS:
            diffs.append(f"{field} 期望 {want}，实际 {got!r}")
    if diffs:
        raise CheckFailed("金额试算", f"用例 {case_id} 口径不一致：" + "；".join(diffs))
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description="结算试算本地一体化检查")
    parser.add_argument("--keep", action="store_true", help="失败时保留服务进程与日志（默认跑完即清理）")
    args = parser.parse_args()

    reporter = Reporter()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reporter.info(f"开始时间 {started_at}，工作目录 {ROOT}")

    backend_proc: subprocess.Popen | None = None
    frontend_proc: subprocess.Popen | None = None
    temp_dir = Path(tempfile.mkdtemp(prefix="settle-devcheck-"))
    backend_log = temp_dir / "backend.log"
    frontend_log = temp_dir / "frontend.log"
    backend_port = pick_free_port()
    frontend_port = pick_free_port()

    def cleanup() -> None:
        for proc in (frontend_proc, backend_proc):
            if proc is not None:
                stop_process(proc)
        if not (args.keep and failed):
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            reporter.info(f"已保留日志：{temp_dir}")

    failed = False
    failed_step = ""
    try:
        # ---- 步骤 0：环境与样例数据预检 ----
        reporter.step("步骤 1/6 环境预检：依赖、样例数据、端口")
        if not VENV_PYTHON.exists():
            raise CheckFailed(
                "环境预检",
                f"后端虚拟环境不存在（{VENV_PYTHON}），先执行 `make install` 或 `cd backend && ./run.sh` 准备依赖",
            )
        probe = subprocess.run(
            [str(VENV_PYTHON), "-c", "import fastapi, uvicorn"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            raise CheckFailed(
                "环境预检",
                f"后端虚拟环境不可用（fastapi/uvicorn 导入失败），重建后重跑：rm -rf backend/.venv && make install\n{probe.stderr.strip()}",
            )
        if not (FRONTEND_DIR / "node_modules").exists():
            raise CheckFailed("环境预检", "前端依赖未安装，先执行 `make install`（或 cd frontend && npm install）")
        if not FIXTURE_PATH.exists():
            raise CheckFailed("环境预检", f"结算样例数据缺失：{FIXTURE_PATH}")
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        trial_cases = fixture.get("trial_cases", [])
        error_cases = fixture.get("error_cases", [])
        entry_cases = fixture.get("entry_cases", [])
        if not trial_cases or not entry_cases:
            raise CheckFailed("环境预检", "样例数据为空：trial_cases / entry_cases 至少各一条")
        reporter.ok(
            f"后端虚拟环境可用；样例数据已加载（试算 {len(trial_cases)} 条、异常 {len(error_cases)} 条、登记 {len(entry_cases)} 条）"
        )
        reporter.info(f"临时端口：后端 {backend_port}，前端 {frontend_port}（不影响在跑的 dev 服务）")

        # ---- 步骤 1：拉起后端 ----
        reporter.step("步骤 2/6 启动后端并做健康检查")
        backend_env = dict(os.environ)
        backend_proc = start_process(
            [str(VENV_PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(backend_port)],
            BACKEND_DIR,
            backend_log,
            backend_env,
        )
        wait_until_ready(
            f"http://127.0.0.1:{backend_port}/api/health",
            "后端",
            tail_of(backend_log),
        )
        status, body = http_request("GET", f"http://127.0.0.1:{backend_port}/api/health")
        if status != 200 or not body.get("ok"):
            raise CheckFailed("后端健康检查", f"/api/health 返回异常：{status} {body}")
        reporter.ok(f"后端就绪，/api/health 返回模块数 {body.get('modules')}")

        # ---- 步骤 2：拉起前端 ----
        reporter.step("步骤 3/6 启动前端并检查页面可用性")
        frontend_env = dict(os.environ)
        frontend_env["VITE_PROXY_TARGET"] = f"http://127.0.0.1:{backend_port}"
        npm_cmd = shutil.which("npm")
        if npm_cmd is None:
            raise CheckFailed("前端启动", "PATH 中找不到 npm")
        frontend_proc = start_process(
            [npm_cmd, "run", "dev", "--", "--port", str(frontend_port), "--strictPort"],
            FRONTEND_DIR,
            frontend_log,
            frontend_env,
        )
        wait_until_ready(
            f"http://127.0.0.1:{frontend_port}/",
            "前端",
            tail_of(frontend_log),
        )
        status, index_body = http_request("GET", f"http://127.0.0.1:{frontend_port}/")
        html = index_body.get("raw", "") if isinstance(index_body, dict) else str(index_body)
        if status != 200 or not re.search(r"<div[^>]*id=[\"']app[\"']", html):
            raise CheckFailed("前端页面检查", f"首页未正常返回（HTTP {status}）\n{tail_of(frontend_log)}")
        reporter.ok("前端 dev server 就绪，首页可访问")

        # 页面模块能被 vite 编译出来（语法/引用错误会在这一步暴露）
        status, view_body = http_request("GET", f"http://127.0.0.1:{frontend_port}/src/views/settle/index.vue")
        view_text = view_body.get("raw", "") if isinstance(view_body, dict) else str(view_body)
        if status != 200 or "/api/settle" not in view_text:
            raise CheckFailed("前端页面检查", f"结算页面模块无法编译或未接入结算接口（HTTP {status}）\n{tail_of(frontend_log)}")
        reporter.ok("结算页面模块编译正常，仍接入 /api/settle（页面本身未改动）")

        # ---- 步骤 3：试算（固定入口，直连后端）----
        reporter.step("步骤 4/6 金额试算：逐用例比对计费口径")
        trial_url_direct = f"http://127.0.0.1:{backend_port}/api/settle/trial"
        for case in trial_cases:
            status, body = http_request("POST", trial_url_direct, {"values": case["input"]})
            if status != 200 or not body.get("ok"):
                raise CheckFailed("金额试算", f"用例 {case['id']}（{case['desc']}）接口报错：{status} {body}")
            assert_expected(case["id"], case["expected"], body["entry"])
            reporter.ok(f"{case['id']}：{case['desc']} → 应收 {body['entry']['应收金额']}")
        for case in error_cases:
            status, body = http_request("POST", trial_url_direct, {"values": case["input"]})
            if body.get("ok"):
                raise CheckFailed("金额试算", f"用例 {case['id']}（{case['desc']}）本应被拦下，接口却返回成功：{body}")
            message = str(body.get("message", ""))
            if case["message_contains"] not in message:
                raise CheckFailed(
                    "金额试算",
                    f"用例 {case['id']} 拦截原因不符：应包含「{case['message_contains']}」，实际「{message}」",
                )
            reporter.ok(f"{case['id']}：{case['desc']}，已被拦下（{message}）")

        # ---- 步骤 4：页面通道（vite 代理）试算必须与直连一致 ----
        reporter.step("步骤 5/6 一致性：页面走 vite 代理复算，结果必须与直连接口相同")
        trial_url_proxy = f"http://127.0.0.1:{frontend_port}/api/settle/trial"
        for case in trial_cases:
            status_direct, direct = http_request("POST", trial_url_direct, {"values": case["input"]})
            status_proxy, proxied = http_request("POST", trial_url_proxy, {"values": case["input"]})
            if status_direct != status_proxy or direct != proxied:
                raise CheckFailed(
                    "接口/页面一致性",
                    f"用例 {case['id']} 直连与页面代理结果不一致：\n  直连 {status_direct} {direct}\n  代理 {status_proxy} {proxied}",
                )
        reporter.ok(f"{len(trial_cases)} 条用例经 vite 代理复算，与直连接口结果逐条一致")

        # ---- 步骤 5：样例数据登记 → 试算金额回读 → 状态流转，验证链路 ----
        reporter.step("步骤 6/6 端到端：样例结算单登记、金额回读与状态流转")
        list_url = f"http://127.0.0.1:{backend_port}/api/settle"
        status, before = http_request("GET", f"{list_url}?page=1&size=200")
        before_total = int(before.get("total", 0))
        created_ids: list[int] = []
        for case in entry_cases:
            values = {k: v for k, v in case.items() if k != "期望应收金额"}
            # 登记方不手填金额：先走唯一试算入口，再把试算结果落单。
            status, body = http_request("POST", trial_url_direct, {"values": values})
            if status != 200 or not body.get("ok"):
                raise CheckFailed("样例数据登记", f"{case['结算单号']} 登记前试算失败：{status} {body}")
            values["应收金额"] = body["entry"]["应收金额"]
            # 夜间加成/月结优惠只是试算入参，不属于结算单落库字段。
            values = {k: v for k, v in values.items() if k not in ("夜间加成", "月结优惠", "作业时间", "单价")}
            status, body = http_request("POST", list_url, {"values": values})
            if status != 200 or not body.get("ok"):
                raise CheckFailed("样例数据登记", f"{case['结算单号']} 登记失败：{status} {body}")
            entry = body["entry"]
            created_ids.append(int(entry["id"]))
            actual_amount = entry.get("应收金额")
            if not isinstance(actual_amount, (int, float)) or abs(float(actual_amount) - float(case["期望应收金额"])) > MONEY_EPS:
                raise CheckFailed(
                    "金额回读",
                    f"{case['结算单号']} 落库金额 {actual_amount!r} 与试算期望 {case['期望应收金额']} 不一致",
                )
            # 登记页展示口径：作业类型/作业量也要在回读结果里
            for field in ("结算单号", "结算对象", "作业类型", "作业量"):
                if entry.get(field) != values.get(field):
                    raise CheckFailed("金额回读", f"{case['结算单号']} 字段 {field} 回读不一致：{entry.get(field)!r}")
            reporter.ok(f"{case['结算单号']} 已登记，应收金额回读 {actual_amount}")

        status, after = http_request("GET", f"{list_url}?page=1&size=200")
        if int(after.get("total", -1)) != before_total + len(entry_cases):
            raise CheckFailed(
                "样例数据登记",
                f"登记后总数应为 {before_total + len(entry_cases)}，实际 {after.get('total')}",
            )

        # 状态流转走一遍：发起核对
        entry_id = created_ids[0]
        status, body = http_request("POST", f"{list_url}/{entry_id}/actions", {"values": {"action": "发起核对"}})
        if status != 200 or not body.get("ok") or body["entry"].get("status") != "核对中":
            raise CheckFailed("状态流转", f"{entry_id} 发起核对未生效：{status} {body}")
        reporter.ok(f"{entry_cases[0]['结算单号']} 发起核对 → 核对中，链路通畅")

    except CheckFailed as exc:
        failed = True
        failed_step = exc.step
        reporter.fail(f"卡在「{exc.step}」：{exc.detail}")
        if backend_proc is not None and backend_log.exists():
            reporter.fail(f"后端日志末尾：\n{tail_of(backend_log)}")
        if frontend_proc is not None and frontend_log.exists():
            reporter.fail(f"前端日志末尾：\n{tail_of(frontend_log)}")
    except FileNotFoundError as exc:
        failed = True
        failed_step = "环境预检"
        reporter.fail(f"缺少可执行文件：{exc}")
    except json.JSONDecodeError as exc:
        failed = True
        failed_step = "环境预检"
        reporter.fail(f"样例数据不是合法 JSON（{FIXTURE_PATH}）：{exc}")
    finally:
        cleanup()

    print()
    if failed:
        print(f"❌ 结论：不可提交。卡在「{failed_step}」，按上面的提示修复后直接重跑本命令即可；本次没有留下进程或中间数据。")
        return 1
    print("✅ 结论：可以提交。样例数据、金额试算与前后端可用性全部通过，临时服务与数据已清理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
