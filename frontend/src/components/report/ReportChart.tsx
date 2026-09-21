/**
 * 一张图。**报告页上唯一碰 ECharts 的地方。**
 *
 * 按需引入而不是 `import * as echarts from 'echarts'`
 * ------------------------------------------------
 * 后者会把全部图表类型和组件打进包里（ECharts 全量约 1MB）。
 * 这一页只画四种图（柱 / 雷达 / 饼 / 折线）加四个组件，按需引入之后
 * 打包体积是这个数字的零头。代价是每加一种图要在这里多写一行 `use`，
 * 而漏写的那一行**不会报错**——ECharts 只是不画，控制台里也不一定有声音。
 * 所以 `use([...])` 这张清单要和 `lib/chartOptions.ts` 的分支一一对应。
 *
 * 折线那条是这么被发现的：`analysis/charts._trend_chart` 从第一天起就发
 * `kind: "line"`，而这份清单里没有它、`chartOptions` 里也没有这个分支，
 * 于是趋势图在页面上一直是那句"这一版还不认识这种图（line）"。
 * 没有报错、没有白屏——**那句说明本身就是这个设计的产出**：
 * 它把一个静默的空白变成了一句能读的话。
 *
 * `chartOption()` 返回 `null` 时不画，显示一句说明
 * ----------------------------------------------
 * 两种情况会走到那里（见 `chartOptions.chartOption` 的注释）：
 * 后端新加了一种本版页面不认识的图，或者 `spec` 的形状不对。
 * 两种都**不该让整页崩掉**——一份报告不该因为一张图而打不开。
 *
 * 为什么用 ResizeObserver 而不是监听 `window.resize`
 * ----------------------------------------------
 * 报告页的左栏可以收起，收起时中间那栏变宽，**而窗口尺寸一点没变**。
 * 只听 `window.resize` 的图会保持旧宽度，和旁边的正文对不齐。
 */
import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart, RadarChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  RadarComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import { chartOption } from '../../lib/chartOptions'
import type { ReportChart as ReportChartSpec } from '../../types/report'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  RadarChart,
  GridComponent,
  LegendComponent,
  RadarComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
])

/** 图表卡片的固定高度。**不能靠内容撑**——canvas 撑不起父元素的高度。 */
const HEIGHT = 260

export function ReportChart({ chart }: { chart: ReportChartSpec }) {
  const hostRef = useRef<HTMLDivElement | null>(null)

  // **只算一次。** 下面渲染时要拿它判空、副作用里要拿它画图，
  // 算两遍的话，两份结果一旦不同（`chartOption` 现在纯，将来不一定），
  // 会出现"渲染了画布但没人往上画"的空白图。
  //
  // 依赖放 `chart` 本身而不是它的字段：报告页只在换了一份报告时才传进来
  // 一个新对象，这个依赖足够稳定，而拆成字段反而容易漏掉新增的那种图。
  const option = useMemo(() => chartOption(chart), [chart])

  useEffect(() => {
    const host = hostRef.current
    if (!host || option === null) return

    const instance = echarts.init(host)
    instance.setOption(option)

    // 宽度变了就 `resize()`；只监听尺寸，不监听内容。
    const observer = new ResizeObserver(() => instance.resize())
    observer.observe(host)

    return () => {
      observer.disconnect()
      // `dispose()` 而不是只 `clear()`：后者会留下 canvas 与事件监听，
      // 报告页有四张图，来回切几次报告就会攒下一堆。
      instance.dispose()
    }
  }, [option])

  return (
    <figure className="rounded-card border border-line bg-panel p-3">
      <figcaption className="mb-1.5 flex items-baseline justify-between gap-3">
        <span className="text-[12px] font-medium text-fg">{chart.title}</span>
        {chart.evidenceIds.length > 0 && (
          <span className="shrink-0 text-[11px] text-fg-faint tabular">
            {chart.evidenceIds.length} 条证据
          </span>
        )}
      </figcaption>

      {option === null ? (
        // 一句说明，而不是一张空图。空图会被读成"这个维度没数据"，
        // 而真相是这张图画不出来——两件事完全不同。
        //
        // 措辞**不点具体的因**：`chartOption` 返回 `null` 有四种因
        // （不认识的 kind、spec 形状不对、折线排不出时间序、折线没有可用序列），
        // 而"这一版还不认识这种图"只说中了第一种。第一版就是那么写的，
        // 于是 `spec` 形状不对时页面会指着一个**它认识的** kind 说不认识
        // ——那句话会把人送去后端查图表类型，而问题在数据里。
        <div className="flex h-[100px] flex-col items-center justify-center gap-1 rounded-lg bg-raised px-4 text-center">
          <p className="text-[12px] text-fg-muted">这张图没画出来（{chart.kind}）</p>
          <p className="text-[11px] leading-relaxed text-fg-faint">
            可能是这一版还不认识这种图，也可能是数据本身画不成图。
            图表的数据在导出的 Markdown 与 JSON 里都还在。
          </p>
        </div>
      ) : (
        <div ref={hostRef} style={{ height: HEIGHT }} className="w-full" />
      )}

      {/* 后端写在这张图上的披露。**不能省**——市场份额图的那句
          "份额为推算值"、舆情图的那句混合标注比例，都是图本身
          没法表达、而读者必须知道的前提。 */}
      {chart.spec.note && (
        <p className="mt-1.5 border-t border-line pt-1.5 text-[11px] leading-relaxed text-fg-faint">
          {chart.spec.note}
        </p>
      )}
    </figure>
  )
}
