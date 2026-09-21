/**
 * 决策回放：把一次任务的 span 摊成一条能拖的时间线，并回答
 * **"游标停在这一步时，哪些证据已经到手里了"**。
 *
 * 这一层是纯函数，因为它要回答的问题都能在没有浏览器的情况下判对错：
 * 顺序对不对、边界在哪、某一步该显示几条。
 *
 * 回放的两根支柱，以及它们各自被什么限制
 * ====================================
 *
 * **一、滑杆走 span，不走证据。** 249 条 span 的 `startedAt`/`endedAt`
 * 是毫秒级的（实测 `TK-1d1ff1f7090b`：14:47:26 → 14:50:30，249 个真实台阶），
 * 所以"拖到第 137 步"是有意义的一句话。
 *
 * 而**证据的捕获时间没有这个分辨率**：`evidences.capturedAt` 是按
 * **采集轮次**盖的章，不是逐条记的。实测两个真任务：
 *
 * | 任务 | 不同的 `capturedAt` 取值 | 分布 |
 * |---|---|---|
 * | `TK-1d1ff1f7090b` | **2 个** | 14:48:42 ×84、14:49:42 ×24 |
 * | `TK-aea39ac70af5` | **2 个** | 21:09:55 ×188、21:11:38 ×24 |
 *
 * 所以"按时间逐条冒出来"这件事**做不到**：那样滑杆走过 45% 的行程
 * 面板都是空的，然后一次性弹出 84 条。那不是回放，是三段跳。
 *
 * **二、span 与证据按域名连，不按时间连。** `fetch` span 的
 * `detail.domain` 与证据 URL 的主机名**对得上**——实测该任务 100 条
 * `fetch` span、63 个主机、108 条证据，**100/100 命中**。
 *
 * 于是证据面板的职责是"**跟着当前 span 高亮/过滤**"，不是"随时间累积"。
 * 这个差别必须在页面上说清楚，否则用户会以为滑杆坏了。
 *
 * 为什么没有引用级联动
 * ------------------
 * `span.detail` 里**没有任何 evidence id**：`search` 只有 `{sites, hits}`，
 * `fetch` 只有 `{domain, chars, boilerplate, degraded}`，`llm` 只有 `{tier}`。
 * 所以"这一条 span 产出的是哪几条证据"**在数据里没有答案**，
 * 只能靠域名+时间近似。引用级联动落在报告那一侧（`citations`），
 * 不在这条时间线上——那里有真正的 id 对应关系。
 */
import type { SpanNode, TraceSpan } from '../types/domain'

/** 时间线上的一个台阶。`at` 用毫秒时间戳，方便和滑杆的数值直接对应。 */
export interface ReplayStep {
  /** 在这条时间线上的位置，从 0 起。**也是滑杆的取值**。 */
  index: number
  span: TraceSpan
  /** 这一步发生在这个时刻。取 `endedAt`——span 是**结束**时才成为事实的 */
  at: number
  /** 相对时间线起点，毫秒。`≥ 0`，解析不出时间的记 0 */
  offsetMs: number
  /** 从起点到这一步累计了多少毫秒（`spanDurationMs` 的前缀和） */
  cumulativeMs: number
  /** 这一步的祖先链，从外到内。当前数据里恒为空（见 `SpanNode`） */
  ancestors: TraceSpan[]
}

export interface ReplayTimeline {
  steps: ReplayStep[]
  /** 起点毫秒时间戳。没有可解析的 span 时为 0 */
  startMs: number
  /** 终点毫秒时间戳（最后一步的 `at`） */
  endMs: number
  /** 整个时间线跨了多少毫秒 */
  totalMs: number
}

/**
 * `SP-00007` → 7。取不出数字的排在最前，与后端
 * `Tracer._sequence` 逐字相同。
 *
 * 为什么要有这个兜底：`spanId` 是零填充的（5 位），到 `SP-100000`
 * 就变成 6 位，那时按字符串排会得到 `SP-100000 < SP-09999`，
 * 顺序从此错位。而错位的表现只是"回放走到一半跳了一下"。
 * 现在库里的量级远到不了，但这个函数不该有一个"数据量大到某个数就悄悄错"的性质。
 */
