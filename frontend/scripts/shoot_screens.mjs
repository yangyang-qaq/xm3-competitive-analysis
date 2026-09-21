/**
 * 作品集截图：**对着跑起来的真应用**截，而不是手工截了贴进 README。
 *
 * 为什么要有这个文件
 * ------------------
 * `e2e/report.spec.ts` 也会截图，但它截的是**夹具报告 + 拦掉所有 API**的页面——
 * 那是为了在 CI 里稳定复现"图有没有画出来"，所以它必须离线。
 * README 上要给人看的那几张不一样：它们要的是**真库里的真报告**，
 * 有真证据、真图表、真专家名册。这两件事的目标不同，所以是两个脚本。
 *
 * 更重要的理由是**手工截图会腐烂**：截一次贴进 README，之后页面改了十次，
 * 那张图仍然在 README 里指着已经不存在的界面，而没有任何东西会告诉你。
 * 这里的约定是：`docs/images/` 里的每一张图都由 `npm run shots` 重新生成，
 * 图过期了就是**再跑一次**的事，不是"记得去截一张"。
 *
 * 它不是 CI 的一部分，也不该是
 * ---------------------------
 * 它需要三样 CI 里没有的东西：跑着的后端（8020）、跑着的前端（3500）、
 * **库里有真数据**。三条里缺一条，它就只能截出一堆空状态——
 * 那种图比没有图更糟，因为它看起来像"这个产品是空的"。
 * 所以它只在**要更新截图时**手工跑，并且跑之前先确认 8020/3500 都在。
 *
 * 用法
 * ----
 *   cd frontend && npm run shots                    # 自动挑库里最丰富的那份报告
 *   npm run shots -- --report RP-xxxx --task TK-xxxx # 指定
 *   npm run shots -- --base http://127.0.0.1:3500
 *
 * 退出码
 * ------
 * 控制台报错会让它红。**截图跑一遍等于把每个页面在真浏览器里打开一次**，
 * 那就顺手把"打开时有没有报错"也管上——一个只截图的脚本，
 * 会让你在一张带着报错弹层的图上工作很久才发现。
 */
import { mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from '@playwright/test'

const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = resolve(HERE, '../../docs/images')

/** 视口用 1600×1000、缩放 1.5：宽到能放下工作台的三栏，密度又还看得清字。 */
const VIEWPORT = { width: 1600, height: 1000 }
const SCALE = 1.5

function arg(name, fallback) {
  const hit = process.argv.find((a) => a === `--${name}` || a.startsWith(`--${name}=`))
  if (!hit) return fallback
  const eq = hit.indexOf('=')
  return eq === -1 ? process.argv[process.argv.indexOf(hit) + 1] : hit.slice(eq + 1)
}

const BASE = arg('base', 'http://127.0.0.1:3500').replace(/\/$/, '')

async function api(path) {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw new Error(`${path} 返回 ${res.status}`)
  return res.json()
}

/**
 * 挑一份"最值得截图"的报告。
 *
 * 按**证据条数**排而不是按时间：最新那份可能刚好是个失败运行（降级过、
 * 图少、章节空），那样截出来的图是在展示这个系统的坏状态。
 * 证据多说明它真的跑完了全流程——这也是 README 上想让人看到的样子。
 */
async function pickReport() {
  const given = arg('report')
  if (given) return given
  const list = await api('/api/reports')
  const rows = Array.isArray(list) ? list : (list.items ?? [])
  if (!rows.length) throw new Error('库里一份报告都没有，先把流水线跑通再截图')
  let best = rows[0]
  let bestCount = -1
  for (const row of rows) {
    const detail = await api(`/api/reports/${row.reportId ?? row.report_id}`)
    const count = (detail?.data?.evidences ?? []).length
    if (count > bestCount) {
      bestCount = count
      best = row
    }
  }
  const id = best.reportId ?? best.report_id
  console.log(`挑中报告 ${id}（${bestCount} 条证据）：${best.query ?? ''}`)
  return id
}

async function pickTask(reportId) {
  const given = arg('task')
  if (given) return given
  const detail = await api(`/api/reports/${reportId}`)
  if (!detail?.taskId) throw new Error('这份报告没有 taskId，工作台那一页截不了')
  return detail.taskId
}

async function main() {
  const reportId = await pickReport()
  const taskId = await pickTask(reportId)
  await mkdir(OUT, { recursive: true })

  const pages = [
    { name: 'home', url: '/', settle: 1200 },
    { name: 'experts', url: '/experts', settle: 1500 },
    // 工作台要等 SSE 把 journal 补完再截，否则截到的是"正在读取任务…"。
    // 等 12 秒是量出来的：848 条事件在局域网内推完约 3 秒，留 4 倍余量。
    { name: 'workspace', url: `/workspace/${taskId}`, settle: 12_000 },
    { name: 'report', url: `/report/${reportId}`, settle: 5000 },
    // 图表在首屏之下，单独截那一块。**裁元素而不是整页**：图表是这一页
    // 最花时间的东西，也是唯一能证明"ECharts 真的画出来了"的东西。
    { name: 'report-charts', url: `/report/${reportId}`, settle: 5000, element: '#charts' },
    // d3-force 的布局要几百毫秒才收敛，等 8 秒是为了让它停下来。
    { name: 'graph', url: `/graph/${reportId}`, settle: 8000 },
    { name: 'trace', url: `/trace/${taskId}`, settle: 4000 },
    { name: 'dashboard', url: '/dashboard', settle: 2500 },
    { name: 'library', url: '/library', settle: 2000 },
  ]

  const browser = await chromium.launch()
  const problems = []
  try {
    for (const spec of pages) {
      // 每一页开一个新 context：上一步留下的滚动位置和 store 状态都在
      // 同一个页面里，共用的话第二张图会带着第一页滚过的位置。
      const context = await browser.newContext({
        viewport: VIEWPORT,
        deviceScaleFactor: SCALE,
        locale: 'zh-CN',
      })
      const page = await context.newPage()
      const errors = []
      page.on('console', (msg) => {
        if (msg.type() === 'error') errors.push(msg.text())
      })
      page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`))

      await page.goto(`${BASE}${spec.url}`, { waitUntil: 'load' })
      await page.waitForTimeout(spec.settle)

      const file = resolve(OUT, `${spec.name}.png`)
      if (spec.element) {
        const target = page.locator(spec.element)
        await target.scrollIntoViewIfNeeded()
        await target.screenshot({ path: file })
      } else {
        await page.screenshot({ path: file })
      }
      await context.close()

      if (errors.length) {
        problems.push(`[${spec.name}] 控制台有报错：\n    ${errors.join('\n    ')}`)
        console.log(`  ${spec.name.padEnd(14)} 截了，但**有控制台报错**`)
      } else {
        console.log(`  ${spec.name.padEnd(14)} ok`)
      }
    }
  } finally {
    await browser.close()
  }

  console.log(`\n图在 ${OUT}`)
  if (problems.length) {
    console.log(`\n有 ${problems.length} 个页面在打开时报了错：\n${problems.join('\n')}`)
    return 1
  }
  return 0
}

process.exit(await main())
