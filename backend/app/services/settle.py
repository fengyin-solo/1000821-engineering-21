"""作业结算业务规则：状态流转、字段校验、金额试算与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services import billing
from app.store import store

MODULE = "settle"
REQUIRED_FIELDS = ["结算单号", "结算对象", "结算周期"]
TRIAL_FIELDS = ["作业量", "装卸单价", "港口费率", "减免"]
STATUS_ORDER = ["待核对", "核对中", "已确认", "已收款", "有争议"]
ACTION_RULES = {"发起核对": "核对中", "确认结算": "已收款", "标记争议": "有争议"}
NEGATIVE_ACTIONS = []


class SettleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("结算单号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def trial(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """金额试算固定入口：不查库、不落数据，只按统一口径算一单。"""
        missing = [field for field in TRIAL_FIELDS[:1] if not str(values.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        try:
            result = billing.calculate(values)
        except ValueError as exc:
            return None, str(exc)
        result["待收金额"] = billing.outstanding({**values, "应收金额": result["应收金额"]})
        return result, ""

    def summary(self) -> dict[str, Any]:
        """页面统计卡的后端口径：应收/已收/待收合计、待核对、争议、本月结算额。"""
        return billing.summarize(store.rows(MODULE))

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"结算单 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于作业结算可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"结算单已{action}"
