/**
 * 论点表。报告里每一个"我们认为是这样"的说法，连同它的置信度与依据。
 *
 * 这一块是**最该被翻的一块**：正文是叙述，叙述可以平滑地滑过
 * 证据不足的地方；论点表是清单，清单滑不过去。同一份报告，
 * 正文读起来笃定、论点表里一半标着"无证据"——这是两份材料同时在说话，
 * 而读者需要看到后者。
 *
 * 默认按**置信度升序**排，也就是最弱的排在最前面
 * -----------------------------------------
 * 按正文顺序排最自然，也最没用：读者会一条条读下去，读到第五十条
 * 才发现最后那三条才是没证据的。默认把最弱的顶上来，等于一进来
 * 就回答"这份报告哪里最不结实"。
 *
 * `phantomEvidenceIds` 是**模型编过引用**的痕迹
 * -----------------------------------------
 * 它非空表示这条论点的引用里，有几个 id 在证据表里查不到（已被剔除）。
 * 这是一个**已经发生过的**幻觉事件——不是风险，是记录。所以它用
 * `danger` 色标出来，而不是并进"低置信"里含糊过去。
 */
import { useMemo, useState } from 'react'

import { CONFIDENCE_LABEL } from '../../lib/format'
import type { ReportClaim } from '../../types/report'

/** 置信度排序权重。**低的排前面**，理由见文件头。 */
const ORDER: Record<string, number> = { unverified: 0, low: 1, medium: 2, high: 3 }

const CONFIDENCE_TONE: Record<string, string> = {
  high: 'bg-ok/12 text-ok',
  medium: 'bg-brand/12 text-brand',
  low: 'bg-warn/14 text-warn',
  unverified: 'bg-danger/12 text-danger',
}

function confidenceLabel(value: string): string {
  return (CONFIDENCE_LABEL as Record<string, string | undefined>)[value] ?? value
}

export function ReportClaims({ claims }: { claims: ReportClaim[] | undefined }) {
  const [onlyWeak, setOnlyWeak] = useState(false)
  // `claims ?? []` 直接写在这里会给每次渲染一个新数组，下面两个 `useMemo`
  // 就每次都重算——memo 白写。把归一化本身也 memo 掉。
  const items = useMemo(() => claims ?? [], [claims])

  const weakCount = useMemo(
    () => items.filter((claim) => (ORDER[claim.confidence] ?? 0) <= 1).length,
    [items],
  )

  const shown = useMemo(() => {
    const filtered = onlyWeak
      ? items.filter((claim) => (ORDER[claim.confidence] ?? 0) <= 1)
      : items
    // 同置信度的两条保持原文顺序：`Array.prototype.sort` 从 ES2019 起
    // 保证稳定，所以这里不用再补一个次级键。显式写一个按 `claimId` 排的
    // 次级键反而更坏——`claimId` 是随机的，那会让列表顺序每次都不一样。
    return [...filtered].sort(
      (left, right) => (ORDER[left.confidence] ?? 0) - (ORDER[right.confidence] ?? 0),
    )
  }, [items, onlyWeak])

  if (items.length === 0) {
    return (
      <section className="rounded-card border border-line bg-panel p-4 shadow-card">
        <h3 className="text-[13px] font-medium text-fg">论点</h3>
        <p className="mt-2 text-[12px] text-fg-faint">这份报告没有单独列出论点。</p>
      </section>
    )
  }

  return (
    <section id="claims" className="scroll-mt-[88px] rounded-card border border-line bg-panel p-4 shadow-card">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h3 className="text-[13px] font-medium text-fg">
          论点 <span className="ml-1 text-[11px] tabular text-fg-faint">{items.length} 条</span>
        </h3>
        <button
          type="button"
          onClick={() => setOnlyWeak((value) => !value)}
          className={[
            'rounded-lg px-2 py-0.5 text-[11px] transition-colors',
            onlyWeak ? 'bg-warn/14 text-warn' : 'text-fg-faint hover:bg-raised hover:text-fg-muted',
          ].join(' ')}
        >
          {onlyWeak ? `显示全部（${items.length}）` : `只看没证据的（${weakCount}）`}
        </button>
      </div>

      {shown.length === 0 ? (
        <p className="mt-2 text-[12px] text-ok">没有低置信或无证据的论点。</p>
      ) : (
        <ul className="mt-2.5 flex flex-col">
          {shown.map((claim) => (
            <li key={claim.claimId} className="border-t border-line py-2 first:border-t-0">
              <div className="flex flex-wrap items-baseline gap-2">
                <span
                  className={[
                    'shrink-0 rounded px-1.5 py-0.5 text-[10px]',
                    CONFIDENCE_TONE[claim.confidence] ?? CONFIDENCE_TONE.medium,
                  ].join(' ')}
                >
                  {confidenceLabel(claim.confidence)}
                </span>

                {claim.crossValidated && (
                  <span
                    className="rounded bg-brand/10 px-1.5 py-0.5 text-[10px] text-brand"
                    title={`${claim.independentDomains ?? '?'} 个独立来源印证`}
                  >
                    交叉验证 {claim.independentDomains ?? ''}
                  </span>
                )}

                {claim.brand && <span className="text-[10px] text-fg-faint">{claim.brand}</span>}
                {claim.dimension && (
                  <span className="text-[10px] text-fg-faint">· {claim.dimension}</span>
                )}

                <span className="ml-auto shrink-0 text-[10px] text-fg-faint tabular">
                  {claim.evidenceIds.length} 条证据
                </span>
              </div>

              <p className="mt-1 text-[12.5px] leading-relaxed text-fg">{claim.text}</p>

              {/* 编造过的引用：**已经发生的事**，不是风险提示。 */}
              {(claim.phantomEvidenceIds ?? []).length > 0 && (
                <p className="mt-1 text-[10.5px] leading-relaxed text-danger">
                  这条论点引用过 {(claim.phantomEvidenceIds ?? []).length} 个查不到的
                  evidence_id
                  <span className="font-mono text-fg-faint">
                    {' '}
                    {(claim.phantomEvidenceIds ?? []).join('、')}
                  </span>
                  ，已从正文里剔除。
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
