/**
 * 证据流。每条证据是一张卡，**带可信度分档和降级标记**。
 *
 * 两件不能省的事：
 * - `degraded`（只有摘要、正文没抓到）必须显示。不显示的话，
 *   一条只有搜索摘要的证据看起来和一条读到了全文的证据一模一样，
 *   而它们的可信度根本不是一回事。
 * - 图片不进这个面板。配图归图集（报告页），混进证据流会让
 *   "这里有多少条证据"这个数变得数不清。
 */
import { CREDIBILITY_TIER_STYLE, credibilityTier, SOURCE_TYPE_LABEL } from '../../lib/format'
import type { Evidence } from '../../types/domain'

function hostOf(url: string): string {
  // 只用来显示。**不用它算"独立信源数"**——那条规则的真相源在后端
  // （`independent_domain`，要处理子域与 www），在前端重写一遍会让
  // 面板上的域名数和指标里的对不上。
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}

function EvidenceCard({ evidence }: { evidence: Evidence }) {
  const tier = credibilityTier(evidence.credibility)
  return (
    <li className="border-b border-line/60 py-2.5 last:border-b-0">
      <div className="flex items-start gap-2">
        <span
          className={[
            'shrink-0 rounded px-1.5 py-0.5 font-mono text-[11px]',
            CREDIBILITY_TIER_STYLE[tier],
          ].join(' ')}
          title="可信度 0–100。分档阈值与后端 evidence/credibility.py 一致"
        >
          {evidence.credibility}
        </span>
        <div className="min-w-0 flex-1">
          <a
            href={evidence.url}
            target="_blank"
            rel="noreferrer noopener"
            className="block truncate text-xs font-medium text-fg hover:text-brand"
            title={evidence.title}
          >
            {evidence.title || evidence.url}
          </a>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-fg-faint">
            <span>{SOURCE_TYPE_LABEL[evidence.sourceType]}</span>
            <span className="truncate">{hostOf(evidence.url)}</span>
            {evidence.brand && <span className="text-fg-muted">{evidence.brand}</span>}
            {evidence.degraded && (
              <span className="text-warn" title="正文没抓到，只有搜索摘要">
                仅摘要
              </span>
            )}
          </div>
        </div>
      </div>
    </li>
  )
}

export function EvidenceStream({ evidences }: { evidences: Evidence[] }) {
  if (evidences.length === 0) {
    return <p className="py-6 text-center text-xs text-fg-faint">还没有采到证据。</p>
  }

  return (
    <div className="h-full overflow-y-auto">
      <ul>
        {evidences.map((evidence, index) => (
          <EvidenceCard
            // 下标参与 key，因为**这个列表只会追加**：证据没有环形上限，
            // 也不重排、不过滤，所以"第几条"就是稳定身份。
            //
            // 单用 `evidenceId` 会出问题：同一个 url 被两个检索词各采到一次是
            // 事实（指标按两条算），而那种情况下 evidenceId 可能相同——
            // 用它做 key 会让 React 把两条当成一条，少渲染一条证据。
            key={`${index}-${evidence.evidenceId}`}
            evidence={evidence}
          />
        ))}
      </ul>
    </div>
  )
}
