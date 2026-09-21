/**
 * 一位专家的档案：他是谁、会什么、**参加过哪几次调研**。
 *
 * 这一页要挡住的错和专家公会是同一个：**把"配置"当成"战绩"**。
 * 名册里每个人的 `skills` / `knowledgeTags` / `oneLiner` 都是手写的配置，
 * 而 `stats` 里那几个 0 是"还没被量过"（后端标了 `source: "seed"`），
 * 不是"量出来是 0"。所以：
 *
 * - 会什么 —— 显示，但标题就叫「能力（配置）」，不放在统计的位置上；
 * - 参加了几次 —— 显示，它由报告正文的 `team` 数出来，是真数据；
 * - `stats` 里那几个数 —— **一个都不显示**，只在页脚说明它们为什么不在。
 *   把 0 印成"完成任务 0 次 / 产出思维 0 条"，读起来像这个人在偷懒，
 *   而事实是这台机器还没跑过几次。
 *
 * 还有一个只有这一页会遇到的数：`participation`（参与者次数，取自全部报告）
 * 与 `reports`（列表，取自最近 200 份）**可以不等**。不等时要说出来，
 * 否则"参与过 13 次"下面只列了 8 条，看起来像丢了 5 次。
 */
import { Link, useParams } from 'react-router-dom'

import { Icon } from '../components/primitives/Icon'
import { Panel } from '../components/primitives/Panel'
import { Empty, ErrorNote, Loading } from '../components/primitives/States'
import { useAsync } from '../hooks/useAsync'
import { api, ApiError } from '../lib/api'
import type { ExpertDetail } from '../lib/api'
import { EXPERT_LEVEL_BADGE, formatDateTime, formatInt } from '../lib/format'

export default function ExpertDetailPage() {
  const { expertId = '' } = useParams()
  const { data, error, loading, reload } = useAsync(() => api.getExpert(expertId), [expertId])

  const notFound = error instanceof ApiError && error.status === 404

  return (
    <div className="mx-auto max-w-5xl px-8 py-8">
      <Link to="/experts" className="text-[12px] text-brand hover:underline">
        ← 专家公会
      </Link>

      {loading && !data && <Loading what="专家档案" />}
      {notFound && (
        <div className="mt-6">
          <Empty
            title={`名册里没有 ${expertId}`}
            hint={
              <>
                名单是代码资产（`experts_seed.yaml` 手写、脚本生成），
                所以拼错的 id 一定查不到。回{' '}
                <Link to="/experts" className="text-brand hover:underline">
                  专家公会
                </Link>{' '}
                对着抄一个。
              </>
            }
          />
        </div>
      )}
      {error && !notFound && (
        <div className="mt-6">
          <ErrorNote error={error} onRetry={reload} />
        </div>
      )}

      {data && <Body expert={data} />}
    </div>
  )
}

