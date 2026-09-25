#!/usr/bin/env node
/**
 * 页面侧金额核对：把结算页面用的同一份 billing.ts 抽出来执行，
 * 逐单与后端 /api/settle/trial 对拍，再把汇总与 /api/settle/summary 对拍。
 *
 * 由 scripts/check-settle.py 调用（不直接面向人）：
 *   node page-check.mjs <billing.ts 绝对路径> <后端基址> <fixture 路径>
 *
 * 退出码：0 全部一致；1 存在口径漂移或接口错误。结果以 JSON 打到 stdout。
 */
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'

const [, , billingPath, backendBase, fixturePath] = process.argv
if (!billingPath || !backendBase || !fixturePath) {
  console.error('用法: node page-check.mjs <billing.ts> <backendBase> <fixture.json>')
  process.exit(2)
}

// 脚本放在仓库 scripts/ 下，esbuild 装在 frontend/node_modules：
// ESM 按脚本文件位置而不是 cwd 解析裸包名，所以显式从前端目录加载。
// billing.ts 位于 <frontend>/src/views/settle/billing.ts，上溯四级即前端根。
const frontendRoot = path.resolve(billingPath, '../../../..')
const requireFromFrontend = createRequire(path.join(frontendRoot, 'package.json'))
const { build } = requireFromFrontend('esbuild')

const TOLERANCE = 0.005 // 金额只保留两位，0.005 以内视为一致

function compare(label, page, api, path = '') {
  const a = Number(page)
  const b = Number(api)
  if (!Number.isFinite(a) || !Number.isFinite(b) || Math.abs(a - b) >= TOLERANCE) {
    return { field: path || label, page: page, api: api }
  }
  return null
}

async function main() {
  // 用 esbuild 把 TS 纯函数即时转成 ESM，走的就是页面真正 import 的那份源码，
  // 不复制算法、不另起一份测试实现。
  const bundled = await build({
    entryPoints: [billingPath],
    bundle: true,
    write: false,
    format: 'esm',
  })
  const module = await import(`data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].contents).toString('base64')}`)
  const { calculateBilling, summarizeSettle } = module

  const fixture = JSON.parse(await readFile(fixturePath, 'utf-8'))
  const diffs = []
  const apiRows = []

  for (const testCase of fixture.cases) {
    const pageResult = calculateBilling(testCase)
    const response = await fetch(`${backendBase}/api/settle/trial`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: testCase }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      diffs.push({ field: `${testCase.结算单号}/试算接口`, page: 'ok', api: payload?.message || `HTTP ${response.status}` })
      continue
    }
    for (const key of ['装卸费', '港口包干费', '应收金额']) {
      const diff = compare(key, pageResult[key], payload.entry[key], `${testCase.结算单号}/${key}`)
      if (diff) diffs.push(diff)
    }
    apiRows.push({
      status: testCase.status,
      结算周期: testCase.结算周期,
      应收金额: payload.entry.应收金额,
      已收金额: Number(testCase.已收金额 || 0),
    })
  }

  // 汇总：页面函数吃「接口算出来的应收」，再与后端汇总接口对拍，
  // 这样既验证逐单口径，也验证统计卡口径。
  const pageSummary = summarizeSettle(apiRows)
  const summaryResponse = await fetch(`${backendBase}/api/settle/summary`)
  const apiSummary = await summaryResponse.json()
  if (!summaryResponse.ok) {
    diffs.push({ field: '汇总接口', page: 'ok', api: `HTTP ${summaryResponse.status}` })
  } else {
    for (const key of ['单数', '应收金额', '已收金额', '待收金额', '待核对', '争议单数', '本月结算额']) {
      const diff = compare(key, pageSummary[key], apiSummary[key], `汇总/${key}`)
      if (diff) diffs.push(diff)
    }
  }

  const result = {
    checkedCases: fixture.cases.length,
    pageSummary,
    apiSummary,
    diffs,
  }
  console.log(JSON.stringify(result, null, 2))
  process.exit(diffs.length === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error(error?.stack || String(error))
  process.exit(2)
})
