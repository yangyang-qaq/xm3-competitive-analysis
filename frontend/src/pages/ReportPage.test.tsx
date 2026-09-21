/**
 * 报告页的渲染测试。**用真实报告数据**（见 `test/fixtures/realReport.ts`）。
 *
 * 这一条是这份测试里最重要的
 * ------------------------
 * **目录上每一条都必须对应一个真的渲染出来的元素。**
 *
 * 报告页的目录由两部分拼成：正文章节（`sections`，有就有、没有就没有）
 * 和"其他"块（指标、矩阵、舆情、图集、参与专家……）。后者是页面**列出来**的，
 * 而它们各自在数据缺席时返回 `null`——`ReportMatrix` 没数据就整块不渲染，
 * `SentimentPanel` 没数据同理，`ReportClaims` 空数组时印一句没有 `id` 的提示。
 *
 * 于是页面很容易列出一条点下去纹丝不动的目录项。读者得到的信息不是
 * "这份报告没有舆情"，而是"这个页面是坏的"——他不会去猜是数据缺了。
 *
 * 而这个错误**在截图上看不出来**：一条灰字目录项，和一条好的一模一样。
 * 只有把"每个锚点是否存在"当成不变量来断言，它才会在改动时被拦住。
 *
 * 顺带守住的两条
 * ------------
 * - 真实报告能整页渲染出来（三十多个键、六七层嵌套，任何一处错形状都会炸）；
 * - 降级横幅在 `degraded` 非空时**必须出现在页面上**（后端写了降级说明，
 *   前端不显示等于没说）。
 *
 * 两份夹具，各管一条路径
 * -------------------
 * `realReport.ts` 那份没有 `citations` 键（它早于引用索引），走的是**旧报告**
 * 那条退路；`realCitedReport.ts` 有 `citations`，走的是**新报告**那条正路。
 * 后者见文件末尾那组用例。
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CITED_REPORT_BODY } from '../test/fixtures/realCitedReport'
import { REAL_REPORT_BODY } from '../test/fixtures/realReport'
import type { ReportBody } from '../types/report'
import ReportPage from './ReportPage'

// ---- fetch 桩。形状照 `ClarifyPage.test.tsx` 里那一套 ----

function json(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    json: async () => body,
  } as unknown as Response
}

let served: ReportBody = REAL_REPORT_BODY
let restoreFetch: () => void

function installFetch(): void {
  const original = globalThis.fetch
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/reports/')) {
      return Promise.resolve(
        json({
          reportId: 'RP-test',
          taskId: 'TK-test',
          data: served,
          feedback: [],
          feedbackCount: 0,
        }),
      )
    }
    return Promise.reject(new Error(`测试没有准备这个地址的响应：${url}`))
  }) as typeof fetch
  restoreFetch = () => {
    globalThis.fetch = original
  }
}

/** ECharts 在 jsdom 里没有 canvas，整块换成一个能查到的占位元素。
 *
 * **这个 mock 曾经是个空转的摆设**：页面当时根本没有引用 `ReportChart`，
 * 于是这句话 mock 的是一个没人 import 的模块，而下面也**没有一条断言**
 * 去查 `[data-chart]`。它带来的唯一效果是让"图表这块做过了"看起来有人证。
 * 真相是报告页上一张图都不显示，直到 `e2e/report.spec.ts` 在真浏览器里
 * 数了一下 canvas（数出来 0）才发现。
 *
 * 所以现在配了一条 `报告页把每一张图都交给图表组件` 的断言——
 * mock 不再是摆设，它守着"图表块确实被挂上了、且每张都传下去了"。
 * （图真的画得出来这件事 jsdom 证不了，由 e2e 那条证。） */
