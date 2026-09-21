/**
 * 舆情面板。
 *
 * 这一块存在的理由不是"报告里有个情感分析"，而是**把标注来源摊开**。
 * 标注是"模型优先、规则兜底"：模型漏掉某条样本时，关键词函数顶上。
 * 如果只印一个"正面 62%"，那个数字看起来全是模型判断的结果，
 * 而其中可能有小一半是"卡顿→负面、好用→正面"这种字符串匹配出来的。
 * 两者的可信度完全不同，混在一起印就是**用规则的结论冒充模型的结论**。
 *
 * 所以这一节的行长这样：先给一个比例条（一眼看到分布），
 * 紧接着就是**来源比例**（一眼看到这个分布有多少是模型给的），
 * 然后是逐条列表——每一条都带 `labeledBy` 徽章。
 * 混合比例不是脚注，它是这一节的主要信息之一。
 *
 * 空面板显示后端写好的那句话
 * ------------------------
 * `note` 是后端算出来的（"没有采到用户评价类证据，本节为空"），
 * 前端**不自己拼**："快速模式不做舆情标注"和"没采到评价类证据"
 * 是两种不同的空，前端分不清是哪种，硬拼一句就会在某种情况下说错。
 */
import { formatPercent, sourceTypeLabel } from '../../lib/format'
import type { Sentiment, SentimentLabel } from '../../types/report'
import { ReportPanel } from './ReportPanel'

const SENTIMENT_LABEL: Record<string, string> = {
  positive: '正面',
  neutral: '中性',
  negative: '负面',
}

/** 三种情感的配色。中性刻意用灰的——它不是"轻微正面"。 */
const TONE: Record<string, { bar: string; chip: string }> = {
  positive: { bar: 'bg-ok', chip: 'bg-ok/12 text-ok' },
  neutral: { bar: 'bg-fg-faint', chip: 'bg-raised text-fg-muted' },
  negative: { bar: 'bg-danger', chip: 'bg-danger/12 text-danger' },
}

function tone(sentiment: string) {
  return TONE[sentiment] ?? { bar: 'bg-fg-faint', chip: 'bg-raised text-fg-muted' }
}

export interface SentimentPanelProps {
  sentiment: Sentiment | undefined
  /** 点一条跳到右边证据面板 */
  onCite: (evidenceId: string) => void
}

function LabelRow({ item, onCite }: { item: SentimentLabel; onCite: (id: string) => void }) {
  const style = tone(item.sentiment)
  const byRule = item.labeledBy !== 'llm'

  return (
    <li className="border-t border-line py-2 first:border-t-0">
      <div className="flex items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${style.chip}`}>
          {SENTIMENT_LABEL[item.sentiment] ?? item.sentiment}
        </span>
        <span className="text-[11px] text-fg-muted">{item.brand || '未标注品牌'}</span>
        <span className="text-[10px] text-fg-faint">{sourceTypeLabel(item.sourceType)}</span>

        {/* 徽章只在**不是模型判断**时出现。
            给"模型判断"也配一个徽章的话，两行之间就没有视觉差别了，
            而读者扫一眼列表时想找的恰恰是那些规则兜底的条目。 */}
        {byRule && (
          <span
            className="ml-auto shrink-0 rounded border border-warn/40 bg-warn/10 px-1.5 py-0.5 text-[10px] text-warn"
            title="这一条不是模型判断的，是关键词规则兜底的"
          >
            规则
          </span>
        )}
        <button
          type="button"
          onClick={() => onCite(item.evidenceId)}
          className={[
            'shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px] text-brand hover:bg-brand/12',
            byRule ? '' : 'ml-auto',
          ].join(' ')}
          title="跳到这条证据"
        >
          {item.evidenceId}
        </button>
      </div>

      {item.excerpt && (
        <p className="mt-1 line-clamp-2 text-[11.5px] leading-relaxed text-fg">{item.excerpt}</p>
      )}
      {item.reason && (
        <p className="mt-0.5 text-[10.5px] leading-relaxed text-fg-faint">判定理由：{item.reason}</p>
      )}
    </li>
  )
}

export function SentimentPanel({ sentiment, onCite }: SentimentPanelProps) {
  if (!sentiment) return null

  const { counts, total } = sentiment

  return (
    <ReportPanel
      id="sentiment"
      title="用户舆情"
      aside={
        total > 0 && <span className="shrink-0 text-[11px] tabular text-fg-faint">{total} 条</span>
      }
    >
      {total === 0 ? (
        <p className="mt-2 text-[12px] text-fg-faint">{sentiment.note}</p>
      ) : (
        <>
          {/* 分布条。三段宽度按条数，中间不留缝——留缝会让人去数有几段。 */}
          <div className="mt-3 flex h-2 overflow-hidden rounded-full bg-raised">
            {(['positive', 'neutral', 'negative'] as const).map((key) =>
              counts[key] > 0 ? (
                <div
                  key={key}
                  className={tone(key).bar}
                  style={{ width: `${(counts[key] / total) * 100}%` }}
                  title={`${SENTIMENT_LABEL[key]} ${counts[key]} 条`}
                />
              ) : null,
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11.5px]">
            {(['positive', 'neutral', 'negative'] as const).map((key) => (
              <span key={key} className="flex items-center gap-1.5">
                <span className={`h-2 w-2 rounded-full ${tone(key).bar}`} />
                <span className="text-fg-muted">{SENTIMENT_LABEL[key]}</span>
                <span className="tabular text-fg">{counts[key]}</span>
                <span className="tabular text-fg-faint">
                  {formatPercent(counts[key] / total, 0)}
                </span>
              </span>
            ))}
          </div>

          {/* 来源比例。用后端写好的那句话，前端不重算——
              重算就是第二份真相源，两句一旦不一致没人知道该信谁。 */}
          <p className="mt-2.5 rounded-lg bg-raised px-3 py-2 text-[11px] leading-relaxed text-fg-muted">
            {sentiment.note}
            {sentiment.labeledByRule > 0 && (
              <span className="text-warn">
                {' '}
                带「规则」徽章的条目<span className="font-medium">不是</span>模型判断的。
              </span>
            )}
          </p>

          {sentiment.byBrand && sentiment.byBrand.length > 1 && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full border-collapse text-[11.5px]">
                <thead>
                  <tr className="text-fg-faint">
                    <th className="py-1 text-left font-medium">品牌</th>
                    <th className="py-1 text-right font-medium">正面</th>
                    <th className="py-1 text-right font-medium">中性</th>
                    <th className="py-1 text-right font-medium">负面</th>
                    <th className="py-1 text-right font-medium">正面率</th>
                  </tr>
                </thead>
                <tbody>
                  {sentiment.byBrand.map((row) => (
                    <tr key={row.brand} className="border-t border-line">
                      <th className="py-1 text-left font-normal text-fg">{row.brand}</th>
                      <td className="py-1 text-right tabular text-fg">{row.positive}</td>
                      <td className="py-1 text-right tabular text-fg-muted">{row.neutral}</td>
                      <td className="py-1 text-right tabular text-fg">{row.negative}</td>
                      <td className="py-1 text-right tabular text-fg-muted">
                        {formatPercent(row.positiveRate, 0)}
                        <span className="ml-1 text-fg-faint">/{row.total}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <ul className="mt-3">
            {sentiment.labels.map((item) => (
              <LabelRow key={item.evidenceId} item={item} onCite={onCite} />
            ))}
          </ul>
        </>
      )}
    </ReportPanel>
  )
}
