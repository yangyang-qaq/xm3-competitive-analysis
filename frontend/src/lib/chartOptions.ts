/**
 * 图表规格 → ECharts option。**纯函数，不碰 DOM。**
 *
 * 为什么单独一个模块而不是写在组件里
 * --------------------------------
 * 组件里那部分（`echarts.init` / `resize` / `dispose`）在 jsdom 里跑不起来
 * ——它要 canvas。混在一起，整套图表逻辑就都测不了，只能靠肉眼看。
 * 拆开之后，"哪种 spec 画成哪种图"是纯数据变换，能被穷举地测；
 * 组件只剩生命周期，那部分由报告页的渲染测试（mock 掉本模块）覆盖。
 *
 * 颜色从哪儿来
 * ------------
 * 数据系列的颜色**由后端给出**（`spec.series[].color`），不由前端选——
 * 同一份报告在页面、导出、图表里必须是同一个颜色，否则读者没法
 * 把图例和表格对上。轴与文字的灰色则取自设计令牌（`index.css`），
 * 不在这里写死十六进制：写死的话，换主题时图表的文字颜色不会跟着变。
 *
 * 返回值为什么是 `EChartsOption` 而不是 `Record<string, unknown>`
 * --------------------------------------------------------------
 * 因为 ECharts **不认识拼错的键**：写成 `axisLable` 不报错、不警告，
 * 那张图只是少了个轴标签。用 `Record<string, unknown>` 的话这个手误
 * 一路编译通过，要到有人肉眼看图才发现——而"图不对"最容易的解释
 * 是"这个维度没数据"，于是手误会冒充成数据问题。
 *
 * 用真类型则把它变成一次编译失败。`EChartsOption` 的联合类型确实复杂，
 * 但它在这里只当**返回值**用，不参与任何推导，代价就是偶尔要给
 * `legendStyle()` 这种中间件写一个具体的返回形状。
 */
import type { EChartsOption } from 'echarts'

import { orderPeriods } from './period'
import type {
  BarSpec,
  LineSpec,
  PieSpec,
  RadarSpec,
  ReportChart,
} from '../types/report'

/**
 * 读一个 CSS 变量。
 *
 * 拿不到时用兜底值而不是抛错：图表是在 `useEffect` 里画的，抛错会
 * 让整个报告页白屏——而"轴的颜色没取到"是一件该降级处理的小事。
 * （jsdom 里 `getComputedStyle` 拿不到 `@theme` 定义的变量，测试
 * 走到这里就会用兜底值。）
 */
export function themeColor(name: string, fallback: string): string {
  if (typeof window === 'undefined' || typeof document === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

function axisTextStyle(): { color: string; fontSize: number } {
  return { color: themeColor('--color-fg-muted', '#6d675b'), fontSize: 11 }
}

function legendStyle(): { textStyle: { color: string; fontSize: number } } {
  return { textStyle: { color: themeColor('--color-fg-muted', '#6d675b'), fontSize: 11 } }
}

/** 柱状图：功能矩阵的"按维度看各品牌"。 */
export function barOption(spec: BarSpec): EChartsOption {
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0, type: 'scroll', ...legendStyle() },
    grid: { left: 4, right: 8, bottom: 4, top: 30, containLabel: true },
    xAxis: {
      type: 'category',
      // 维度名很长（七八个字），横着排会互相压住。斜排是这类中文
      // 分类轴上唯一能看的排法。
      data: spec.categories,
      axisLabel: { ...axisTextStyle(), rotate: 30 },
    },
    // Y 轴：**后端给了量表就用后端给的**，没给才自适应。
    //
    // 能力矩阵那根柱状图的 spec 里带着 `yAxis: {min: 0, max: 5, name: "评分"}`
    // ——后端在说"这是一个 1–5 分的量表"。前端不用它、让 ECharts 自适应的话，
    // 一根 4.2 和一根 4.4 的柱子会看起来差一倍，而**图的标题写着"评分"**，
    // 读者会以为那两倍是真的。量表图的刻度不是排版，是口径。
    //
    // 不给 `max` 是留给"别的柱状图"的：那种图的上限由数据决定，写死会把
    // 超出的部分截掉，而截掉的柱子看起来只是"这一项低"，不像被裁过。
    // 但 `min: 0` 无论哪种都给——不从零起的柱状图会让人误读倍数关系。
    yAxis: {
      type: 'value',
      min: spec.yAxis?.min ?? 0,
      ...(spec.yAxis?.max !== undefined ? { max: spec.yAxis.max } : {}),
      ...(spec.yAxis?.name ? { name: spec.yAxis.name, nameTextStyle: axisTextStyle() } : {}),
      axisLabel: axisTextStyle(),
      splitLine: { lineStyle: { color: themeColor('--color-line', '#e7e0d2') } },
    },
    series: spec.series.map((item) => ({
      name: item.brand,
      type: 'bar',
      data: item.values,
      itemStyle: item.color ? { color: item.color } : undefined,
      barMaxWidth: 22,
    })),
  }
}