export function spanSequence(spanId: string): number {
  const suffix = spanId.slice(3)
  if (!/^\d+$/.test(suffix)) return 0
  return Number(suffix)
}

/** 解析 ISO 时间戳。解析不出给 `null`，调用方决定怎么兜。 */
export function parseTime(value: string): number | null {
  if (!value) return null
  const ms = new Date(value).getTime()
  return Number.isNaN(ms) ? null : ms
}

/**
 * 把树摊成**开始顺序**的一串节点，连祖先链一起带上。
 *
 * 为什么必须重排、而不能用深度优先的遍历顺序：接口给的树是深度优先的，
 * 而**父 span 一定晚于子 span 完成**（`_finish` 在子之后调），
 * 所以两种顺序在嵌套时不一样。
 *
 * **排序键用 `spanId` 编号，`startedAt` 只做并列时的兜底。**
 * 这一点是改过的，原因值得留着：
 *
 * 后端 `Tracer.spans()` 的排序键是 `_sequence(span_id)`，
 * 而它的 docstring 说明"按开始顺序返回，与 `span_id` 的编号一致"——
 * 这个等式成立是因为**编号是在 span 开始时发的**。
 * 也就是说后端认为**编号就是开始顺序**，它根本没拿时间戳排过序。
 *
 * 第一版前端用的是 `startedAt`，与后端平时给出一样的结果，
 * 但两者**在结构上可以不一致**：`span()` 里编号在锁内发、
 * 时间戳在锁外取，多线程并发开 span 时（搜索与抓取都是
 * `asyncio.to_thread` 扇出的）两道临界区之间那道缝足以让
 * "编号 41 的 `startedAt` 晚于编号 42"。
 * 实测库里 15 个任务的两种排法**当前完全一致**，所以这是个潜伏分歧，
 * 不是正在错的行为——但它是**两个定义**：接口给的数组本来就按编号排好了，
 * trace 页与 `metrics()` 也都用编号那套，只有回放页另立一套。
 * 平时给一样的结果，就没有任何信号告诉你有两套。
 *
 * 所以现在：**编号优先，取不出数字（游离 span，`_sequence` 返回 0）
 * 时用时间戳兜底**。这与后端 `_sequence` 的注释"取不出数字的排在最前"
 * 是同一套规则。
 */
export function flattenSpans(roots: SpanNode[]): Array<{
  span: TraceSpan
  ancestors: TraceSpan[]
}> {
  const out: Array<{ span: TraceSpan; ancestors: TraceSpan[] }> = []
  const walk = (nodes: SpanNode[], ancestors: TraceSpan[]): void => {
    for (const node of nodes) {
      out.push({ span: node, ancestors })
      const children = node.children ?? []
      if (children.length) walk(children, [...ancestors, node])
    }
  }
  walk(roots, [])

  return out.sort((left, right) => {
    const bySequence = spanSequence(left.span.spanId) - spanSequence(right.span.spanId)
    if (bySequence !== 0) return bySequence

    const a = parseTime(left.span.startedAt)
    const b = parseTime(right.span.startedAt)
    if (a !== null && b !== null && a !== b) return a - b
    if (a === null && b !== null) return -1
    if (a !== null && b === null) return 1
    return 0
  })
}

/**
 * 摊成时间线。**步骤顺序完全由 `flattenSpans` 决定（= 编号 = 开始顺序）。**
 *
 * `at` 取 `endedAt` 而不是 `startedAt`：滑杆停在某一步时，读者要看到的是
 * "这一步**已经发生**"，而一个正在跑的 span 还没有结果。
 *
 * 时间戳解析不出来的记成 `startMs`，**而不是丢掉这一条**——
 * 丢一条会让步数和 `summary.spanCount` 对不上，而页面上看不出来。
 * 那一条会停在它**编号该在的位置**上（早期版本会把它挪到最前面，见下）。
 */
