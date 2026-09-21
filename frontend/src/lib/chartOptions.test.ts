/**
 * 图表规格 → ECharts option：这一侧的那条测试。
 *
 * 为什么这些用例断言的键名这么具体
 * ------------------------------
 * ECharts **不认识拼错的键**，也不认识多余的键。写错一个字母，
 * 它不报错、不警告，那张图只是少了一部分。所以这里钉的是"哪个键
 * 装着什么"，而不是"画出来好不好看"——后者只能靠眼睛，
 * 而前者是唯一能在 CI 里守住的东西。
 *
 * 三条用例是真的抓着决策的（不是补覆盖率）：
 * - `柱状图的 yAxis`：**后端给了量表就用后端的**（矩阵是 1–5 分量表，
 *   自适应轴会让 4.2 与 4.4 看着差一倍），后端没给才自适应——但下限
 *   无论哪种都从 0 起。这三条是一件事的三个面，拆开是因为它们会
 *   分别被别人改坏。
 * - `饼图的 tooltip 用 {c} 不用 {d}`：见下面那条用例的注释，
 *   用 `{d}` 会印出一个正文与证据里都不存在的百分比。
 * - `画不出来时返回 null`：这是"一份报告不该因为一张图而打不开"。
 */
import { describe, expect, it } from 'vitest'

import {
  barOption,
  chartOption,
  lineOption,
  pieOption,
  radarOption,
  themeColor,
} from './chartOptions'
import type { BarSpec, LineSpec, PieSpec, RadarSpec, ReportChart } from '../types/report'

const BAR: BarSpec = {
  categories: ['功能完整度', '定价'],
  series: [
    { brand: '甲', color: '#c2410c', values: [4, 3] },
    { brand: '乙', values: [2, 5] },
  ],
}

const RADAR: RadarSpec = {
  indicators: [
    { name: '功能完整度', max: 5 },
    { name: '定价', max: 5 },
  ],
  series: [{ brand: '甲', values: [4, 3] }],
}

const PIE: PieSpec = {
  data: [
    { brand: '特来电', share: 41.0, basis: '充电桩保有量' },
    { brand: '星星充电', share: 27.4, basis: '充电量' },
  ],
}

/**
 * 构造一张后端新加、本版页面不认识的图。
 *
 * 只能靠断言：类型这一侧**刻意**没有 `kind: string` 的兜底成员
 * （加了的话按 `kind` 收窄就永远收窄不干净，见 `types/report.ts`）。
 * 但 JSON 里真的会出现这种图——类型声明的是"本版本认识的"，
 * 不是"所有可能存在的"。
 */
function foreignChart(kind: string, spec: unknown): ReportChart {
  return { chartId: 'c-x', kind, title: '外来图', evidenceIds: [], spec } as unknown as ReportChart
}

describe('轴与文字的颜色', () => {
  it('取不到 CSS 变量时用兜底值，不抛错', () => {
    // 图表是在 useEffect 里画的。抛错会冒泡到 React 的渲染阶段，
    // 整个报告页白屏——而"轴的颜色没取到"是件该降级处理的小事。
    expect(themeColor('--xm3-这个变量不存在', '#6d675b')).toBe('#6d675b')
  })

  it('变量存在时用变量的值', () => {
    document.documentElement.style.setProperty('--xm3-test-color', '#123456')
    try {
      expect(themeColor('--xm3-test-color', '#6d675b')).toBe('#123456')
    } finally {
      document.documentElement.style.removeProperty('--xm3-test-color')
    }
  })
})