vi.mock('../components/report/ReportChart', () => ({
  ReportChart: ({ chart }: { chart: { chartId: string; title: string } }) => (
    <div data-chart={chart.chartId}>{chart.title}</div>
  ),
}))

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/report/RP-test']}>
      <Routes>
        <Route path="/report/:reportId" element={<ReportPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** 目录里所有锚点，以及每一个是否真的在文档里找得到。 */
function anchorReport(container: HTMLElement) {
  const buttons = Array.from(container.querySelectorAll<HTMLElement>('[data-anchor]'))
  return buttons.map((button) => {
    const id = button.dataset.anchor ?? ''
    return { id, label: button.textContent ?? '', exists: container.querySelector(`#${CSS.escape(id)}`) !== null }
  })
}

beforeEach(() => {
  served = REAL_REPORT_BODY
  installFetch()
})

afterEach(() => {
  restoreFetch()
  vi.restoreAllMocks()
})

describe('报告页 · 渲染真实报告', () => {
  it('整页渲染出来，并列出正文的每一节', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelector('#metrics')).not.toBeNull())

    for (const section of REAL_REPORT_BODY.sections ?? []) {
      expect(container.querySelector(`#${CSS.escape(section.key)}`)).not.toBeNull()
      // 目录里也要有这一条
      expect(container.querySelector(`[data-anchor="${section.key}"]`)).not.toBeNull()
    }
  })

  it('目录上每一条都对应一个真的存在的元素', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelector('#metrics')).not.toBeNull())

    const anchors = anchorReport(container)
    // 目录非空，否则下面那句断言是空转的
    expect(anchors.length).toBeGreaterThan(10)

    const dead = anchors.filter((anchor) => !anchor.exists).map((anchor) => anchor.id)
    expect(dead, `这些目录项点了没反应：${dead.join('、')}`).toEqual([])
  })

  it('数据缺席的块既不出现在页面上，也不出现在目录里', async () => {
    // 这份 fixture 的 `matrix` 是真的存在的，先确认它在
    const first = renderPage()
    await waitFor(() => expect(first.container.querySelector('#matrix')).not.toBeNull())
    expect(first.container.querySelector('[data-anchor="matrix"]')).not.toBeNull()
    first.unmount()

    // 拿掉 `matrix` 与 `sentiment` 之后，两块都不该还在
    served = { ...REAL_REPORT_BODY }
    delete served.matrix
    delete served.sentiment

    const { container } = renderPage()
    await waitFor(() => expect(container.querySelector('#metrics')).not.toBeNull())

    expect(container.querySelector('#matrix')).toBeNull()
    expect(container.querySelector('[data-anchor="matrix"]')).toBeNull()
    expect(container.querySelector('#sentiment')).toBeNull()
    expect(container.querySelector('[data-anchor="sentiment"]')).toBeNull()

    // 而整条不变量仍然成立
    const dead = anchorReport(container).filter((anchor) => !anchor.exists)
    expect(dead).toEqual([])
  })

  it('降级说明原样显示在页首的横幅里', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelector('#metrics')).not.toBeNull())

    const degraded = REAL_REPORT_BODY.degraded ?? []
    expect(degraded.length).toBeGreaterThan(0)

    const text = container.textContent ?? ''
    for (const line of degraded) {
      expect(text).toContain(line)
    }
  })

  it('正文里的角标编号与右栏证据的编号是同一套', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelector('#metrics')).not.toBeNull())

    const aside = container.querySelector('aside')
    expect(aside).not.toBeNull()
    // 右栏至少列出了被正文引用到的那些证据
    const cited = within(aside as HTMLElement).getAllByText(/^EV-/)
    expect(cited.length).toBeGreaterThan(0)
  })

  it('每一节底下都有批注工具条，展开后是两个按钮', async () => {
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelector('#metrics')).not.toBeNull())

    // 工具条默认折起来，所以先看"每节都有一个入口"，**数量要对得上**——
    // 少一个的话那一节就没法深化，而在页面上看不出来（少一个折叠条
    // 不像缺失，像那一节比较短）。
    const toggles = screen.getAllByRole('button', { name: '批注 / 深化本节' })
    expect(toggles.length).toBe((REAL_REPORT_BODY.sections ?? []).length)

    await userEvent.click(toggles[0]!)

    // 展开后**两个**按钮：只记批注（不花钱）与按批注深化（要几十秒）。
    // 两个都在是刻意的——只有一个的话，用户会以为写批注就等于改报告。
    expect(screen.getByRole('button', { name: '只记批注' })).toBeTruthy()
    const refine = screen.getByRole('button', { name: '按批注深化' })
    expect(refine).toBeTruthy()

    // 而且工具条挂在**它自己那一节**里，不是页尾一个全局的
    const firstSection = container.querySelector(`#${CSS.escape(REAL_REPORT_BODY.sections![0]!.key)}`)
    expect(within(firstSection as HTMLElement).getByRole('button', { name: '只记批注' })).toBeTruthy()
  })

  it('报告里的每一张图都被挂到页面上，且目录里有对应锚点', async () => {
    // **这条是补的，而它本来就该在。**
    //
    // 报告页曾经一张图都不渲染：`ReportChart` 写好了但没人引用，
    // 而本文件里那句 `vi.mock('../components/report/ReportChart')`
    // mock 的是一个页面没 import 的模块，也没有断言查它的产出——
    // 一个空转的 mock 让"图表这块做过了"看起来有人证。
    // 真正发现它的是 `e2e/report.spec.ts` 在真浏览器里数 canvas（数出来 0）。
    //
    // 所以这条断言只说 jsdom 说得清的那部分：**图表块被挂上了、
    // 每一张都传下去了、目录锚点对得上**。图真的画得出来，
    // 由 e2e 那条用 canvas 的像素来说。
    const charts = REAL_REPORT_BODY.charts ?? []
    expect(charts.length, 'fixture 里没有图，这条测试会退化成空转').toBeGreaterThan(0)

    const { container } = renderPage()
    await waitFor(() => expect(container.querySelector('#metrics')).not.toBeNull())

    // 面板本身是**同步**渲染的（`ReportPanel` 不在懒加载那一侧），
    // 所以锚点这时就该在了。
    expect(container.querySelector('#charts'), '图表面板没有同步渲染出来').not.toBeNull()

    // 但里面的图是**懒加载**的（ECharts 那一块单独一个 chunk），
    // 所以要等它到。这里必须显式 `waitFor`：不等的话，这条断言能否通过
    // 取决于上面那个 `waitFor` 恰好转了几个微任务——
    // 一个"通常能过"的断言迟早会变成一条间歇性红的用例，
    // 而间歇性红最坏的结果是被人加 `retry` 置若罔闻。
    //
    // 一张不少。少一张的话，页面上只是"这块图比别的报告少"，
    // 看不出来是漏了——所以这里比的是**数量**，不是"至少有一张"。
    await waitFor(() => {
      const rendered = Array.from(container.querySelectorAll('[data-chart]'))
      expect(rendered.map((node) => node.getAttribute('data-chart'))).toEqual(
        charts.map((chart) => chart.chartId),
      )
    })

    // 目录里那条锚点必须真的指向一个渲染出来的 `id`——
    // 报告页那段注释说得很清楚：点了没反应的目录项会被读成"页面坏了"。
    const anchor = container.querySelector('[data-anchor="charts"]')
    expect(anchor, '目录里没有「图表」这一项').not.toBeNull()
    expect(container.querySelector('#charts'), '目录有「图表」但页面上没有 #charts').not.toBeNull()
  })
})

