/**
 * 时间标签排序。**只为折线图的横轴服务。**
 *
 * 为什么需要它
 * ----------
 * 折线图里每条序列自带自己的时间点，所以横轴是各序列时间点的**并集**。
 * 并集本身没有顺序，而折线图的横轴顺序**就是这张图的主张**——
 * 它说"这是时间顺序"。按出现先后排的话，一份普通的数据就能排出
 * `2023Q4 · 2024Q2 · 2024Q3 · 2024Q1` 这样的轴：线来回折，
 * 而折出来的"趋势"是纯粹的排版产物。
 *
 * （这不是假想：这组用例就是照着测试里那份构造数据排出来的。）
 *
 * 解析不了时**不猜**
 * ---------------
 * 返回 `null` 表示"这个标签的时间位置无法确定"。调用方据此决定：
 * 全都解析得出 → 按时间排；**只有一部分解析得出 → 不画图**
 * （见 `chartOptions.lineOption`）。把一半能排序、一半不能的标签混着排，
 * 会得到一条看着有序、实际错乱的轴——比不排更坏。
 *
 * 认哪几种写法
 * ----------
 * 只认**无歧义**的：`2024-01` / `2024/01` / `2024年1月` / `2024Q1` /
 * `2024年第1季度` / `2024`。刻意不认"上个月""近期""中期"这类相对说法——
 * 它们相对的是**写作时间**，而写作时间在报告里并不总是同一件事
 * （证据的发布时间、报告生成时间、读者的当前时间，三个都可能）。
 * 把这些映射成某个绝对时间就等于替报告编了一个时间轴。
 */

/** 归一化的时间键：`年 * 100 + 月`。同一年内可比，跨年也可比。 */
export type PeriodKey = number

const PATTERNS: Array<{ re: RegExp; toKey: (match: RegExpMatchArray) => number | null }> = [
  // 2024-01 / 2024/01 / 2024.01
  { re: /^(\d{4})[-/.](\d{1,2})$/, toKey: (m) => monthKey(m, 1, 2) },
  // 2024年1月 / 2024年01月
  { re: /^(\d{4})\s*年\s*(\d{1,2})\s*月$/, toKey: (m) => monthKey(m, 1, 2) },
  // 2024Q1 / 2024 Q1 / 2024q1
  { re: /^(\d{4})\s*[Qq]\s*([1-4])$/, toKey: (m) => quarterKey(m, 1, 2) },
  // 2024年第1季度 / 2024年第1季
  { re: /^(\d{4})\s*年?\s*第\s*([1-4])\s*季(?:度)?$/, toKey: (m) => quarterKey(m, 1, 2) },
  // 2024
  { re: /^(\d{4})$/, toKey: (m) => yearKey(m, 1) },
]

function numberAt(match: RegExpMatchArray, index: number): number | null {
  const raw = match[index]
  if (typeof raw !== 'string') return null
  const value = Number.parseInt(raw, 10)
  return Number.isFinite(value) ? value : null
}

function yearKey(match: RegExpMatchArray, yearIndex: number): number | null {
  const year = numberAt(match, yearIndex)
  return year === null ? null : year * 100
}

function monthKey(match: RegExpMatchArray, yearIndex: number, monthIndex: number): number | null {
  const year = numberAt(match, yearIndex)
  const month = numberAt(match, monthIndex)
  if (year === null || month === null) return null
  // 13 月、0 月不存在。放过去的话它们会排到一个不存在的日期上，
  // 而那个位置看起来完全正常——一个"13 月"被排在 12 月之后、
  // 次年 1 月之前，读者不会觉得哪里不对。
  if (month < 1 || month > 12) return null
  return year * 100 + month
}

function quarterKey(match: RegExpMatchArray, yearIndex: number, quarterIndex: number): number | null {
  const year = numberAt(match, yearIndex)
  const quarter = numberAt(match, quarterIndex)
  if (year === null || quarter === null) return null
  // 季度折成它的第一个月，于是季度和月份可以排在同一条轴上：
  // 2024Q1(2024*100+1) < 2024年4月(2024*100+4)。
  return year * 100 + (quarter - 1) * 3 + 1
}

/** 一个时间标签 → 可比较的键。认不出来返回 `null`。 */
export function periodKey(period: string): PeriodKey | null {
  const text = period.trim()
  if (!text) return null
  for (const { re, toKey } of PATTERNS) {
    const match = text.match(re)
    if (match) return toKey(match)
  }
  return null
}

/**
 * 把一批时间标签排成时间序。**排不了就原样返回并说明原因。**
 *
 * 返回 `{ periods, ordered }` 而不是单给数组：调用方要能分清
 * "这是时间序"和"这只是原样搬过来的"，因为后者不能拿去画折线。
 *
 * `ordered: false` 的两种情形都返回**原序**，因为原序毕竟是个确定的顺序，
 * 而"半排过的顺序"不是：
 * - 有标签认不出来（空字符串、`近期`、`H1` 之类）。
 * - 一部分认得出、一部分认不出。**这是最危险的一种**：能排的那部分
 *   排好了，不能排的原封不动，于是整条轴看着有序、实际错乱。
 */
export function orderPeriods(periods: string[]): { periods: string[]; ordered: boolean } {
  if (periods.length === 0) return { periods, ordered: true }

  const keys = periods.map(periodKey)
  if (keys.some((key) => key === null)) return { periods, ordered: false }

  const indexed = periods.map((period, index) => ({ period, key: keys[index] ?? 0, index }))
  // 键相同时用原始下标兜底，保证排序**稳定**——
  // 不稳定排序会让同一份数据两次渲染出不同的轴顺序。
  indexed.sort((left, right) => left.key - right.key || left.index - right.index)
  return { periods: indexed.map((item) => item.period), ordered: true }
}
