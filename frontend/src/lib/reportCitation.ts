/**
 * 正文角标：把 `[证据: EV-x]` 拆成能点的片段。
 *
 * 编号从哪来
 * ---------
 * **优先读报告里存的那份**（`body.citations`，后端 `assemble` 算的）。
 * 报告页是第二个渲染器——导出 Markdown 是第一个——两个渲染器各算一遍
 * 编号，就得保证遍历顺序、容错、参与编号的块永远一致，而没有东西守着。
 * 错一位的编号看起来完全正常，读者只会以为自己数错了。
 *
 * 退回现算的那条路**不是冗余**
 * --------------------------
 * 库里早于这次改动的那些报告没有 `citations` 键。退回现算用的规则与
 * 后端 `build_citations` 逐字相同（只扫正文、按首次出现、未知 id 不占号），
 * 所以对它们算出来的编号与导出里印出来的是同一个数。
 *
 * 为什么未知 id 要显示成 `[?]` 而不是悄悄去掉
 * -----------------------------------------
 * 导出那边已经这么做了，理由一样：**看得见的坏比看不见的坏好。**
 * 一个被吃掉的角标会让读者以为这句话不需要证据，而实际上它曾经引用过
 * 一条（可能已被删除的）证据。报告页上 `[?]` 是可点的，点开会说
 * "这条引用在证据表里找不到"。
 */
import type { ReportBody, ReportEvidence } from '../types/report'

/**
 * 正文里的引用标记。**与后端 `report/citation_index.py` 的 `MARKER`
 * 是同一条规则**，改一处就要改另一处。
 *
 * 容错到全角冒号与多余空白：这些标记是模型写的，而渲染器不该因为一个
 * 全角冒号就把角标漏成一片死链接。
 *
 * ⚠️ 带 `g` 标志的正则是有状态的（`lastIndex`）。这里只用 `matchAll`
 * 与 `replace`：前者会克隆正则，后者用完即重置——都不用调用方记得
 * 手动归零。**别把 `MARKER.test(...)` 加进来**，那才是会踩到的那个用法。
 */
export const MARKER = /\[证据[:：]\s*([^\]\s]+)\s*\]/g

export interface TextSegment {
  kind: 'text'
  text: string
}

export interface CitationSegment {
  kind: 'citation'
  /** 原文里的那一整段标记 */
  text: string
  evidenceId: string
  /** 在报告里的编号。`null` = 这条引用在证据表里找不到（显示成 `[?]`） */
  number: number | null
}

export type Segment = TextSegment | CitationSegment

/**
 * 取一次匹配里的证据 id。取不到返回 `null`，调用方跳过这一处。
 *
 * `MARKER` 的捕获组是 `([^\]\s]+)`——不是 `*`，所以匹配上了就一定有值。
 * 但 `noUncheckedIndexedAccess`（见 `tsconfig.app.json`）让 `match[1]` 的
 * 类型是 `string | undefined`：编译器不知道正则的性质。
 *
 * 这里**没有写 `?? ''`**。空 id 会一路走到 `unresolvedCitations`，
 * 于是页面说"有 1 处引用点不开"——而真相是这段代码错了，
 * 不是报告错了。让假的坏引用去冒充真的坏引用，比崩掉更难查。
 *
 * 真正取不到时**跳过这一处匹配**：标记留在原文里显示成 `[证据: ...]`，
 * 难看，但读者看到的是一个没被渲染的标记，而不是一个指错地方的编号。
 */
function capturedId(match: RegExpMatchArray): string | null {
  const value = match[1]
  return typeof value === 'string' && value.length > 0 ? value : null
}

/** 证据表里所有的 id。 */
export function evidenceIds(body: ReportBody): string[] {
  return (body.evidences ?? []).map((item) => item.evidenceId).filter(Boolean)
}

/** 正文里被引用到的证据 id，按首次出现顺序。**只扫正文**。 */
export function proseCitedIds(body: ReportBody): string[] {
  const seen: string[] = []
  for (const section of body.sections ?? []) {
    for (const match of String(section.content ?? '').matchAll(MARKER)) {
      const evidenceId = capturedId(match)
      if (evidenceId !== null && !seen.includes(evidenceId)) seen.push(evidenceId)
    }
  }
  return seen
}

