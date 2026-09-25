"""结算计费口径：金额怎么算统一收在这一处。

试算没有固定入口、改一次口径要人工核页面，根因是算法散在调用方。这里只做纯计算，
服务层、路由层和 scripts/check-settle 流水线都走同一套函数；前端在
frontend/src/views/settle/billing.ts 维护同口径实现，由流水线逐单比对。

金额一律用 Decimal 计算，ROUND_HALF_UP 保留两位，避免 0.1 + 0.2 这类浮点尾巴。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

SCALE = Decimal("0.01")

# 字段键名与示例数据 cases.json 保持一致，避免再来一层中英映射。
QTY_KEY = "作业量"
UNIT_PRICE_KEY = "装卸单价"
PORT_RATE_KEY = "港口费率"
DISCOUNT_KEY = "减免"
RECEIVED_KEY = "已收金额"
PERIOD_KEY = "结算周期"

CURRENT_PERIOD = "2026-09"


def _money(value: Any, field: str) -> Decimal:
    """把入参安全转成非负 Decimal；非法或为负时带上字段名抛错。"""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field}必须是数字，收到的是 {value!r}") from None
    if amount < 0:
        raise ValueError(f"{field}不允许为负数，收到的是 {value!r}")
    return amount


def _qty(value: Any) -> Decimal:
    qty = _money(value, QTY_KEY)
    if qty == 0:
        raise ValueError(f"{QTY_KEY}必须大于 0")
    return qty


def _round(amount: Decimal) -> Decimal:
    return amount.quantize(SCALE, rounding=ROUND_HALF_UP)


def calculate(values: dict[str, Any]) -> dict[str, float]:
    """按统一口径试算一单，返回装卸费/港口包干费/减免/应收金额。

    应收金额 = 作业量×装卸单价 + 作业量×港口费率 - 减免
    """
    qty = _qty(values.get(QTY_KEY))
    unit_price = _money(values.get(UNIT_PRICE_KEY, 0), UNIT_PRICE_KEY)
    port_rate = _money(values.get(PORT_RATE_KEY, 0), PORT_RATE_KEY)
    discount = _money(values.get(DISCOUNT_KEY, 0), DISCOUNT_KEY)

    handling = _round(qty * unit_price)
    port_fee = _round(qty * port_rate)
    receivable = _round(handling + port_fee - discount)
    return {
        "装卸费": float(handling),
        "港口包干费": float(port_fee),
        "减免": float(_round(discount)),
        "应收金额": float(receivable),
    }


def outstanding(values: dict[str, Any]) -> float:
    """待收金额 = 应收 - 已收；已收超过应收按 0 处理。"""
    receivable = Decimal(str(values.get("应收金额", calculate(values)["应收金额"])))
    received = _money(values.get(RECEIVED_KEY, 0), RECEIVED_KEY)
    return float(_round(max(receivable - received, Decimal(0))))


def summarize(rows: list[dict[str, Any]], *, period: str = CURRENT_PERIOD) -> dict[str, Any]:
    """页面统计卡口径：单数、应收/已收/待收合计、待核对与争议单数、本月结算额。

    本月结算额取结算周期等于当前周期的应收金额合计，和页面卡片一致。
    """
    total_receivable = Decimal("0")
    total_received = Decimal("0")
    period_receivable = Decimal("0")
    pending_review = 0
    disputed = 0
    for row in rows:
        receivable = _money(row.get("应收金额", 0), "应收金额")
        received = _money(row.get(RECEIVED_KEY, 0), RECEIVED_KEY)
        total_receivable += receivable
        total_received += received
        if str(row.get(PERIOD_KEY) or "") == period:
            period_receivable += receivable
        if row.get("status") == "待核对":
            pending_review += 1
        if row.get("status") == "有争议":
            disputed += 1
    return {
        "单数": len(rows),
        "应收金额": float(_round(total_receivable)),
        "已收金额": float(_round(total_received)),
        "待收金额": float(_round(max(total_receivable - total_received, Decimal(0)))),
        "待核对": pending_review,
        "争议单数": disputed,
        "本月结算额": float(_round(period_receivable)),
    }