function Body({ expert }: { expert: ExpertDetail }) {
  // 列表是"最近 200 份报告里有他的"，而 participation 是"全部报告里有他的"。
  // 只有超过 200 份报告时才会不等——那时页面必须解释，不能默默少列几条。
  const shown = expert.reports.length
  const truncated = expert.participation > shown

  return (
    <>
      {/* ---------- 页头 ---------- */}
      <header className="mt-4 mb-6 flex items-start gap-4">
        <span
          className="flex size-14 shrink-0 items-center justify-center rounded-full text-[20px] font-medium text-white"
          style={{ backgroundColor: expert.avatarColor || '#8a8577' }}
        >
          {expert.name.slice(0, 1)}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-[22px] font-semibold tracking-tight text-fg">{expert.name}</h1>
            <span
              className={[
                'rounded px-1.5 py-0.5 font-mono text-[11px]',
                EXPERT_LEVEL_BADGE[expert.level] ?? 'bg-raised text-fg-muted',
              ].join(' ')}
            >
              {expert.expertId}
            </span>
            <span className="rounded bg-raised px-1.5 py-0.5 text-[11px] text-fg-muted">
              {expert.levelLabel}
            </span>
            {expert.group && (
              <span className="text-[11px] text-fg-faint">{expert.group}</span>
            )}
          </div>
          <p className="mt-1.5 text-[13px] text-fg-muted">{expert.roleTitle}</p>
          <p className="mt-2 text-[13px] leading-relaxed text-fg">{expert.oneLiner}</p>
        </div>
      </header>

      {/* ---------- 战绩 ---------- */}
      <Panel
        title="参与过的调研"
        aside={
          expert.participation > 0 && (
            <span className="tabular text-[11px] text-fg-faint">
              共 {formatInt(expert.participation)} 次
            </span>
          )
        }
      >
        {expert.participation === 0 ? (
          // 不写"参与 0 次"。一次调研只用得上 48 人里的一小部分，
          // "这次没轮到他"与"他从没干过活"是两件事。
          <p className="text-[12px] leading-relaxed text-fg-muted">
            这位还没被派进过调研。名册有 48 人，而一次调研只用得上一部分——
            没有记录不等于没有能力。
          </p>
        ) : (
          <>
            {truncated && (
              <p className="mb-2 text-[11px] text-warn">
                这里只列了 {formatInt(shown)} 条：接口取的是
                <span className="text-fg-muted">最近 200 份报告</span>
                ，而他参与过 {formatInt(expert.participation)} 次。差的那几次在更早的报告里。
              </p>
            )}
            <ul className="divide-y divide-line">
              {expert.reports.map((report) => (
                <li key={report.reportId}>
                  <Link
                    to={`/report/${report.reportId}`}
                    className="flex items-baseline gap-3 py-2 hover:text-brand"
                  >
                    <span className="min-w-0 flex-1 truncate text-[12.5px] text-fg">
                      {report.subject || report.query || '（未命名）'}
                    </span>
                    <span className="shrink-0 font-mono text-[10.5px] text-fg-faint">
                      {report.reportId}
                    </span>
                    <span className="shrink-0 text-[10.5px] text-fg-faint">
                      {report.generatedAt ? formatDateTime(report.generatedAt) : ''}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )}
      </Panel>

      {/* ---------- 配置 ---------- */}
      <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
        <Panel title="能力（配置）">
          <div className="flex flex-wrap gap-1.5">
            {expert.skills.map((skill) => (
              <span
                key={skill}
                className="rounded bg-raised px-2 py-0.5 text-[11px] text-fg-muted"
              >
                {skill}
              </span>
            ))}
          </div>
          <p className="mt-3 border-t border-line pt-2.5 text-[11px] leading-relaxed text-fg-faint">
            这一栏是<span className="text-fg-muted">名册里写好的</span>
            ，不是跑出来的。「能力」是配置，「参与过几次」才是战绩。
          </p>
        </Panel>

        <Panel title="知识领域">
          <p className="text-[12px] leading-relaxed text-fg-muted">
            {expert.knowledgeBase || '（名册里没写知识库）'}
          </p>
          {expert.knowledgeTags.length > 0 && (
            <div className="mt-2.5 flex flex-wrap gap-1.5 border-t border-line pt-2.5">
              {expert.knowledgeTags.map((tag) => (
                <span
                  key={tag}
                  className="rounded border border-line px-2 py-0.5 text-[11px] text-fg-muted"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </Panel>
      </div>

      {/* ---------- 为什么没有统计 ---------- */}
      {expert.stats.source === 'seed' && (
        <p className="mt-4 flex items-start gap-2 rounded-card border border-dashed border-line-strong px-4 py-3 text-[11px] leading-relaxed text-fg-faint">
          <Icon name="spark" size={12} />
          <span>
            名册自带的统计（完成任务数、产出思维条数、成本）
            <span className="text-fg-muted">还没被量过</span>
            ，后端标的是 `source: seed`——里面的 0 是"此刻正确"，不是"量出来是 0"。
            所以这一页一个都不显示：把 0 印成战绩，读起来像这位专家没干过活。
          </span>
        </p>
      )}
    </>
  )
}