/** 雷达图：功能矩阵的"按品牌看各维度"。 */
export function radarOption(spec: RadarSpec): EChartsOption {
  return {
    tooltip: {},
    legend: { top: 0, type: 'scroll', ...legendStyle() },
    radar: {
      indicator: spec.indicators,
      radius: '62%',
      center: ['50%', '56%'],
      axisName: axisTextStyle(),
      splitLine: { lineStyle: { color: themeColor('--color-line', '#e7e0d2') } },
      splitArea: { show: false },
      axisLine: { lineStyle: { color: themeColor('--color-line', '#e7e0d2') } },
    },
    series: [
      {
        type: 'radar',
        data: spec.series.map((item) => ({
          name: item.brand,
          value: item.values,
          itemStyle: item.color ? { color: item.color } : undefined,
        })),
      },
    ],
  }
}

/**
 * 饼图：市场份额、来源构成。
 *
 * tooltip 用 `{c}%` 而不是 `{d}%`——**这不是风格问题**。
 * `{d}` 是 ECharts 自己归一化出来的占比（各片 ÷ 总和），
 * 而后端给的 `share` 本来就是百分比（41.0 表示 41%）。
 * 两者在有重复品牌、或数据不构成一个完整整体时**不相等**：
 * 库里那份报告有"特来电 41%"与"特来电 27.4%"两条（来自两个不同口径），
 * 用 `{d}` 会显示成 47% 和 31%——一个正文和证据里都不存在的数。
 *
 * 口径（`basis`）不在这里显示，由卡片上的表格列出来：
 * 一张"合计不等于 100%"的饼，把每一片的口径写出来才是诚实的做法。
 */
export function pieOption(spec: PieSpec): EChartsOption {
  return {
    tooltip: { trigger: 'item', formatter: '{b}：{c}%' },
    legend: { bottom: 0, type: 'scroll', ...legendStyle() },
    series: [
      {
        type: 'pie',
        radius: ['42%', '66%'],
        center: ['50%', '44%'],
        label: { color: themeColor('--color-fg-muted', '#6d675b'), fontSize: 11 },
        data: spec.data.map((item) => ({ name: item.brand, value: item.share })),
      },
    ],
  }
}

/**
 * 折线图：趋势序列。**排不出时间序就返回 `null`（不画）。**
 *
 * 横轴是各序列时间点的**并集**，而且要按时间排
 * ------------------------------------------
 * 每条线自带自己的 `points`（见 `LineSpec` 的注释），所以不存在一个现成的
 * `categories`。最省事的写法是拿第一条线的 `points` 当横轴——**那是错的**：
 * 第二条线如果多一个时间点，它就被画丢了；如果少一个，它整体**错位一格**，
 * 而错位的折线看起来完全正常（线还是那条线，只是每个点标错了期）。
 *
 * 但"取并集"还不够。并集本身没有顺序，而**折线图的横轴顺序就是这张图的
 * 主张**——它说"这是时间顺序"。按出现先后排的话，一份普通的数据
 * （一条线是 2023Q4/2024Q2/2024Q3、另一条多一个 2024Q1）就能排出
 * `2023Q4 · 2024Q2 · 2024Q3 · 2024Q1`：线来回折，而折出来的"趋势"
 * 是排版产物。所以这里调 `orderPeriods` 按时间排。
 *
 * 排不了就不画，而不是退回原序
 * --------------------------
 * `orderPeriods` 认不出全部标签时返回 `ordered: false`。这一刻**退回
 * 原序就等于画一条时间轴乱掉的趋势线**，而它看起来和正常的图没有区别——
 * 读者会拿它当趋势读。这和矩阵转置是同一类错误：形状对、读数错、
 * 肉眼看不出来。所以返回 `null`，由卡片上说一句"这张图的数据排不出时间序"。
 *
 * 某个序列没有某个时间点时补 `null` 而不是 `0`
 * ------------------------------------------
 * ECharts 遇到 `null` 会**断线**（留一个缺口）。补 `0` 的话图上会多出一个
 * "这个季度是 0"的数据点——那是我们编的，而且它看起来像一次暴跌。
 * 缺口至少诚实：读者看到断线会知道这里没数据。
 */
