/**
 * 右栏：证据面板。正文里点 `[3]`，这一栏滚到第 3 条并高亮。
 *
 * 为什么默认只列**被正文引用到的**证据
 * ----------------------------------
 * 一份报告的 `evidences` 有几十上百条，其中被正文引用的是十几条。
 * 全列出来的话，读者点一个角标要在几百条里找——而"找到那一条"
 * 恰恰是这个面板唯一的用途。所以默认只列被引用的，另给一个开关看全部。
 *
 * 为什么"点不开的引用"要单独说一句
 * ----------------------------
 * 正文里的 `[?]` 指向一条不在证据表里的 id（模型编的，或已被剔除的）。
 * 它没有卡片可显示，**点它这一栏不会有任何反应**——而"点了没反应"
 * 会被读成"这个面板坏了"。所以在顶上明说有几处、分别是哪些 id。
 *
 * 可信度要说得出"为什么是 82 分"
 * --------------------------
 * `credibilityBreakdown` 是后端给的四项加分与扣分。参考实现的评分是
 * 一个裸整数，于是"为什么这条 82 分"在界面上永远说不出来。这里把它
 * 折在 `<details>` 里：默认不占地方，想看的时候点开就有。
 *
 * 舆情标签挂在证据卡上，**带 `labeledBy`**
 * ------------------------------------
 * 同一条证据在舆情那一节被标过情感。在证据卡上重复显示是有用的
 * （读者从这里点进来时应该看到它），但**必须带上判断来源**——
 * 一个"负面"标签，是模型判的还是关键词匹配出来的，是两件事。
 */
import { useEffect, useMemo, useState } from 'react'

import {
  credibilityTier,
  formatRelative,
  sourceTypeLabel,
} from '../../lib/format'
import {
  citationNumbers,
  citedEvidences,
  evidenceIds,
  unresolvedCitations,
} from '../../lib/reportCitation'
import type { ReportBody, ReportEvidence, SentimentLabel } from '../../types/report'

const TIER_TONE: Record<string, string> = {
  high: 'bg-ok/12 text-ok',
  medium: 'bg-warn/14 text-warn',
  low: 'bg-raised text-fg-muted',
}

export interface EvidenceAsideProps {
  body: ReportBody
  activeEvidenceId: string | null
  onCite: (evidenceId: string) => void
}

function Credibility({ evidence }: { evidence: ReportEvidence }) {
  const tier = credibilityTier(evidence.credibility)
  const breakdown = evidence.credibilityBreakdown

  return (
    <details className="mt-1">
      <summary className="flex cursor-pointer list-none items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-[10px] tabular ${TIER_TONE[tier]}`}>
          可信度 {Math.round(evidence.credibility)}
        </span>
        {evidence.degraded && (
          <span className="rounded bg-warn/12 px-1.5 py-0.5 text-[10px] text-warn">正文没抓到</span>
        )}
        {breakdown && (
          <span className="text-[10px] text-fg-faint">看明细</span>
        )}
      </summary>
      {breakdown ? (
        <table className="mt-1 border-collapse text-[10.5px] tabular">
          <tbody className="text-fg-muted">
            {(
              [
                ['来源类型', breakdown.sourceTypeScore],
                ['时效', breakdown.freshnessScore],
                ['正文内容', breakdown.contentScore],
                ['交叉引用', breakdown.crossRefScore],
                ['扣分', breakdown.penalties],
              ] as const
            ).map(([label, value]) => (
              <tr key={label}>
                <th className="pr-3 text-left font-normal text-fg-faint">{label}</th>
                <td className="pr-3 text-right">{value > 0 ? `+${value}` : value}</td>
              </tr>
            ))}
            <tr className="border-t border-line">
              <th className="pr-3 text-left font-normal text-fg">合计</th>
              <td className="pr-3 text-right text-fg">{breakdown.total}</td>
            </tr>
          </tbody>
        </table>
      ) : (
        // 老报告没有这个字段。**不编一个**："没法说明"要说出来，
        // 而不是显示一个看起来一样、其实是空的明细表。
        <p className="mt-1 text-[10.5px] text-fg-faint">
          这份报告没有存评分明细（早于可解释评分的版本）。
        </p>
      )}
    </details>
  )
}