describe('柱状图', () => {
  it('每个品牌一条系列，值原样搬过去', () => {
    const option = barOption(BAR)
    expect(option.series).toEqual([
      { name: '甲', type: 'bar', data: [4, 3], itemStyle: { color: '#c2410c' }, barMaxWidth: 22 },
      { name: '乙', type: 'bar', data: [2, 5], itemStyle: undefined, barMaxWidth: 22 },
    ])
    expect(option.xAxis).toMatchObject({ type: 'category', data: ['功能完整度', '定价'] })
  })

  it('后端没给量表时，yAxis 不给上限，但从 0 起', () => {
    // 矩阵的分数是 1–5，而"未来可能有的别的柱状图"不一定是。
    // 写死上限会把超出部分截掉——而截掉的柱子看起来只是
    // "这个品牌分低"，不像一个被裁掉的数据。这条用例是那个决策的守卫：
    // 谁在没数据源的时候加了 `max`，这里就红。
    //
    // 下限则相反，**必须给 0**：不从零起的柱状图会让人把
    // "4.2 对 4.4"读成"差了一倍"。
    const yAxis = barOption(BAR).yAxis as Record<string, unknown>
    expect(yAxis).not.toHaveProperty('max')
    expect(yAxis).toMatchObject({ type: 'value', min: 0 })
  })

  it('后端给了量表就用它，不被上面的自适应覆盖', () => {
    // 真实数据里能力矩阵那根柱状图带着 `yAxis: {min:0, max:5, name:"评分"}`，
    // 后端在说"这是 1–5 分的量表"。前端丢掉它、让 ECharts 自适应的话，
    // 4.2 与 4.4 的柱子会看起来差一倍，而**图的标题写着"评分"**。
    const yAxis = barOption({
      ...BAR,
      yAxis: { min: 0, max: 5, name: '评分' },
    }).yAxis as Record<string, unknown>
    expect(yAxis).toMatchObject({ min: 0, max: 5, name: '评分' })
  })

  it('量表只给一半时，另一半按默认走', () => {
    // 后端哪天只给 `max` 不给 `min`，`min` 仍然要是 0——
    // 不能因为"有量表了"就把下限交给 ECharts 自适应。
    const onlyMax = barOption({ ...BAR, yAxis: { max: 5 } }).yAxis as Record<string, unknown>
    expect(onlyMax).toMatchObject({ min: 0, max: 5 })
    expect(onlyMax).not.toHaveProperty('name')
  })

  it('分类轴标签斜排', () => {
    // 维度名有七八个字，横排会互相压住。
    const xAxis = barOption(BAR).xAxis as { axisLabel: { rotate: number } }
    expect(xAxis.axisLabel.rotate).toBe(30)
  })

  it('没有系列时画出一张空图，而不是抛错', () => {
    // 这里说的是 `spec.series` 是 `[]`——那是**合法的**空数据
    // （一份没采到矩阵的报告）。与"`series` 键根本不存在"不同，
    // 后者由 `chartOption` 拦掉，见下面那组。
    expect(() => barOption({ categories: ['x'], series: [] })).not.toThrow()
    expect(barOption({ categories: ['x'], series: [] }).series).toEqual([])
  })
})

describe('雷达图', () => {
  it('指标原样搬进 radar.indicator', () => {
    const radar = radarOption(RADAR).radar as { indicator: unknown }
    expect(radar.indicator).toEqual([
      { name: '功能完整度', max: 5 },
      { name: '定价', max: 5 },
    ])
  })

  it('所有品牌挤在同一条 series 里', () => {
    // 这是 ECharts 雷达图的形状：series 只有一条，品牌在 `data` 里。
    // 按柱状图那样"一个品牌一条 series"写的话，图能画出来但叠加不对。
    const series = radarOption(RADAR).series as unknown[]
    expect(series).toHaveLength(1)
    expect(series[0]).toMatchObject({ type: 'radar', data: [{ name: '甲', value: [4, 3] }] })
  })
})

