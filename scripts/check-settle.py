#!/usr/bin/env python3
"""结算核对流水线：示例数据 → 金额试算 → 前后端启动可用性，一条命令跑完并给结论。

每一步都是独立阶段，失败时明确报「卡在哪一步」并保留日志，修好后直接重跑即可；
成功时不留任何中间数据与进程（自己起的服务自己关，运行目录清掉）。

阶段：
  1. 前置检查      python/node/npm、后端依赖、前端依赖、示例数据可读
  2. 清场          杀掉可能占用隔离端口的残留进程，清空运行目录
  3. 起后端        SETTLE_DEVDATA=1，装载 settlement-fixtures/cases.json
  4. 健康检查      /api/health 可用且模块齐全
  5. 数据核对      /api/settle/export 与示例数据逐单一致（含金额）
  6. 试算核对      每单 /api/settle/trial 与 fixture expected 一致；/summary 与 expectedSummary 一致
  7. 起前端        vite dev server（隔离端口），index.html 可取
  8. 端到端核对    经 vite 代理访问后端 + 页面 billing.ts 与后端逐单对拍
  9. 清场退出      关进程、删运行目录；任一步失败则保留现场并打印排查信息

用法：python3 scripts/check-settle.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
FIXTURE_PATH = ROOT / "settlement-fixtures" / "cases.json"
RUNTIME_DIR = ROOT / ".settle-check"

BACKEND_PORT = int(os.environ.get("SETTLE_CHECK_BACKEND_PORT", "8010"))
FRONTEND_PORT = int(os.environ.get("SETTLE_CHECK_FRONTEND_PORT", "5180"))
BACKEND_BASE = f"http://127.0.0.1:{BACKEND_PORT}"
FRONTEND_BASE = f"http://127.0.0.1:{FRONTEND_PORT}"

TOLERANCE = Decimal("0.005")

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


class StepFailed(Exception):
    """某个阶段失败：消息直接展示给使用者。"""


class CheckPipeline:
    def __init__(self) -> None:
        self.processes: list[subprocess.Popen[bytes]] = []
        self.steps: list[tuple[str, bool, str]] = []
        self.backend_env: dict[str, str] = {}

    # ---- 通用工具 -------------------------------------------------------

    def step(self, name: str) -> None:
        print(f"\n{YELLOW}▶ {name}{RESET}")

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append((name, ok, detail))
        mark = f"{GREEN}通过{RESET}" if ok else f"{RED}失败{RESET}"
        print(f"  {mark}  {detail or name}")

    def http(self, url: str, *, payload: dict | None = None, timeout: float = 5.0):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def probe(self, url: str, timeout: float = 3.0) -> int:
        """就绪探针只看 HTTP 状态码，不解析响应体（前端首页是 HTML，不能按 JSON 读）。"""
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read()
            return response.status

    def wait_http(self, url: str, *, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        last_error = "尚未尝试"
        while time.time() < deadline:
            dead = [(i, proc.pid, proc.returncode) for i, proc in enumerate(self.processes) if proc.poll() is not None]
            if dead:
                raise StepFailed(f"服务进程提前退出：{dead}，最后一次探针错误：{last_error}")
            try:
                if self.probe(url) == 200:
                    return
                last_error = "非 200 响应"
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
        raise StepFailed(f"等待 {url} 超时（{timeout:.0f}s）：{last_error}")

    # ---- 阶段 1：前置检查 -----------------------------------------------

    def preflight(self) -> None:
        self.step("前置检查：运行环境与依赖")

        def have(command: str) -> bool:
            return shutil.which(command) is not None

        if not have("python3"):
            raise StepFailed("找不到 python3")
        if not have("node") or not have("npm"):
            raise StepFailed("找不到 node/npm，请先安装 Node.js")

        deps_kind = ""
        if (BACKEND_DIR / ".venv" / "bin" / "python").exists():
            deps_kind = ".venv"
        elif (BACKEND_DIR / ".pydeps" / "fastapi").exists():
            deps_kind = ".pydeps"
        else:
            raise StepFailed(
                "后端依赖未安装：请运行 `make install`（或 cd backend && ./run.sh 让它自动装）"
            )

        if not (FRONTEND_DIR / "node_modules" / "vite").exists():
            raise StepFailed("前端依赖未安装：请在 frontend 目录执行 npm install")
        if not (FRONTEND_DIR / "node_modules" / "@esbuild").exists():
            raise StepFailed("前端 esbuild 平台二进制缺失：请删掉 frontend/node_modules 后重新 npm install")

        if not FIXTURE_PATH.exists():
            raise StepFailed(f"结算示例数据不存在：{FIXTURE_PATH}")
        try:
            fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StepFailed(f"结算示例数据不是合法 JSON（第 {exc.lineno} 行）：{exc.msg}")
        if not fixture.get("cases"):
            raise StepFailed("结算示例数据缺少非空 cases 列表")

        self.record("前置检查", True, f"python3/node/npm 就绪，后端依赖 {deps_kind}，示例 {len(fixture['cases'])} 单")

    # ---- 阶段 2：清场 ----------------------------------------------------

    def port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def kill_port(self, port: int) -> None:
        """尽量清掉占用隔离端口的残留（主要是上次流水线没清干净的自己）。"""
        try:
            output = subprocess.run(
                ["fuser", "-k", f"{port}/tcp"], capture_output=True
            )
            _ = output
        except FileNotFoundError:
            # 没有 fuser 的环境靠 start_new_session 的进程组兜底；这里只能提示
            pass

    def cleanup_start(self) -> None:
        self.step("清场：清理上一次的进程与中间数据")
        for port in (BACKEND_PORT, FRONTEND_PORT):
            if self.port_in_use(port):
                self.kill_port(port)
                for _ in range(20):
                    if not self.port_in_use(port):
                        break
                    time.sleep(0.25)
                if self.port_in_use(port):
                    raise StepFailed(
                        f"端口 {port} 被其他进程占用且无法清理，请先释放该端口"
                        f"（可用 SETTLE_CHECK_BACKEND_PORT / SETTLE_CHECK_FRONTEND_PORT 换端口）"
                    )
        if RUNTIME_DIR.exists():
            shutil.rmtree(RUNTIME_DIR)
        RUNTIME_DIR.mkdir(parents=True)
        self.record("清场", True, f"端口 {BACKEND_PORT}/{FRONTEND_PORT} 空闲，运行目录 {RUNTIME_DIR.name}/ 已重建")

    # ---- 阶段 3-4：起后端 + 健康检查 ------------------------------------

    def start_backend(self) -> None:
        self.step("启动后端（装载统一结算示例数据）")
        env = os.environ.copy()
        env["SETTLE_DEVDATA"] = "1"
        env["PORT"] = str(BACKEND_PORT)
        log_path = RUNTIME_DIR / "backend.log"
        log_file = open(log_path, "wb")

        if (BACKEND_DIR / ".venv" / "bin" / "python").exists():
            cmd = [".venv/bin/python", "-m", "uvicorn", "app.main:app",
                   "--host", "127.0.0.1", "--port", str(BACKEND_PORT)]
        else:
            cmd = ["python3", "-m", "uvicorn", "app.main:app",
                   "--host", "127.0.0.1", "--port", str(BACKEND_PORT)]
            pydeps = str(BACKEND_DIR / ".pydeps")
            env["PYTHONPATH"] = pydeps + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

        proc = subprocess.Popen(
            cmd, cwd=BACKEND_DIR, env=env, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(proc)

        try:
            self.wait_http(f"{BACKEND_BASE}/api/health", timeout=30)
        except StepFailed:
            raise StepFailed(f"后端未就绪，日志见 {log_path}")

        status, payload = self.http(f"{BACKEND_BASE}/api/health")
        if not payload.get("ok"):
            raise StepFailed(f"健康检查返回非 ok：{payload}")
        self.record("启动后端", True, f"健康检查通过，业务模块 {payload.get('modules')} 个，PID {proc.pid}")

    # ---- 阶段 5：示例数据核对 -------------------------------------------

    def check_fixture(self) -> None:
        self.step("数据核对：接口数据必须与示例数据逐单一致")
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        _, exported = self.http(f"{BACKEND_BASE}/api/settle/export?size=200")
        rows = exported.get("items", [])
        cases = fixture["cases"]
        if len(rows) != len(cases):
            raise StepFailed(f"接口返回 {len(rows)} 单，示例数据 {len(cases)} 单，条数对不上")

        rows_by_no = {row["结算单号"]: row for row in rows}
        for case in cases:
            row = rows_by_no.get(case["结算单号"])
            if row is None:
                raise StepFailed(f"接口缺少结算单 {case['结算单号']}（疑似上一次的中间数据没清干净）")
            for key in ("结算对象", "结算周期", "开票状态"):
                if str(row.get(key)) != str(case.get(key)):
                    raise StepFailed(
                        f"{case['结算单号']} 字段「{key}」不一致：接口={row.get(key)!r}，示例={case.get(key)!r}"
                    )
            if row.get("status") != case.get("status"):
                raise StepFailed(
                    f"{case['结算单号']} 状态不一致：接口={row.get('status')!r}，示例={case.get('status')!r}"
                )
            expected_amount = Decimal(str(case["expected"]["应收金额"]))
            actual_amount = Decimal(str(row.get("应收金额")))
            if abs(actual_amount - expected_amount) > TOLERANCE:
                raise StepFailed(
                    f"{case['结算单号']} 应收金额漂移：接口 {actual_amount}，示例期望 {expected_amount}"
                )
            expected_received = Decimal(str(case.get("已收金额", 0)))
            actual_received = Decimal(str(row.get("已收金额", 0)))
            if abs(actual_received - expected_received) > TOLERANCE:
                raise StepFailed(
                    f"{case['结算单号']} 已收金额不一致：接口 {actual_received}，示例 {expected_received}"
                )
        self.record("数据核对", True, f"{len(rows)} 单的字段、状态、金额与 settlement-fixtures/cases.json 完全一致")

    # ---- 阶段 6：金额试算核对 -------------------------------------------

    def check_trial(self) -> None:
        self.step("试算核对：固定入口 /api/settle/trial 与汇总口径")
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for case in fixture["cases"]:
            status, payload = self.http(
                f"{BACKEND_BASE}/api/settle/trial", payload={"values": case}
            )
            if not payload.get("ok"):
                raise StepFailed(f"{case['结算单号']} 试算被拒：{payload.get('message')}")
            entry = payload["entry"]
            for key in ("装卸费", "港口包干费", "应收金额"):
                expected = Decimal(str(case["expected"][key]))
                actual = Decimal(str(entry[key]))
                if abs(actual - expected) > TOLERANCE:
                    raise StepFailed(
                        f"{case['结算单号']} {key} 口径漂移：试算 {actual}，期望 {expected}"
                    )

        _, summary = self.http(f"{BACKEND_BASE}/api/settle/summary")
        for key, expected_value in fixture["expectedSummary"].items():
            actual = Decimal(str(summary.get(key)))
            expected = Decimal(str(expected_value))
            if abs(actual - expected) > TOLERANCE:
                raise StepFailed(f"汇总「{key}」口径漂移：接口 {actual}，期望 {expected}")

        # 非法入参要被拦下，保证试算入口对脏数据有明确结论而不是静默算错
        _, rejected = self.http(
            f"{BACKEND_BASE}/api/settle/trial",
            payload={"values": {"作业量": -10, "装卸单价": 45}},
        )
        if rejected.get("ok"):
            raise StepFailed("作业量为负数时试算竟然通过，入参校验缺失")
        self.record("试算核对", True, "5 单试算与汇总全部命中期望值，非法入参被正确拦截")

    # ---- 阶段 7-8：前端可用性 + 端到端 ----------------------------------

    def start_frontend(self) -> None:
        self.step("启动前端 dev server")
        env = os.environ.copy()
        env["VITE_PROXY_TARGET"] = BACKEND_BASE
        log_path = RUNTIME_DIR / "frontend.log"
        log_file = open(log_path, "wb")
        proc = subprocess.Popen(
            [str(FRONTEND_DIR / "node_modules" / ".bin" / "vite"),
             "--host", "127.0.0.1",
             "--port", str(FRONTEND_PORT),
             "--strictPort"],
            cwd=FRONTEND_DIR, env=env, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes.append(proc)
        try:
            self.wait_http(f"{FRONTEND_BASE}/", timeout=45)
        except StepFailed as exc:
            raise StepFailed(f"前端 dev server 未就绪（{exc}），日志见 {log_path}")
        self.record("启动前端", True, f"vite 就绪（{FRONTEND_BASE}），PID {proc.pid}")

    def check_end_to_end(self) -> None:
        self.step("可用性核对：前端可取页、代理通后端、页面与接口金额一致")
        # 1) 页面资源可取
        with urllib.request.urlopen(f"{FRONTEND_BASE}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        if "<div id=\"app\">" not in html:
            raise StepFailed("前端首页 HTML 结构异常，结算页面可能无法挂载")

        # 2) 经 vite 代理访问后端（与页面运行时完全同一条链路）
        status, proxied = self.http(f"{FRONTEND_BASE}/api/settle/summary")
        if "应收金额" not in proxied:
            raise StepFailed("经前端代理访问 /api/settle/summary 失败，代理配置可能有误")

        # 3) 页面同一份 billing.ts 与后端逐单对拍
        page_check = ROOT / "scripts" / "page-check.mjs"
        result = subprocess.run(
            ["node", str(page_check),
             str(FRONTEND_DIR / "src" / "views" / "settle" / "billing.ts"),
             BACKEND_BASE, str(FIXTURE_PATH)],
            capture_output=True, text=True, cwd=FRONTEND_DIR,
        )
        if result.returncode != 0:
            detail = "页面与接口金额口径不一致：\n" + result.stdout.strip()
            if result.stderr.strip():
                detail += "\n" + result.stderr.strip()
            raise StepFailed(detail)
        payload = json.loads(result.stdout)
        self.record(
            "端到端核对",
            True,
            f"页面可访问、/api 代理正常，{payload['checkedCases']} 单试算 + 汇总前后端零差异",
        )

    # ---- 阶段 9：清场退出 ------------------------------------------------

    def teardown(self, failed: bool) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
        deadline = time.time() + 8
        for proc in self.processes:
            remain = max(deadline - time.time(), 0.1)
            try:
                proc.wait(timeout=remain)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()

        for port in (BACKEND_PORT, FRONTEND_PORT):
            for _ in range(20):
                if not self.port_in_use(port):
                    break
                time.sleep(0.2)

        if not failed:
            # 成功才删：失败时保留日志现场，修好后重跑会在清场阶段自动清掉
            if RUNTIME_DIR.exists():
                shutil.rmtree(RUNTIME_DIR)

    def run(self) -> int:
        failed_step = ""
        try:
            self.preflight()
            self.cleanup_start()
            self.start_backend()
            self.check_fixture()
            self.check_trial()
            self.start_frontend()
            self.check_end_to_end()
        except StepFailed as exc:
            failed_step = str(exc)
            print(f"\n{RED}✗ 卡在这一步：{failed_step}{RESET}")
        except KeyboardInterrupt:
            failed_step = "人工中断"
            print(f"\n{YELLOW}已中断，开始清场{RESET}")
        finally:
            self.teardown(failed=bool(failed_step))

        print("\n" + "=" * 64)
        for name, ok, detail in self.steps:
            print(f"  [{'✓' if ok else '✗'}] {name} — {detail}")

        if failed_step:
            print(f"\n{RED}结论：不可提交{RESET}")
            print(f"卡点：{failed_step}")
            if RUNTIME_DIR.exists():
                print(f"现场日志保留在：{RUNTIME_DIR}/（backend.log、frontend.log）")
            print("修复后直接重跑：make check-settle（或 python3 scripts/check-settle.py）")
            return 1

        leftover = [p for p in (BACKEND_PORT, FRONTEND_PORT) if self.port_in_use(p)]
        if leftover:
            print(f"\n{RED}结论：不可提交{RESET} — 收尾时端口 {leftover} 仍被占用，存在残留进程")
            return 1

        print(f"\n{GREEN}结论：可以提交{RESET} — 示例数据、金额试算、前后端可用性全部核对通过，现场已清理")
        return 0


if __name__ == "__main__":
    sys.exit(CheckPipeline().run())
