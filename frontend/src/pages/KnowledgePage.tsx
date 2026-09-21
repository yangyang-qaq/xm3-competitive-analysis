/**
 * 知识库：沉淀下来的证据来源。
 *
 * 这一页最要紧的一件事是把**两个数分开说**
 * ------------------------------------
 * "266 条来源"和"被引用 2426 次"是同一批数据的两个口径，而它们差了 9 倍。
 * 只显示其中一个，用户就会在别处看到另一个数然后以为是 bug：
 * 报告里写"本次用了 216 条证据"（那是行数），知识库写"266 条来源"
 * （那是去重后的）。两个都对，问的是两件事。所以页头把两个都写出来，
 * 并注明各自的口径。
 *
 * 去重在**服务端**做（`repo.evidences.library`）。放在前端做的话，
 * 分页会在去重之前发生——第 2 页可能全是第 1 页已经显示过的来源。
 */
import { useState } from 'react'

import { Icon } from '../components/primitives/Icon'
import { PageHeader } from '../components/primitives/PageHeader'
import { Empty, ErrorNote, Loading } from '../components/primitives/States'
import { useAsync } from '../hooks/useAsync'
import { api, type EvidenceQuery } from '../lib/api'
import {
  SOURCE_TYPE_LABEL,
  credibilityTier,
  formatInt,
  formatRelative,
} from '../lib/format'
import type { SourceType } from '../types/domain'

const PAGE_SIZE = 24

/** 可信度分档的颜色。三档与后端 `credibility.py` 的语义一致。 */
const TIER_STYLE = {
  high: 'bg-ok/12 text-ok',
  medium: 'bg-warn/15 text-warn',
  low: 'bg-fg-faint/15 text-fg-muted',
} as const