/**
 * `evidenceId → 编号`。
 *
 * 存的那份只收**证据表里真的有**的 id：收下一个未知 id 的话，正文里
 * 那个 `[?]` 会被发一个编号，于是页面上出现一个能点的角标、点开什么都没有。
 *
 * 存的那份**不全**时，正文里没编号的接在最大号之后
 * ----------------------------------------------
 * 这不是冗余分支，是给**已经存在的坏数据**兜底。2026-09-19 之前
 * 「深化本节」不重发编号（后端 `refine._recompute` 漏了这一步，已修），
 * 所以库里那些被深化过的旧报告，`citations` 里**没有新采到的证据**。
 *
 * 续号从 `max(已有) + 1` 起算，与后端 `_CitationIndex._next` 逐字相同。
 * 写成"从个数 + 1 起算"在种子连续时给出同一个数，但种子一旦有缺口
 * 就会和已有的某个编号**撞车**——两句话的角标指向同一条证据，
 * 而页面上看起来完全正常。
 *
 * 存的那份**整个为空**（旧报告没有这个键）时，就是纯现算：
 * 第一个被引用的证据是 `[1]`，与后端 `build_citations` 相同。
 */
export function citationNumbers(body: ReportBody): Map<string, number> {
  const known = new Set(evidenceIds(body))
  const numbers = new Map<string, number>()

  for (const item of body.citations ?? []) {
    if (known.has(item.evidenceId) && !numbers.has(item.evidenceId)) {
      numbers.set(item.evidenceId, item.number)
    }
  }
  let next = Math.max(0, ...numbers.values()) + 1

  for (const evidenceId of proseCitedIds(body)) {
    if (known.has(evidenceId) && !numbers.has(evidenceId)) {
      numbers.set(evidenceId, next)
      next += 1
    }
  }
  return numbers
}

/** 把一段正文拆成"纯文本"与"角标"两种片段。 */
export function splitCitations(text: string, numbers: Map<string, number>): Segment[] {
  const segments: Segment[] = []
  let cursor = 0

  for (const match of text.matchAll(MARKER)) {
    const evidenceId = capturedId(match)
    // 取不到 id 时 `continue` 且**不推进游标**：这一整段标记会留在
    // 后面那个文本片段里原样显示。推进游标再跳过的话，标记就被
    // 无声吃掉了——读者会以为这句话本来就不需要证据。
    if (evidenceId === null) continue
    const start = match.index ?? 0
    if (start > cursor) segments.push({ kind: 'text', text: text.slice(cursor, start) })
    segments.push({
      kind: 'citation',
      text: match[0],
      evidenceId,
      number: numbers.get(evidenceId) ?? null,
    })
    cursor = start + match[0].length
  }

  if (cursor < text.length) segments.push({ kind: 'text', text: text.slice(cursor) })
  return segments
}

/**
 * 正文里指向**不存在的证据**的引用 id。**按出现次数，不去重**。
 *
 * 这是"幻觉引用"在页面上的可见形态。后端 `metrics.hallucinationRate`
 * 算的是**模型输出**里的编造比例，而那些 id 在 `coercion.phantomIds` 里、
 * 且已从正文删掉了一部分；这里数的是**最终正文里还剩多少处**——
 * 两个数不是一回事，也都不该被另一个顶替。
 *
 * 不去重是刻意的：页面上那句话要说的是"有 2 处引用点不开"（读者要
 * 一处一处去核），而不是"有 1 种坏的 id"。所以这里**不复用
 * `proseCitedIds`**——那个函数按定义是去重的（它给编号用）。
 */
export function unresolvedCitations(body: ReportBody): string[] {
  const known = new Set(evidenceIds(body))
  const found: string[] = []
  for (const section of body.sections ?? []) {
    for (const match of String(section.content ?? '').matchAll(MARKER)) {
      const evidenceId = capturedId(match)
      if (evidenceId !== null && !known.has(evidenceId)) found.push(evidenceId)
    }
  }
  return found
}

/**
 * 被正文引用到的证据，按编号升序。右侧证据面板读它。
 *
 * 编号为 `null`（不存在）的不进这个列表——它们没有可显示的卡片，
 * 而正文里的 `[?]` 本身已经说明了问题。
 */
export function citedEvidences(body: ReportBody): ReportEvidence[] {
  const byId = new Map((body.evidences ?? []).map((item) => [item.evidenceId, item]))
  return [...citationNumbers(body).entries()]
    .sort((left, right) => left[1] - right[1])
    .map(([evidenceId]) => byId.get(evidenceId))
    .filter((item): item is ReportEvidence => item !== undefined)
}
