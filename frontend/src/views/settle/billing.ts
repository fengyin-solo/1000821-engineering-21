/**
 * 结算金额口径（前端侧）：与后端 app/services/billing.py 保持同一套算法。
 *
 * 这份文件是纯函数、不依赖 Vue 与网络，既能被结算页面直接用，也会被
 * scripts/check-settle 流水线抽出来逐单与后端 /api/settle/trial 对拍，
 * 两边算出的结果不一致时流水线直接判不可提交。
 */

export interface BillingInput {
  作业量: number | string
  装卸单价?: number | string
  港口费率?: number | string
  减免?: number | string
  已收金额?: number | string
}

export interface BillingBreakdown {
  装卸费: number
  港口包干费: number
  减免: number
  应收金额: number
  待收金额: number
}

/** 输入字段非法时抛出的错误，消息与后端 ValueError 文案对应。 */
export class BillingError extends Error {}

const EPSILON = 1e-9

/** ROUND_HALF_UP 保留两位；JS 的 toFixed 是 half-even，不能直接用。 */
export function roundMoney(amount: number): number {
  return Math.round((amount + EPSILON * Math.sign(amount)) * 100) / 100
}

function toAmount(value: number | string | undefined, field: string): number {
  if (value === null || value === undefined || value === '') {
    throw new BillingError(`${field}必须是数字，收到的是 ${String(value)}`)
  }
  const amount = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(amount)) {
    throw new BillingError(`${field}必须是数字，收到的是 ${String(value)}`)
  }
  if (amount < 0) {
    throw new BillingError(`${field}不允许为负数，收到的是 ${String(value)}`)
  }
  return amount
}

/** 按统一口径试算一单：应收 = 作业量×装卸单价 + 作业量×港口费率 - 减免。 */
export function calculateBilling(input: BillingInput): BillingBreakdown {
  const qty = toAmount(input.作业量, '作业量')
  if (qty === 0) {
    throw new BillingError('作业量必须大于 0')
  }
  const unitPrice = toAmount(input.装卸单价 ?? 0, '装卸单价')
  const portRate = toAmount(input.港口费率 ?? 0, '港口费率')
  const discount = toAmount(input.减免 ?? 0, '减免')

  const handling = roundMoney(qty * unitPrice)
  const portFee = roundMoney(qty * portRate)
  const receivable = roundMoney(handling + portFee - discount)
  const received = toAmount(input.已收金额 ?? 0, '已收金额')
  return {
    装卸费: handling,
    港口包干费: portFee,
    减免: roundMoney(discount),
    应收金额: receivable,
    待收金额: roundMoney(Math.max(receivable - received, 0)),
  }
}

export interface SettleRow {
  status?: string
  结算周期?: string
  应收金额?: number | string
  已收金额?: number | string
}

export interface SettleSummary {
  单数: number
  应收金额: number
  已收金额: number
  待收金额: number
  待核对: number
  争议单数: number
  本月结算额: number
}

export const CURRENT_PERIOD = '2026-09'

/** 页面统计卡口径，与后端 billing.summarize 对齐。 */
export function summarizeSettle(rows: SettleRow[], period = CURRENT_PERIOD): SettleSummary {
  let receivable = 0
  let received = 0
  let periodReceivable = 0
  let pendingReview = 0
  let disputed = 0
  for (const row of rows) {
    const rowReceivable = toAmount(row.应收金额 ?? 0, '应收金额')
    const rowReceived = toAmount(row.已收金额 ?? 0, '已收金额')
    receivable += rowReceivable
    received += rowReceived
    if (String(row.结算周期 ?? '') === period) {
      periodReceivable += rowReceivable
    }
    if (row.status === '待核对') pendingReview += 1
    if (row.status === '有争议') disputed += 1
  }
  return {
    单数: rows.length,
    应收金额: roundMoney(receivable),
    已收金额: roundMoney(received),
    待收金额: roundMoney(Math.max(receivable - received, 0)),
    待核对: pendingReview,
    争议单数: disputed,
    本月结算额: roundMoney(periodReceivable),
  }
}