function EvidenceCard({
  evidence,
  number,
  sentiment,
  active,
  onCite,
}: {
  evidence: ReportEvidence
  number: number | undefined
  sentiment: SentimentLabel | undefined
  active: boolean
  onCite: (id: string) => void
}) {
  return (
    <li
      id={`evidence-${evidence.evidenceId}`}
      className={[
        'rounded-lg border px-3 py-2.5 transition-colors',
        active ? 'border-brand bg-brand/6' : 'border-line bg-panel',
      ].join(' ')}
    >
      <div className="flex items-baseline gap-2">
        <button
          type="button"
          onClick={() => onCite(evidence.evidenceId)}
          title={evidence.evidenceId}
          className={[
            'shrink-0 rounded px-1 font-mono text-[10px] tabular',
            active ? 'bg-brand text-canvas' : 'bg-raised text-fg-muted',
          ].join(' ')}
        >
          {number ?? '—'}
        </button>
        <span className="text-[10px] text-fg-faint">{sourceTypeLabel(evidence.sourceType)}</span>
        {evidence.siteName && (
          <span className="truncate text-[10px] text-fg-faint">· {evidence.siteName}</span>
        )}
        {evidence.brand && (
          <span className="ml-auto shrink-0 text-[10px] text-fg-muted">{evidence.brand}</span>
        )}
      </div>

      <a
        href={evidence.url}
        target="_blank"
        // `noopener`：不加的话新开的页面能通过 `window.opener` 操作本页。
        rel="noreferrer noopener"
        className="mt-1.5 block text-[12px] leading-snug text-fg hover:text-brand"
      >
        {evidence.title || evidence.url || '（这条证据没有标题）'}
      </a>

      {(evidence.fullText || evidence.snippet) && (
        <p className="mt-1 line-clamp-4 text-[11px] leading-relaxed text-fg-muted">
          {/* 优先用 `fullText`：抓到了正文就用正文。抓不到时后端会把
              `degraded` 置位并保留 `snippet`，上面那个徽章会说明这件事。 */}
          {evidence.fullText || evidence.snippet}
        </p>
      )}

      <div className="mt-1 flex flex-wrap items-center gap-2">
        <Credibility evidence={evidence} />
        {evidence.publishedAt && (
          <span className="text-[10px] text-fg-faint">
            发布于 {formatRelative(evidence.publishedAt)}
          </span>
        )}
      </div>

      {sentiment && (
        <p className="mt-1.5 flex items-center gap-1.5 text-[10px] text-fg-faint">
          <span>舆情：{sentiment.sentiment}</span>
          {/* **来源必须一起显示。** 模型判的"负面"和关键词匹配出来的
              "负面"不是一回事，而这个标签会被人拿去数比例。 */}
          <span
            className={[
              'rounded px-1 py-px',
              sentiment.labeledBy === 'llm' ? 'bg-brand/12 text-brand' : 'bg-warn/12 text-warn',
            ].join(' ')}
          >
            {sentiment.labeledBy === 'llm' ? '模型判断' : '关键词规则'}
          </span>
        </p>
      )}

      {evidence.matchedDimensions.length > 0 && (
        <p className="mt-1 text-[10px] text-fg-faint">
          命中维度：{evidence.matchedDimensions.join('、')}
        </p>
      )}
    </li>
  )
}

export function EvidenceAside({ body, activeEvidenceId, onCite }: EvidenceAsideProps) {
  const [showAll, setShowAll] = useState(false)

  const cited = useMemo(() => citedEvidences(body), [body])
  // 编号直接读 `citationNumbers`，**不按列表下标现推一个**。
  // 下标推出来的那一份在正常情况下和它一模一样，所以两版看着都能跑；
  // 但正文的角标读的是 `citationNumbers`，一旦两者哪天不一致
  // （比如这里少渲染了一条），推出来的编号会整体串位，而页面上
  // 每一张卡片都还带着一个看着正常的号。
  const numbers = useMemo(() => citationNumbers(body), [body])

  const all = useMemo(() => {
    if (!showAll) return cited
    const byId = new Map(cited.map((item) => [item.evidenceId, item]))
    for (const evidence of body.evidences ?? []) {
      if (!byId.has(evidence.evidenceId)) byId.set(evidence.evidenceId, evidence)
    }
    return [...byId.values()]
  }, [cited, showAll, body.evidences])

  const sentimentByEvidence = useMemo(() => {
    const map = new Map<string, SentimentLabel>()
    for (const label of body.sentiment?.labels ?? []) {
      if (!map.has(label.evidenceId)) map.set(label.evidenceId, label)
    }
    return map
  }, [body.sentiment])

  const unresolved = unresolvedCitations(body)
  const total = evidenceIds(body).length

  // 角标被点之后，把对应的卡片滚进视野。
  //
  // `block: 'nearest'` 而不是 `'start'`：`'start'` 会把卡片顶到容器最上面，
  // 于是读者刚读的那条证据跳走了、上下文丢了。`'nearest'` 只在它不在
  // 视野里时才滚，而且滚最小的距离。
  //
  // 依赖放在 `activeEvidenceId` 上，**不放 `all`**：切换"显示全部"
  // 也会触发滚动，而那时读者没有点任何角标。
  useEffect(() => {
    if (!activeEvidenceId) return
    document
      .getElementById(`evidence-${activeEvidenceId}`)
      ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [activeEvidenceId])

  return (
    <div className="flex min-h-0 flex-col gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-[12px] font-medium text-fg-muted">证据</h2>
        <button
          type="button"
          onClick={() => setShowAll((value) => !value)}
          className="rounded px-1.5 py-0.5 text-[10.5px] text-fg-faint transition-colors hover:bg-raised hover:text-fg-muted"
        >
          {showAll ? `只看被引用的（${cited.length}）` : `显示全部（${total}）`}
        </button>
      </div>

      {unresolved.length > 0 && (
        <p className="rounded-lg border border-warn/40 bg-warn/8 px-2.5 py-1.5 text-[10.5px] leading-relaxed text-fg-muted">
          正文里有 <span className="tabular">{unresolved.length}</span> 处引用点不开
          （指向的证据不在证据表里）：
          <span className="font-mono text-fg-faint"> {unresolved.join('、')}</span>
        </p>
      )}

      {all.length === 0 ? (
        <p className="text-[12px] text-fg-faint">这份报告没有证据。</p>
      ) : (
        <ul className="flex min-h-0 flex-col gap-2 overflow-y-auto pr-0.5">
          {all.map((evidence) => (
            <EvidenceCard
              key={evidence.evidenceId}
              evidence={evidence}
              // 没被正文引用的证据没有编号——**不编一个**。
              // 编了的话读者会去正文里找那个不存在的角标。
              number={numbers.get(evidence.evidenceId)}
              sentiment={sentimentByEvidence.get(evidence.evidenceId)}
              active={evidence.evidenceId === activeEvidenceId}
              onCite={onCite}
            />
          ))}
        </ul>
      )}
    </div>
  )
}
