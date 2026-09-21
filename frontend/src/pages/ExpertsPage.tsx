/**
 * 专家公会：48 人的分工与参与度。
 *
 * **不编统计数字。**
 * 名册自带的 `stats` 里每个数都是 0，而那是"还没被量过"而不是
 * "量出来是 0"（后端 `Expert` 的注释就是这么写的，`source: "seed"`
 * 是它的标记）。把 0 当实测值印出来，用户看到的是一整页
 * "参与 0 次调研"的专家——看起来像系统坏了，或者像这 48 个人从没干过活。
 *
 * 所以这里**分两层显示**：
 * - 每个专家身上一定有的是 `participation`（被派进过多少份报告）——
 *   它从报告正文的 `team` 字段数出来，是真实数据。
 * - 名册里的 `skills` / `knowledgeTags` 是**配置**，不是战绩。
 *   显示它们是对的，但不能摆在"统计"的位置上。
 *
 * 参与度为 0 时直接不显示那个数字，而不是显示"0 次"——
 * 一次都没跑过的时候，这一页本来就没有统计可言。
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { Icon } from '../components/primitives/Icon'
import { PageHeader } from '../components/primitives/PageHeader'
import { Empty, ErrorNote, Loading } from '../components/primitives/States'
import { useAsync } from '../hooks/useAsync'
import { api } from '../lib/api'
import { EXPERT_LEVEL_BADGE, formatInt } from '../lib/format'

export default function ExpertsPage() {
  const [level, setLevel] = useState('')
  const [group, setGroup] = useState('')

  const { data, error, loading, reload } = useAsync(
    () => api.listExperts({ level, group }),
    [level, group],
  )

  return (
    <div className="mx-auto max-w-6xl px-8 py-8">
      <PageHeader
        title="专家公会"
        sub={
          data
            ? `${data.rosterSize} 位专家，分三层：决策层定边界、战略层切维度、执行层取证。`
            : '48 位专家的分工名册。'
        }
      />

      {/* ---------- 层级说明 ---------- */}
      {data && (
        <div className="mb-5 grid grid-cols-1 gap-3 sm:grid-cols-3">
          {data.byLevel.map((bucket) => (
            <button
              key={bucket.level}
              type="button"
              onClick={() => setLevel(level === bucket.level ? '' : bucket.level)}
              className={[
                'rounded-card border px-4 py-3 text-left transition-colors',
                level === bucket.level
                  ? 'border-brand bg-brand/8'
                  : 'border-line bg-panel hover:border-line-strong',
              ].join(' ')}
            >
              <div className="flex items-baseline gap-2">
                <span
                  className={[
                    'rounded px-1.5 py-0.5 text-[11px] font-medium',
                    EXPERT_LEVEL_BADGE[bucket.level] ?? 'bg-raised text-fg-muted',
                  ].join(' ')}
                >
                  {bucket.label}
                </span>
                <span className="tabular text-[18px] font-semibold text-fg">
                  {bucket.count}
                </span>
                <span className="text-[11px] text-fg-faint">人</span>
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-fg-muted">
                {bucket.description}
              </p>
            </button>
          ))}
        </div>
      )}

      {/* ---------- 分组筛选 ---------- */}
      {data && (
        <div className="mb-5 flex flex-wrap items-center gap-1.5">
          <Chip active={group === ''} onClick={() => setGroup('')}>
            全部分组
          </Chip>
          {data.byGroup.map((bucket) => (
            <Chip
              key={bucket.value}
              active={group === bucket.value}
              onClick={() => setGroup(group === bucket.value ? '' : bucket.value)}
            >
              {bucket.value}（{bucket.count}）
            </Chip>
          ))}
        </div>
      )}

      {loading && <Loading what="专家名册" />}
      {error && <ErrorNote error={error} onRetry={reload} />}

      {data && data.items.length === 0 && <Empty title="这个筛选下没有人" />}

      {/* ---------- 名册 ---------- */}
      {data && data.items.length > 0 && (
        <ul className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {data.items.map((expert) => (
            <li key={expert.expertId}>
              {/* 整张卡可点。用 `Link` 而不是 `a href`：后者会让整个应用
                  重新加载一次（React Router 的客户端路由就白做了）。 */}
              <Link
                to={`/experts/${expert.expertId}`}
                className="block h-full rounded-card focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
              >
                <article className="flex h-full flex-col rounded-card border border-line bg-panel px-4 py-3.5 shadow-card transition-colors hover:border-line-strong">
                <div className="flex items-start gap-3">
                  {/* 头像用名册里那个确定性的颜色（`hash(id) % palette`），
                      所以同一个人每次刷新都是同一个颜色——随机取色会让
                      "上次那个紫色的"变成一个找不回来的描述。 */}
                  <span
                    className="flex size-9 shrink-0 items-center justify-center rounded-full text-[13px] font-medium text-white"
                    style={{ backgroundColor: expert.avatarColor || '#8a8577' }}
                  >
                    {expert.name.slice(0, 1)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <h2 className="truncate text-[13px] font-medium text-fg">
                        {expert.name}
                      </h2>
                      <span
                        className={[
                          'shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px]',
                          EXPERT_LEVEL_BADGE[expert.level] ?? 'bg-raised text-fg-muted',
                        ].join(' ')}
                      >
                        {expert.expertId}
                      </span>
                    </div>
                    <p className="truncate text-[11px] text-fg-muted">
                      {expert.roleTitle}
                      {expert.group && (
                        <span className="text-fg-faint"> · {expert.group}</span>
                      )}
                    </p>
                  </div>
                </div>

                <p className="mt-2.5 flex-1 text-[11px] leading-relaxed text-fg-muted">
                  {expert.oneLiner}
                </p>

                <div className="mt-2.5 flex flex-wrap gap-1">
                  {expert.skills.slice(0, 4).map((skill) => (
                    <span
                      key={skill}
                      className="rounded bg-raised px-1.5 py-0.5 text-[10px] text-fg-muted"
                    >
                      {skill}
                    </span>
                  ))}
                </div>

                {/* 真实战绩。0 次时**什么都不显示**——
                    印一个"参与 0 次调研"出来，读起来像这个人在偷懒，
                    而事实是这台机器还没跑过几次。 */}
                {expert.participation > 0 && (
                  <div className="mt-3 flex items-center gap-1.5 border-t border-line pt-2.5 text-[11px] text-brand">
                    <Icon name="spark" size={12} />
                    <span className="tabular">
                      参与过 {formatInt(expert.participation)} 次调研
                    </span>
                  </div>
                )}
                </article>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'rounded-full border px-3 py-1 text-[11px] transition-colors',
        active
          ? 'border-brand bg-brand/10 text-brand'
          : 'border-line text-fg-muted hover:border-line-strong hover:text-fg',
      ].join(' ')}
    >
      {children}
    </button>
  )
}
