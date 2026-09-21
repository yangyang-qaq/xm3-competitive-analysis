/**
 * 展示层格式化。
 *
 * 集中的理由：同一份数据在指标面板、报告正文、tooltip 里出现多次，
 * 三处各写一遍迟早会不一致（比如一处显示 71.3%，一处显示 71%）。
 */
import type { Confidence, ResearchMode, SourceType, SpanKind, StageId } from '../types/domain'

export const MODE_LABEL: Record<ResearchMode, string> = {
  quick: '快速',
  deep: '深度',
  expert: '专家级',
}

export const STAGE_LABEL: Record<StageId, string> = {
  intake: '需求解析',
  orchestrator: '专家调度',
  collect: '证据采集',
  analyze: '分析研判',
  audit: '质量审计',
  rework: '返工闭环',
  write: '报告撰写',
  done: '完成',
}

export const CONFIDENCE_LABEL: Record<Confidence, string> = {
  high: '高置信',
  medium: '中置信',
  low: '低置信',
  unverified: '无证据',
}

/**
 * span 的种类名。
 *
 * 本来它长在 `components/trace/TracePanel.tsx` 里，而回放页也要用同一张表——
 * 于是这里成了第二个定义。放这儿是因为**这两个页面对同一个 span 必须叫同一个名字**：
 * 工作台上写着"模型"、回放页上写着"LLM"，读者会以为是两种东西。
 * 一处定义、两处引用，比两处各自正确要可靠。
 */
export const SPAN_KIND_LABEL: Record<SpanKind, string> = {
  llm: '模型',
  search: '检索',
  fetch: '抓取',
  stage: '阶段',
}

export const SOURCE_TYPE_LABEL: Record<SourceType, string> = {
  official: '官方',
  financial_report: '财报',
  news: '新闻',
  zhihu: '知乎',
  bilibili: 'B站',
  weibo: '微博',
  xiaohongshu: '小红书',
  douyin: '抖音',
  review: '评测',
  web: '网页',
  unknown: '未知',
}

/**
 * `sourceType` 的中文名。**查不到就原样返回。**
 *
 * `SOURCE_TYPE_LABEL` 的键是 `SourceType`（联合类型），而报告里存的
 * `sourceType` 是 `string`——后端 `models.py` 那张表可以在不改前端的情况下
 * 多一个取值。查表时要按联合类型下标，就会写成
 * `SOURCE_TYPE_LABEL[value as SourceType]`，而那个断言在新增取值时
 * 不报错、只返回 `undefined`，页面上就是一个**空白标签**——
 * 看起来像"这条证据没有来源类型"。
 *
 * 退回原样显示 `bilibili` 至少是诚实的：读者知道这是没翻译的键，
 * 而不是没有值。
 */
export function sourceTypeLabel(value: string): string {
  return (SOURCE_TYPE_LABEL as Record<string, string | undefined>)[value] ?? value
}

/**
 * `mode` 的中文名。查不到就原样返回，理由同 `sourceTypeLabel`。
 *
 * 报告里存的是 `mode: {key, label}`——**`label` 是后端写好的**，
 * 能用就用它，这个函数是给它缺席时的兜底（旧报告里 `mode` 可能只有 `key`）。
 * 所以调用处应该写 `body.mode?.label || modeLabel(body.mode?.key ?? '')`。
 */
export function modeLabel(value: string): string {
  return (MODE_LABEL as Record<string, string | undefined>)[value] ?? value
}

export function formatPercent(value: number, digits = 0): string {
  return `${(value * 100).toFixed(digits)}%`
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  const total = Math.round(seconds)
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return rest === 0 ? `${minutes} 分` : `${minutes} 分 ${rest} 秒`
}

/**
 * 成本统一用美元展示。
 *
 * 各家 provider 按自己的货币计价（DeepSeek 是人民币），在适配层就归一到 USD，
 * 这样跨 provider 的成本才可比。换算汇率写在 provider 的定价表里，是可审计的常量。
 */
export function formatCost(usd: number): string {
  if (!Number.isFinite(usd) || usd <= 0) return '$0'
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  return `$${usd.toFixed(2)}`
}

export function formatInt(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(Math.round(value))
}

export function formatDateTime(iso: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

const RELATIVE_UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
]

export function formatRelative(iso: string): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const deltaSeconds = (then - Date.now()) / 1000
  const formatter = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' })
  for (const [unit, seconds] of RELATIVE_UNITS) {
    if (Math.abs(deltaSeconds) >= seconds) {
      return formatter.format(Math.round(deltaSeconds / seconds), unit)
    }
  }
  return '刚刚'
}

/** 可信度分档。阈值与后端 evidence/credibility.py 的语义保持一致。 */
export function credibilityTier(score: number): 'high' | 'medium' | 'low' {
  if (score >= 70) return 'high'
  if (score >= 45) return 'medium'
  return 'low'
}

/**
 * 可信度徽章的配色。**跟着 `credibilityTier` 走，不另立一套阈值。**
 *
 * 证据流与回放页都要给同一个分数上同一种颜色——两处各写一张表的话，
 * 同一个"82 分"在证据流里是绿的、在回放页里是黄的，而两边都不会报错。
 * 类型写成 `Record<ReturnType<typeof credibilityTier>, string>`：
 * 档位增删时这里会跟着报错，`Record<string, string>` 不会。
 */
export const CREDIBILITY_TIER_STYLE: Record<ReturnType<typeof credibilityTier>, string> = {
  high: 'bg-ok/15 text-ok',
  medium: 'bg-warn/15 text-warn',
  low: 'bg-danger/15 text-danger',
}

/**
 * 专家层级徽章的配色，对着 `--color-l1/l2/l3` 三个令牌。
 *
 * 键是 `string` 而不是 `'L1'|'L2'|'L3'`：后端的 `level` 是自由字符串
 * （名册是生成出来的，多一个层级不该让前端编译不过），取不到时调用方
 * 用 `??` 退到灰底。这一点与上面那张可信度表**故意不同**——那张表的档位
 * 由前端自己算，所以能收窄成联合类型；这张的值来自后端，收窄了就是
 * 假装我们知道后端只会发这三种。
 *
 * 两处用它：专家公会（卡片）与专家详情（页头）。**一处定义、两处引用**：
 * 分头写的话，同一个人在两张页面上会是两种颜色。
 */
export const EXPERT_LEVEL_BADGE: Record<string, string> = {
  L3: 'bg-l3/12 text-l3',
  L2: 'bg-l2/12 text-l2',
  L1: 'bg-l1/12 text-l1',
}