describe('饼图', () => {
  it('tooltip 用 {c} 而不是 {d}', () => {
    // **这不是风格问题。** `{d}` 是 ECharts 自己归一化出来的占比
    // （各片 ÷ 总和），而后端给的 `share` 本来就是百分比。
    // 上面那组数据（41.0 + 27.4 = 68.4）用 `{d}` 会显示成 60% 和 40%
    // ——两个正文和证据里都不存在的数。所以这里钉的是这个字面量。
    expect(pieOption(PIE).tooltip).toEqual({ trigger: 'item', formatter: '{b}：{c}%' })
  })

  it('share 直接当 value，不做归一化', () => {
    const series = pieOption(PIE).series as Array<{ data: unknown }>
    expect(series[0]?.data).toEqual([
      { name: '特来电', value: 41.0 },
      { name: '星星充电', value: 27.4 },
    ])
  })
})

describe('折线图', () => {
  /**
   * **这份数据是刻意不对称的。**
   *
   * 两条线的时间点不一样：`充电桩数` 少一个 2024Q1，`用户数` 多一个 2024Q2。
   * 如果只有一条线、或者两条线的时间点完全一致，"拿第一条线的 points
   * 当横轴"和"取并集"会给出**同一个结果**——那样这组用例就什么都证明不了。
   */
  const LINE: LineSpec = {
    series: [
      {
        name: '充电桩数',
        unit: '万台',
        points: [
          { period: '2023Q4', value: 12 },
          { period: '2024Q2', value: 18 },
          { period: '2024Q3', value: 21 },
        ],
      },
      {
        name: '用户数',
        unit: '万人',
        points: [
          { period: '2023Q4', value: 300 },
          { period: '2024Q1', value: 340 },
          { period: '2024Q2', value: 390 },
        ],
      },
    ],
  }

  it('横轴是所有序列时间点的并集，而且按时间排', () => {
    // 两个决定各拦一种错法：
    //
    // ① 拿 `series[0].points` 当横轴 → 2024Q1 整个消失，第二条线的
    //    三个点各自错位一格（340 画在 2024Q2 上）。线还是那条线，
    //    只是每点标错了期。
    // ② 取并集但按**出现先后**排 → 得到 2023Q4 · 2024Q2 · 2024Q3 · 2024Q1，
    //    线来回折，而折出来的"趋势"是排版产物。
    //
    // 所以这里钉的是完整的时间序，不是"并集"本身。
    const option = lineOption(LINE)
    const xAxis = option?.xAxis as { data: string[] }
    expect(xAxis.data).toEqual(['2023Q4', '2024Q1', '2024Q2', '2024Q3'])
  })

  it('缺的那个时间点补 null，不补 0', () => {
    // 补 0 的话图上会多一个"2024Q1 充电桩数为 0"的点——那是编的，
    // 而且它看起来像一次暴跌。`null` 让 ECharts 断线，缺口至少诚实。
    const series = lineOption(LINE)?.series as Array<{ data: Array<number | null> }>
    expect(series[0]?.data).toEqual([12, null, 18, 21])
    expect(series[1]?.data).toEqual([300, 340, 390, null])
  })

  it('每条系列的取值个数都等于横轴长度', () => {
    // 个数对不上时 ECharts 不报错，它按顺序配——于是整条线错位。
    const option = lineOption(LINE)
    const xAxis = option?.xAxis as { data: string[] }
    const series = option?.series as Array<{ data: unknown[] }>
    for (const item of series) expect(item.data).toHaveLength(xAxis.data.length)
  })

  it('单位拼进系列名，因为两条线的量纲不同', () => {
    // "充电桩数"和"用户数"画在同一张图上，不写单位的话读者会把
    // 300 和 12 当成同一件事的两个量级。
    const series = lineOption(LINE)?.series as Array<{ name: string }>
    expect(series.map((item) => item.name)).toEqual(['充电桩数（万台）', '用户数（万人）'])
  })

  it('yAxis 不给上限', () => {
    // 趋势的量纲是未知的（条数、万元、百分比都可能），写死上限会截掉超出部分。
    const yAxis = lineOption(LINE)?.yAxis as Record<string, unknown>
    expect(yAxis).not.toHaveProperty('max')
  })

  it('时间标签排不出序时不画，而不是按出现顺序画', () => {
    // `orderPeriods` 认不出全部标签时返回原序。退回去画的话，得到的是一条
    // **时间轴乱掉的趋势线**，而它和正常的图看不出区别——读者会拿它当趋势读。
    const mixed: LineSpec = {
      series: [
        {
          name: 'n',
          unit: '',
          points: [
            { period: '2024-03', value: 1 },
            { period: '近期', value: 2 },
          ],
        },
      ],
    }
    expect(lineOption(mixed)).toBeNull()
  })

  it('series 里缺 points 的那条被跳过，其余的照画', () => {
    // `chartOption` 只守卫了 `series` 是数组，没守卫每个元素有 `points`。
    // 一条残序列不该让整张图消失——但也**不能**让它变成一条全 0 的线。
    const partial = {
      series: [
        { name: '好的', unit: '', points: [{ period: '2024-01', value: 5 }] },
        { name: '残的', unit: '' },
      ],
    } as unknown as LineSpec
    const series = lineOption(partial)?.series as Array<{ name: string }>
    expect(series.map((item) => item.name)).toEqual(['好的'])
  })
})

