"""作业结算业务规则：状态流转、字段校验、计费试算与筛选口径都收在这里。

计费口径只有一个入口：``SettleService.trial``（对应 POST /api/settle/trial）。
页面、脚本和后续要接的审批流都走它，改费率或改算法只改这一处，
避免出现“接口一个数、页面一个数”。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.store import store

MODULE = "settle"
REQUIRED_FIELDS = ["结算单号", "结算对象", "结算周期"]
# 登记结算单时允许落库的数值/金额字段；不在白名单内的字段一律不收。
OPTIONAL_AMOUNT_FIELDS = ["作业类型", "作业量", "应收金额", "已收金额", "开票状态", "结算状态"]
STATUS_ORDER = ["待核对", "核对中", "已确认", "已收款", "有争议"]
ACTION_RULES = {"发起核对": "核对中", "确认结算": "已收款", "标记争议": "有争议"}
NEGATIVE_ACTIONS = []

# ===== 计费口径 v1（唯一事实来源）=====
# 不同作业类型的单价（元/单位作业量）。改口径改这里 + fixtures 里的期望值即可。
RATE_TABLE: dict[str, Decimal] = {
    "装卸": Decimal("120.00"),
    "堆存": Decimal("8.50"),
    "引航拖轮": Decimal("3500.00"),
}
# 夜间作业（22:00-次日 06:00 含端点）在单价基础上加成 15%。
NIGHT_SURCHARGE = Decimal("0.15")
# 月结客户优惠 5%，在加成之后计算。
MONTHLY_DISCOUNT = Decimal("0.05")
# 金额一律四舍五入保留两位小数。
MONEY_QUANT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """金额取整：银行家舍入会产生“五舍六入”的争议，口径统一用四舍五入。"""
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _to_decimal(raw: Any, field: str) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field}必须是数字，收到的是「{raw}」") from None


def _is_night(time_text: str) -> bool:
    """识别夜间时段。入参只取小时部分，支持 HH:MM 或完整时间串；无法识别按白天处理。"""
    text = str(time_text or "").strip()
    if not text:
        return False
    try:
        hour = int(text.split("T")[-1][:2])
    except ValueError:
        return False
    return hour >= 22 or hour < 6


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

    def trial(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """按计费口径试算一条结算单的金额，不落库。

        必填：作业类型、作业量。可填：单价（覆盖费率表）、夜间加成、月结优惠、
        作业时间（传了就按小时自动判夜间）。
        返回明细拆分，方便页面和脚本核对每一费用项。
        """
        job_type = str(values.get("作业类型") or "").strip()
        if not job_type:
            return None, "缺少必填字段：作业类型"
        # 注意不能用 `or` 判空：作业量 0 是合法值，会被当成假值漏掉。
        if values.get("作业量") is None or str(values.get("作业量")).strip() == "":
            return None, "缺少必填字段：作业量"

        quantity = _to_decimal(values["作业量"], "作业量")
        if quantity < 0:
            return None, "作业量不能为负数"

        if values.get("单价") not in (None, ""):
            unit_price = _to_decimal(values["单价"], "单价")
            price_source = "自定义"
        else:
            unit_price = RATE_TABLE.get(job_type)
            price_source = "费率表"
            if unit_price is None:
                known = "、".join(RATE_TABLE)
                return None, f"作业类型「{job_type}」没有配置单价，请先在费率表登记（已有：{known}）"
        if unit_price < 0:
            return None, "单价不能为负数"

        night = bool(values.get("夜间加成")) or _is_night(str(values.get("作业时间", "")))
        monthly = bool(values.get("月结优惠"))

        base = _money(quantity * unit_price)
        surcharge_rate = NIGHT_SURCHARGE if night else Decimal("0")
        surcharge = _money(base * surcharge_rate)
        discount_rate = MONTHLY_DISCOUNT if monthly else Decimal("0")
        # 优惠按“基础费用 + 夜间加成”后的金额计算。
        discount = _money((base + surcharge) * discount_rate)
        total = _money(base + surcharge - discount)

        return {
            "作业类型": job_type,
            "作业量": float(quantity),
            "单价": float(unit_price),
            "单价来源": price_source,
            "夜间加成": night,
            "月结优惠": monthly,
            "基础费用": float(base),
            "夜间费用": float(surcharge),
            "优惠金额": float(discount),
            "应收金额": float(total),
        }, ""

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_AMOUNT_FIELDS:
            if field in values and values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry.setdefault("应收金额", 0)
        entry.setdefault("已收金额", 0)
        entry.setdefault("开票状态", "未开票")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

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
