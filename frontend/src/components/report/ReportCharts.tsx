/**
 * 报告页的图表区。**这个文件是补上的，理由值得写下来。**
 *
 * `ReportChart` 早就写好了——按需引入、ResizeObserver、空图给一句说明，
 * 一百多行，连它修过的一个 bug 都记在文件头。但它**没有任何地方引用**：
 * 报告页没 import 它，全站只有那个文件自己提过自己。
 * 于是报告页上四张图一张都不显示，而这件事**没有任何测试发现**，
 * 因为 `ReportPage.test.tsx` 里有一句
 *
 *     vi.mock('../components/report/ReportChart', () => ({ ... }))
 *
 * ——它 mock 的是一个页面根本没引用的模块，而且没有一条断言去查那个
 * 假组件渲染出来的东西。一个死 mock 给了一个死组件一个"有人在用我"的假象。
 *
 * 发现它的是 `e2e/report.spec.ts`：那份测试在真浏览器里数 canvas，
 * 数出来是 0。（jsdom 没有 canvas，所以这个问题在单测层面
 * **结构上不可见**——那不是"单测写得不够多"，是那一层测不了这件事。）
 *
 * 顺带一个更早的征兆，当时被读过去了：`completeness.blocks` 里有一项叫
 * `图表`，页面上的"模块完整度"那张表会把它列出来，还可能标着 `degraded`
 * ——**页面先告诉读者有图表这块，然后一个图都不给**。
 *
 * 位置放在矩阵与结构化块之后
 * --------------------------
 * 后端这四张图就是矩阵和份额的图形化（`chart-matrix-bar` / `-radar` /
 * `chart-market-share` / `chart-source-mix`），紧挨着它们的**数据来源**
 * 放，读者一眼能对上"这张图和上面那张表是同一批数"。
 * 放到页面末尾的附录里，这层对应关系就没了。
 */
import { Suspense, lazy } from 'react'

import { ReportPanel } from './ReportPanel'
import type { ReportChart as ReportChartSpec } from '../../types/report'

/**
 * 图表网格**按需拉**。ECharts 约 572 kB（实测，见 `ReportChartGrid`），
 * 而它是全站唯一碰 ECharts 的地方——静态 import 的话这 572 kB 会进
 * 首屏那个 chunk，首页、工作台、仪表盘全都要为一个它们用不到的东西付一次。
 *
 * 只有**这一块**是懒的：外面的 `ReportPanel`（连同 `id="charts"`）同步渲染，
 * 所以目录里那条锚点从第一帧起就指向一个真实存在的元素。
 */
const ReportChartGrid = lazy(() =>
  import('./ReportChartGrid').then((module) => ({ default: module.ReportChartGrid })),
)

/** 等 chunk 到达时占位。**高度照着两张图**给，不是给一行 spinner——
 * 后者会让下面的内容在 chunk 到达时整体跳一下，而读者正在读那里。 */
function GridFallback() {
  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2" aria-hidden="true">
      {[0, 1].map((key) => (
        <div key={key} className="h-[300px] animate-pulse rounded-card border border-line bg-raised" />
      ))}
    </div>
  )
}

export function ReportCharts({ charts }: { charts?: ReportChartSpec[] }) {
  const list = charts ?? []
  // 没有图就整块不渲染，连 `id` 都不给——与 `ReportMatrix` 的约定一致。
  // 渲染一个空卡片的话，TOC 上会多一条点了没反应的锚点，
  // 而读者点一下没动，得到的信息是"这个页面坏了"。
  //
  // **这条早退条件与 `ReportPage` 的 `anchors` 是耦合的**：
  // 那边列 `charts` 锚点的条件必须与这里一致。见报告页那段注释。
  if (list.length === 0) return null

  return (
    <ReportPanel id="charts" title="图表" count={list.length}>
      <Suspense fallback={<GridFallback />}>
        <ReportChartGrid charts={list} />
      </Suspense>
    </ReportPanel>
  )
}