describe('按 kind 分派', () => {
  it('四种认识的图各自走到对应的构造函数', () => {
    const bar = chartOption({ chartId: 'c1', kind: 'bar', title: 't', evidenceIds: [], spec: BAR })
    const radar = chartOption({ chartId: 'c2', kind: 'radar', title: 't', evidenceIds: [], spec: RADAR })
    const pie = chartOption({ chartId: 'c3', kind: 'pie', title: 't', evidenceIds: [], spec: PIE })
    const line = chartOption({
      chartId: 'c4',
      kind: 'line',
      title: 't',
      evidenceIds: [],
      // 时间标签要写成**认得出的格式**：折线在那个分支里还会
      // 要求排得出时间序，写 `'p'` 会得到 `null`——那不是"没这个分支"，
      // 是这条用例的数据不对。
      spec: { series: [{ name: 'n', unit: '', points: [{ period: '2024-01', value: 1 }] }] },
    })

    expect(bar?.series).toEqual(barOption(BAR).series)
    expect(radar?.radar).toEqual(radarOption(RADAR).radar)
    expect(pie?.tooltip).toEqual(pieOption(PIE).tooltip)
    // 折线那条曾经**根本没有这个分支**，而 `charts._trend_chart` 从第一天
    // 起就发 `kind: "line"`——趋势图在页面上一直是"这一版还不认识这种图"。
    expect(line).not.toBeNull()
    expect(line?.series).toHaveLength(1)
  })

  it('不认识的 kind 返回 null，不抛错', () => {
    // 抛错的话，后端新增一种图会让**历史上所有带它的报告**整页白屏。
    expect(chartOption(foreignChart('sankey', { links: [] }))).toBeNull()
  })

  it('spec 形状不对时返回 null，不抛错', () => {
    // 这是第二道闸。类型只在编译期成立，而这是从 JSON 反序列化出来的
    // 数据——编译器对它一无所知。少了这一层，`series.map` 会当场抛，
    // 异常冒泡到 React 的渲染阶段，整页白屏。
    expect(chartOption(foreignChart('bar', { categories: ['x'] }))).toBeNull()
    expect(chartOption(foreignChart('radar', { series: [] }))).toBeNull()
    expect(chartOption(foreignChart('pie', { series: [] }))).toBeNull()
    expect(chartOption(foreignChart('line', { points: [] }))).toBeNull()
  })

  it('spec 不是对象时也返回 null', () => {
    // `typeof null === 'object'` 这个坑写在 `hasSeries` 里，
    // 但 `chart.kind === 'pie'` 那条分支没走 `hasSeries`——
    // 它直接读 `spec.data`，所以 `null` 与字符串都会当场抛。
    expect(chartOption(foreignChart('pie', null))).toBeNull()
    expect(chartOption(foreignChart('bar', '不是对象'))).toBeNull()
    expect(chartOption(foreignChart('radar', undefined))).toBeNull()
  })
})