export function buildTimeline(roots: SpanNode[]): ReplayTimeline {
  const flat = flattenSpans(roots)
  const starts = flat
    .map((item) => parseTime(item.span.startedAt))
    .filter((value): value is number => value !== null)
  const startMs = starts.length ? Math.min(...starts) : 0

  let cumulative = 0
  const steps: ReplayStep[] = flat.map((item, index) => {
    const at = parseTime(item.span.endedAt) ?? startMs
    cumulative += Math.max(0, item.span.durationMs || 0)
    return {
      index,
      span: item.span,
      at,
      offsetMs: Math.max(0, at - startMs),
      cumulativeMs: cumulative,
      ancestors: item.ancestors,
    }
  })

  // 这里**不再按 `at` 重排**。第一版排了，理由是"滑杆往右拖，`at` 不能倒退"，
  // 但那个重排自己带来两个更糟的后果：
  //
  // 1. **它制造了第三种顺序。** `flattenSpans` 刚按编号排好（= 后端的
  //    "开始顺序"），这里又按**结束**时间排一遍，于是"第几步"又换了个定义。
  //    嵌套时两者必然不同：父 span 先开始、后结束。问题 35 记的就是这件事。
  // 2. **它让 `cumulativeMs` 不再是前缀和。** 那个累加是在 `map` 里按
  //    `flat` 顺序算的，排完序之后"到第 i 步为止的累计"就是假的——
  //    而字段的文档写着它是前缀和。
  //
  // 而"不能倒退"这个顾虑本来就不成立：`replayFrame` 的 `hosts` 是
  // **按步骤下标累加**的（`collectHosts` 从头扫到游标），与 `at` 无关，
  // 所以游标右移时面板只会增、不会减。
  //
  // `at` 因此**允许非单调**：嵌套时父的结束时刻本来就晚于子。
  // 当前真库里 1036 行没有一行有 `parent_id`，所以这个差别还看不出来。
  const endMs = steps.reduce((max, step) => Math.max(max, step.at), startMs)
  return { steps, startMs, endMs, totalMs: Math.max(0, endMs - startMs) }
}

/**
 * 证据按主机名分组。**主机名的归一规则要和后端一致**：
 * 小写、去掉 `www.`（与 `core/evidence/sourcetypes.py` 的
 * `independent_domain` 同一套判据的取向）。
 *
 * 为什么这里敢重写一遍主机名规则，而契约文件里明确说"别在前端用
 * `new URL().hostname` 重写 `independent_domain`"：因为那说的是**计数**
 * （`metrics.independentDomains` 是头条指标，两处算会不一致）。
 * 这里做的是**配对**——把一个 `fetch` span 和它抓到的那几条证据对上，
 * 配错了的后果是"面板里少一条/多一条"，不是"报告里的域名数变了"。
 * 判据仍然是同一套：小写 + 去 `www.`。
 */