export default function KnowledgePage() {
  const [query, setQuery] = useState<EvidenceQuery>({ limit: PAGE_SIZE, offset: 0 })
  const [draft, setDraft] = useState('')

  const facets = useAsync(() => api.evidenceFacets())
  const list = useAsync(() => api.listEvidences(query), [
    query.brand,
    query.sourceType,
    query.q,
    query.offset,
  ])

  /** 改筛选条件时**必须把 offset 归零**。不归零的话，用户在第 3 页
   *  换一个品牌，会得到一个空的第 3 页，而结果其实有几十条。 */
  function update(patch: Partial<EvidenceQuery>) {
    setQuery((prev) => ({ ...prev, ...patch, offset: 0 }))
  }

  const page = Math.floor((query.offset ?? 0) / PAGE_SIZE) + 1
  const pages = list.data ? Math.max(1, Math.ceil(list.data.total / PAGE_SIZE)) : 1

  return (
    <div className="mx-auto max-w-6xl px-8 py-8">
      <PageHeader
        title="知识库"
        sub={
          facets.data ? (
            <>
              从历次调研里沉淀下来的证据来源，按使用频次排序。
              <span className="text-fg-faint">
                {' '}
                {formatInt(facets.data.total)} 条来源（去重后）· 累计被引用{' '}
                {formatInt(facets.data.mentions)} 次 —— 两个数都对，只是口径不同。
              </span>
            </>
          ) : (
            '从历次调研里沉淀下来的证据来源。'
          )
        }
      />

      {/* ---------- 筛选 ---------- */}
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <div className="relative">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-fg-faint">
            <Icon name="search" size={15} />
          </span>
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') update({ q: draft.trim() })
              if (event.key === 'Escape') {
                setDraft('')
                update({ q: '' })
              }
            }}
            placeholder="按标题或摘要搜，回车确认"
            className="w-64 rounded-lg border border-line bg-panel py-1.5 pl-9 pr-3 text-[12px] text-fg placeholder:text-fg-faint focus:border-brand focus:outline-none"
          />
        </div>

        <FilterSelect
          label="来源"
          value={query.sourceType ?? ''}
          options={(facets.data?.bySourceType ?? []).map((bucket) => ({
            value: bucket.value,
            label: SOURCE_TYPE_LABEL[bucket.value as SourceType] ?? bucket.value,
            count: bucket.count,
          }))}
          onChange={(value) => update({ sourceType: value })}
        />

        <FilterSelect
          label="品牌"
          value={query.brand ?? ''}
          options={(facets.data?.byBrand ?? []).map((bucket) => ({
            value: bucket.value,
            label: bucket.value,
            count: bucket.count,
          }))}
          onChange={(value) => update({ brand: value })}
        />

        {(query.brand || query.sourceType || query.q) && (
          <button
            type="button"
            onClick={() => {
              setDraft('')
              setQuery({ limit: PAGE_SIZE, offset: 0 })
            }}
            className="rounded-lg border border-line px-3 py-1.5 text-[12px] text-fg-muted hover:border-line-strong hover:text-fg"
          >
            清空筛选
          </button>
        )}
      </div>

      {list.error && <ErrorNote error={list.error} onRetry={list.reload} />}
      {!list.error && list.loading && <Loading what="证据来源" />}
      {!list.error && !list.loading && list.data?.items.length === 0 && (
        <Empty
          title="没有匹配的来源"
          hint="换个关键词，或者清空筛选条件。"
        />
      )}

      {/* ---------- 列表 ---------- */}
      {list.data && list.data.items.length > 0 && (
        <>
          <ul className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {list.data.items.map((item) => {
              const tier = credibilityTier(item.credibility)
              return (
                <li key={item.evidenceId}>
                  <article className="flex h-full flex-col rounded-card border border-line bg-panel px-4 py-3.5 shadow-card">
                    <div className="flex items-start justify-between gap-3">
                      <h2 className="line-clamp-2 text-[13px] font-medium leading-snug text-fg">
                        {item.title || item.url}
                      </h2>
                      <span
                        className={[
                          'tabular shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium',
                          TIER_STYLE[tier],
                        ].join(' ')}
                        title="确定性可信度评分：来源类型 + 正文长度 + 是否有发布时间"
                      >
                        {Math.round(item.credibility)}
                      </span>
                    </div>

                    <p className="mt-1.5 line-clamp-3 flex-1 text-[11px] leading-relaxed text-fg-muted">
                      {item.snippet}
                    </p>

                    <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-fg-faint">
                      <span className="rounded bg-raised px-1.5 py-0.5">
                        {SOURCE_TYPE_LABEL[item.sourceType] ?? item.sourceType}
                      </span>
                      {item.siteName && <span>{item.siteName}</span>}
                      {/* **这一列是知识库存在的理由。** 一条被 11 次调研
                          都采到的来源，比一条只有高分但没人用过的更值得复用。 */}
                      <span className="tabular text-brand">
                        被 {item.taskCount} 次调研引用
                      </span>
                      {item.degraded && <span className="text-warn">降级</span>}
                      <span className="ml-auto">{formatRelative(item.capturedAt)}</span>
                    </div>

                    <a
                      href={item.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="mt-2 truncate font-mono text-[10px] text-fg-faint hover:text-brand"
                      title={item.url}
                    >
                      {item.url}
                    </a>
                  </article>
                </li>
              )
            })}
          </ul>

          {/* ---------- 翻页 ---------- */}
          {pages > 1 && (
            <div className="mt-6 flex items-center justify-center gap-3">
              <button
                type="button"
                disabled={page <= 1}
                onClick={() =>
                  setQuery((prev) => ({ ...prev, offset: Math.max(0, (prev.offset ?? 0) - PAGE_SIZE) }))
                }
                className="rounded-lg border border-line px-3 py-1 text-[12px] text-fg-muted disabled:opacity-40 enabled:hover:border-line-strong enabled:hover:text-fg"
              >
                上一页
              </button>
              <span className="tabular text-[12px] text-fg-muted">
                {page} / {pages}
              </span>
              <button
                type="button"
                disabled={page >= pages}
                onClick={() =>
                  setQuery((prev) => ({ ...prev, offset: (prev.offset ?? 0) + PAGE_SIZE }))
                }
                className="rounded-lg border border-line px-3 py-1 text-[12px] text-fg-muted disabled:opacity-40 enabled:hover:border-line-strong enabled:hover:text-fg"
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

/** 下拉筛选。计数显示在选项里——**没有计数的筛选项会让人点进空结果**。 */
function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: Array<{ value: string; label: string; count: number }>
  onChange: (value: string) => void
}) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="rounded-lg border border-line bg-panel px-3 py-1.5 text-[12px] text-fg focus:border-brand focus:outline-none"
    >
      <option value="">
        {label}：全部
        {options.length > 0 &&
          `（${formatInt(options.reduce((sum, item) => sum + item.count, 0))}）`}
      </option>
      {options.map((item) => (
        <option key={item.value} value={item.value}>
          {item.label}（{formatInt(item.count)}）
        </option>
      ))}
    </select>
  )
}
