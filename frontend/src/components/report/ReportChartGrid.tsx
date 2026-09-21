/**
 * 真正画图的那一格。**单独一个文件，唯一理由是打包体积。**
 *
 * 这个文件 import 了 `ReportChart`，而后者 import 了 ECharts。
 * 它是全站**唯一**碰 ECharts 的地方，所以它必须和别的代码分开成一块，
 * 由 `ReportCharts` 用 `lazy()` 按需拉。
 *
 * 不分开的代价是实测出来的：
 *
 *     图表块不接（也没有 import）    437 kB / gzip 132 kB
 *     图表块直接静态 import         1010 kB / gzip 327 kB
 *
 * 多出来的约 572 kB 会进**首屏那个 chunk**，也就是首页、工作台、
 * 仪表盘、专家页全都要为它付一次——而它们一张图都没有。
 * 路由没有做懒加载（`App.tsx` 里全是静态 import），所以这一层必须自己拆。
 *
 * 拆成两个文件而不是把 `lazy()` 写在 `ReportCharts` 里包住整块面板：
 * `ReportPanel` 的 `id="charts"` 必须**同步**渲染出来。报告页的目录
 * 是按数据算出来的（后端发了 charts 就列这一项），所以先有目录项、
 * 后面板才随 chunk 到达的那一小段时间里，点目录会没反应——
 * 而"点了没反应的目录项"正是报告页那段注释要避免的东西。
 * 把面板留在同步那一侧，这个窗口就不存在。
 */
import { ReportChart } from './ReportChart'
import type { ReportChart as ReportChartSpec } from '../../types/report'

export function ReportChartGrid({ charts }: { charts: ReportChartSpec[] }) {
  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
      {charts.map((chart) => (
        <ReportChart key={chart.chartId} chart={chart} />
      ))}
    </div>
  )
}