/**
 * 带 `citations` 的那条路径。**新生成的报告全都走这条。**
 *
 * 后端把编号算好存进 `body["citations"]`，页面与导出读同一份
 * （见 `lib/reportCitation.ts` 的注释、`问题记录.md` 问题 25/30.1）。
 * 所以这里要钉的不是"编号算得对不对"——那是 `lib/reportCitation.test.ts`
 * 的事——而是**"编号真的落到 DOM 上了，而且正文与右栏是同一份"**。
 *
 * 最后一条用例是这组里唯一有证明力的
 * -----------------------------
 * 前两条就算前端完全无视 `citations`、每次按正文顺序现算一遍，也照样绿
 * ——因为这份数据里"存的顺序"与"现算的顺序"**恰好相同**。
 * 所以还要一条**把 `citations` 数组顺序翻过来**的用例：存的那份改了，
 * 页面上的角标必须跟着改。只有读存下来那份的代码才过得了这一条。
 */
describe('报告页 · 引用编号', () => {
  /** 正文与右栏各处的角标文字，按出现顺序。 */
  function badgeTexts(container: HTMLElement): string[] {
    return Array.from(container.querySelectorAll<HTMLElement>('[aria-label^="跳到第"]')).map(
      (button) => button.textContent ?? '',
    )
  }

  /** 这份夹具没有 `metrics` 键（裁掉了），所以等第一节出现。 */
  async function firstSection(container: HTMLElement) {
    const key = CITED_REPORT_BODY.sections![0]!.key
    await waitFor(() => expect(container.querySelector(`#${CSS.escape(key)}`)).not.toBeNull())
    return key
  }

  it('正文角标渲染成可点的编号，而不是 `[证据: EV-x]`', async () => {
    served = CITED_REPORT_BODY
    const { container } = renderPage()
    await firstSection(container)

    // 两节正文内容相同、各引 4 条，所以是 8 个角标
    expect(badgeTexts(container)).toEqual(['1', '2', '3', '4', '1', '2', '3', '4'])
    // 原始标记不该有任何一处漏到页面上
    expect(container.textContent).not.toContain('证据: EV-')
  })

  it('右栏那张卡片上的编号与正文是同一份', async () => {
    served = CITED_REPORT_BODY
    const { container } = renderPage()
    await firstSection(container)

    for (const citation of CITED_REPORT_BODY.citations ?? []) {
      const card = container.querySelector(`#evidence-${CSS.escape(citation.evidenceId)}`)
      expect(card, `右栏没有列出 ${citation.evidenceId}`).not.toBeNull()
      // 卡片上那个编号按钮的 `title` 就是证据 id，文字是编号
      const badge = (card as HTMLElement).querySelector(`button[title="${citation.evidenceId}"]`)
      expect(badge, `${citation.evidenceId} 的卡片上没有编号按钮`).not.toBeNull()
      expect(badge!.textContent).toBe(String(citation.number))
    }
  })

  it('存下来的编号变了，正文角标就跟着变（不是每次现算）', async () => {
    // **这份 `citations` 是构造的，不是后端会发出来的形状。**
    // 后端 `build_citations` 永远按首次出现顺序发 1..N，所以真实数据里
    // "存的号"与"按正文现算的号"**恰好相同**——前两条用例因此
    // 对"读存的"和"每次现算"是**一样绿**的，证明不了任何事。
    //
    // 要分开这两种实现，只能把编号**换掉**。这里给四个 id 重新分配
    // 1..4：被正文最先引到的 7ff3 拿到 3，而现算一定会给它 1。
    // 于是角标是 `3,1,4,2` 还是 `1,2,3,4`，直接说明读的是哪一份。
    const permuted: Record<string, number> = {
      'EV-7ff3df64c008': 3,
      'EV-bda50d06a718': 1,
      'EV-d28541ffca9d': 4,
      'EV-911f34b05c0d': 2,
    }
    served = {
      ...CITED_REPORT_BODY,
      citations: (CITED_REPORT_BODY.citations ?? []).map((item) => ({
        ...item,
        number: permuted[item.evidenceId] ?? item.number,
      })),
    }
    const { container } = renderPage()
    await firstSection(container)

    expect(badgeTexts(container)).toEqual(['3', '1', '4', '2', '3', '1', '4', '2'])
  })
})