export function evidenceHost(url: string): string {
  try {
    const host = new URL(url).hostname.toLowerCase()
    return host.startsWith('www.') ? host.slice(4) : host
  } catch {
    // 相对 URL 或缺协议头的脏数据。退回"取 ? 和 / 之前那一段"，
    // 拿不到就给空串——空串会在配对里自然落空，而不是错误地配到别人身上。
    const trimmed = url.split(/[?#]/)[0] ?? ''
    const afterScheme = trimmed.replace(/^[a-z]+:\/\//i, '')
    const host = (afterScheme.split('/')[0] ?? '').split('@').pop() ?? ''
    const bare = host.split(':')[0] ?? ''
    return bare.toLowerCase().replace(/^www\./, '')
  }
}

/**
 * 一条 span 碰到了哪些主机。
 *
 * 只有 `fetch` span 有 `domain`。`search` span 有 `sites`，
 * 但那是**请求时指定的站点过滤**、不是"抓到了哪些站"，
 * 算进去会让面板在采集刚开始时就显示一堆还没抓的证据。
 */
export function spanHosts(span: TraceSpan): string[] {
  const domain = span.detail?.['domain']
  if (typeof domain !== 'string' || !domain) return []
  return [domain.toLowerCase().replace(/^www\./, '')]
}

export interface ReplayFrame {
  /** 游标所在的这一步。时间线为空时是 `null` */
  step: ReplayStep | null
  /** 到这一步为止，`fetch` span 碰到过的主机（**按首次出现排序**） */
  hosts: string[]
  /** 到这一步为止，这些主机名下已经能看到的证据条数 */
  reached: number
  /** 这一步**新**碰到的主机（高亮用）。第一步之前为空 */
  freshHosts: string[]
}

/**
 * 游标在 `cursor` 这一步时画面该是什么样。
 *
 * **是"到这一步为止"而不是"这一步"**：一条证据可能由好几条 `fetch` span
 * 先后碰到（同一主机的不同页面），而读者的心智模型是"我现在手里有什么"。
 * 这也是为什么 `hosts` 返回的是累计集合。
 *
 * `cursor` 越界时钳到两端，不返回空——`<input type="range">` 在
 * 时间线变化（比如换了一个任务）时会短暂地把值留在旧范围外，
 * 那时返回空画面会让面板闪一下。
 */
export function replayFrame(
  timeline: ReplayTimeline,
  cursor: number,
  evidencesByHost: Map<string, unknown[]>,
): ReplayFrame {
  const { steps } = timeline
  if (!steps.length) return { step: null, hosts: [], reached: 0, freshHosts: [] }

  const clamped = Math.min(Math.max(Math.trunc(cursor), 0), steps.length - 1)
  const previous = new Set<string>()
  if (clamped > 0) {
    for (const host of collectHosts(steps, clamped - 1)) previous.add(host)
  }

  const hosts = collectHosts(steps, clamped)
  let reached = 0
  for (const host of hosts) reached += evidencesByHost.get(host)?.length ?? 0

  return {
    step: steps[clamped] ?? null,
    hosts,
    reached,
    freshHosts: hosts.filter((host) => !previous.has(host)),
  }
}

/** 前 `upTo` 步（含）碰到过的所有主机，按首次出现顺序。 */
function collectHosts(steps: ReplayStep[], upTo: number): string[] {
  const seen: string[] = []
  for (let i = 0; i <= upTo && i < steps.length; i += 1) {
    for (const host of spanHosts(steps[i]!.span)) {
      if (!seen.includes(host)) seen.push(host)
    }
  }
  return seen
}

/**
 * 证据按主机名分组。同一主机下按可信度**降序**：
 * 面板上先给读者看最值得看的那一条，而不是抓到的第一条。
 *
 * 并列时保持原顺序（`sort` 在 V8 上稳定），所以同一份数据两次渲染
 * 的顺序一致——不一致的话，拖滑杆时列表会自己抖。
 */
export function groupEvidencesByHost<T extends { url: string; credibility: number }>(
  evidences: T[],
): Map<string, T[]> {
  const grouped = new Map<string, T[]>()
  for (const evidence of evidences) {
    const host = evidenceHost(evidence.url)
    if (!host) continue
    const bucket = grouped.get(host)
    if (bucket) bucket.push(evidence)
    else grouped.set(host, [evidence])
  }
  for (const bucket of grouped.values()) {
    bucket.sort((left, right) => right.credibility - left.credibility)
  }
  return grouped
}

/**
 * 时间线里**第一条件为真的 `fetch` span** 的位置。
 *
 * 页面上要用它把"还没开始采集"与"采集中"分开：实测
 * `TK-1d1ff1f7090b` 的 249 步里，**第一个 `fetch` span 排在第 117 位**
 * （需求解析、调度、以及最早那批搜索占了前面这一段），滑杆的前 47%
 * 都属于这里。不标出来的话，读者会以为回放坏了。
 *
 * 别把这个数和"第一轮证据盖章之前结束了多少条 span"（那个是 113）混起来——
 * 两个数差 4，问的是两个不同的问题，见 `问题记录.md` 问题 34。
 *
 * 一条 `fetch` 都没有时返回 `null`——那是"这次没采集"，
 * 与"还没轮到采集"是两件事，页面上要说不同的话。
 */
export function firstFetchIndex(timeline: ReplayTimeline): number | null {
  const found = timeline.steps.findIndex((step) => spanHosts(step.span).length > 0)
  return found === -1 ? null : found
}
