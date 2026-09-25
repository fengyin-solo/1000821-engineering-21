"""结算示例数据装载：把 settlement-fixtures/cases.json 装进内存仓库。

本地起服务和 check-settle 流水线都从这里取数，保证页面、接口、试算看到的是同一份数据；
不新增文件、不写库，每次启动都是干净的一份，不存在上一次的中间数据。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.billing import RECEIVED_KEY, calculate

# 仓库根目录 / settlement-fixtures / cases.json
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "settlement-fixtures" / "cases.json"

REQUIRED_KEYS = ["id", "结算单号", "结算对象", "结算周期", "作业量", "装卸单价", "港口费率"]


def load_cases(path: Path | str | None = None) -> list[dict[str, Any]]:
    """读取示例用例并做结构校验；文件坏了或缺字段直接失败，不静默吞掉。"""
    fixture_path = Path(path) if path else FIXTURE_PATH
    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"结算示例数据文件不存在：{fixture_path}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"结算示例数据不是合法 JSON（{fixture_path} 第 {exc.lineno} 行）：{exc.msg}") from None

    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("结算示例数据缺少非空 cases 列表")

    loaded: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise RuntimeError(f"第 {index} 条用例不是对象")
        missing = [key for key in REQUIRED_KEYS if case.get(key) in (None, "")]
        if missing:
            raise RuntimeError(f"用例 {case.get('结算单号', index)} 缺少字段：{'、'.join(missing)}")
        loaded.append(case)
    return loaded


def to_store_row(case: dict[str, Any]) -> dict[str, Any]:
    """把示例用例转成结算表行：金额由统一口径试算，文件里不允许手写应收金额。

    这样改计费口径时，示例数据不用跟着改，期望值漂移会被流水线抓出来。
    """
    trial = calculate(case)
    return {
        "id": int(case["id"]),
        "status": case.get("status", "待核对"),
        "pending": case.get("status") != "有争议",
        "abnormal": case.get("status") == "有争议",
        "结算单号": case["结算单号"],
        "结算对象": case["结算对象"],
        "结算周期": case["结算周期"],
        "作业量": case["作业量"],
        "应收金额": trial["应收金额"],
        "已收金额": float(case.get(RECEIVED_KEY, 0) or 0),
        "开票状态": case.get("开票状态", "未开票"),
        "结算状态": case.get("status", "待核对"),
    }


def build_settle_rows(path: Path | str | None = None) -> list[dict[str, Any]]:
    """读取 + 转换一步到位，供启动时装载与流水线核对复用。"""
    return [to_store_row(case) for case in load_cases(path)]