export function lineOption(spec: LineSpec): EChartsOption | null {
  const usable = spec.series.filter((item) => Array.isArray(item.points))
  if (usable.length === 0) return null

  // `Array.from(new Set(...))` 而不是 `[...new Set(...)]`：目标编译档位下
  // 展开 Set 需要 `downlevelIteration`，这里不为此改编译配置。
  const union = Array.from(
    new Set(usable.flatMap((item) => item.points.map((point) => point.period))),
  )
  const { periods, ordered } = orderPeriods(union)
  if (!ordered) return null

  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0, type: 'scroll', ...legendStyle() },
    grid: { left: 4, right: 12, bottom: 4, top: 30, containLabel: true },
    xAxis: { type: 'category', data: periods, axisLabel: { ...axisTextStyle(), rotate: 30 } },
    yAxis: {
      type: 'value',
      // 同柱状图：**不给 max**。趋势的量纲是未知的（条数、万元、百分比都可能），
      // 写死上限会截掉超出部分。
      axisLabel: axisTextStyle(),
      splitLine: { lineStyle: { color: themeColor('--color-line', '#e7e0d2') } },
    },
    series: usable.map((item) => {
      const byPeriod = new Map(item.points.map((point) => [point.period, point.value]))
      return {
        name: item.unit ? `${item.name}（${item.unit}）` : item.name,
        type: 'line' as const,
        // `?? null` 而不是 `?? 0`，理由见上。
        data: periods.map((period) => byPeriod.get(period) ?? null),
        connectNulls: false,
        smooth: false,
      }
    }),
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** `spec[key]` 是不是一个数组。**先判 `spec` 本身是不是对象**——
 *  `typeof null === 'object'`，漏掉这一条的话 `null.data` 会当场抛。
 *  取键名而不是写死属性，是为了让四条分支共用一个守卫：
 *  四条里少写一条 `spec` 判空，就会漏掉一种残报告的形态。 */
function hasArray(spec: unknown, key: string): boolean {
  return isObject(spec) && Array.isArray(spec[key])
}

/**
 * 按 `kind` 分派。**画不出来就返回 `null`**，由调用方显示一句说明——
 * 而不是抛错。
 *
 * 两条会走到 `null` 的路，理由不同但结论一样（**一份报告不该因为
 * 一张图而打不开**）：
 *
 * 1. `kind` 是本版页面不认识的（后端新加了一种图）。抛错的话，
 *    新增一个图表类型会让历史上所有带它的报告整页白屏。
 * 2. `spec` 的形状不对（缺 `series` / `data`，或者 `spec` 本身不是对象）。
 *    `barOption` 里那句 `.map` 会当场抛，而这个异常会冒泡到
 *    React 的渲染阶段。
 *
 * 形状这一条是第二道闸：类型只在编译期成立，而这是**从 JSON 反序列化
 * 出来的数据**，编译器对它一无所知。
 *
 * 这里每一个守卫都走 `hasArray(chart.spec, ...)` 而不是直接
 * `chart.spec.data`——**不要写成后者**。`chart.spec` 在这里的静态类型
 * 已经是 `PieSpec`，所以 `Array.isArray(chart.spec.data)` 编译得过；
 * 但那份类型是从判别联合里推出来的，运行时 `spec` 完全可以是 `null`。
 * 第一版就是那么写的，`spec: null` 抛 `Cannot read properties of null`，
 * 由一条测试抓到。
 */
export function chartOption(chart: ReportChart): EChartsOption | null {
  if (chart.kind === 'bar' && hasArray(chart.spec, 'series')) return barOption(chart.spec)
  if (chart.kind === 'radar' && hasArray(chart.spec, 'indicators') && hasArray(chart.spec, 'series')) {
    return radarOption(chart.spec)
  }
  if (chart.kind === 'pie' && hasArray(chart.spec, 'data')) return pieOption(chart.spec)
  if (chart.kind === 'line' && hasArray(chart.spec, 'series')) return lineOption(chart.spec)
  return null
}
