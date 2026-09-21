/**
 * 决策回放：把一次任务的调用轨迹摊开在时间上，拖着看它**当时**是怎么一步步走过来的。
 *
 * 这一页回答的是「它为什么这么做」，不是「它做出了什么」——后者在报告页。
 * 所以主角是顺序、耗时、钱，不是结论。
 *
 * 三件事必须写在页面上，否则这一页一定会被读错
 * ============================================
 *
 * **一、滑杆走的是 span，不是证据。** `evidences.capturedAt` 是**按采集轮次**
 * 盖的章（实测两个真任务各只有 2 个不同取值），没有逐条的时间分辨率，
 * 所以"证据随着时间一条条冒出来"在数据上做不到。右栏因此是
 * **跟着当前步高亮/过滤**，不是随时间累积。不说清楚，用户会以为滑杆坏了。
 *
 * **二、左栏的树顺序与滑杆的步序不是同一个顺序。** 接口给的是深度优先的树，
 * 时间线是按 `spanId` 编号排的（= 开始顺序，见 `lib/replay.ts::flattenSpans`）。
 * 两者只在有嵌套时才不同，而当前库里 1036 行 span **没有一行有 `parentId`**，
 * 所以现在看不出差别——正因为看不出，才要当场把对应关系算出来印在每一行上
 * （"第 N 步"），而不是指望两个顺序碰巧一致。等真出现了嵌套，
 * 这里不会静默错位。
 *
 * **三、前 47% 是没在抓取的。** 实测 `TK-1d1ff1f7090b` 的 249 步里，
 * 第一个 `fetch` span 排在第 117 位（需求解析、专家调度、最早的几轮搜索
 * 占了前面这一段）。滑杆拖到一半还是"0 条证据"，看起来就像坏了，
 * 所以把那条线标出来，并给一个直接跳过去的按钮。
 *
 * 数据全部走 `GET /api/tasks/{id}/trace`。**没有 `/api/reports/{id}/trace`，
 * 是有意不做的**（见 `docs/DECISIONS.md` D4）：报告详情已经给了 `taskId`，
 * 再开一条报告级的路由就是同一份数据两条取法。
 * `?report=` 只用来取**证据列表**，取不到就不显示右栏，而不是显示 0 条。
 */
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { Panel } from '../components/primitives/Panel'
import { Empty, ErrorNote, Loading } from '../components/primitives/States'
import { useAsync } from '../hooks/useAsync'
import { api } from '../lib/api'
import {
  CREDIBILITY_TIER_STYLE,
  SPAN_KIND_LABEL,
  credibilityTier,
  formatCost,
  formatDuration,
  formatInt,
} from '../lib/format'
import {
  buildTimeline,
  firstFetchIndex,
  groupEvidencesByHost,
  replayFrame,
} from '../lib/replay'
import type { ReplayFrame, ReplayTimeline } from '../lib/replay'
import type { SpanNode, SpanStatus } from '../types/domain'
import type { ReportEvidence } from '../types/report'

const STATUS_STYLE: Record<SpanStatus, string> = {
  ok: 'text-fg-faint',
  degraded: 'text-warn',
  error: 'text-danger',
}

const STATUS_LABEL: Record<SpanStatus, string> = {
  ok: '正常',
  degraded: '降级',
  error: '失败',
}

/**
 * 展示用的步号。**滑杆的取值是 0 起的下标**，`+1` 只出现在文字里。
 *
 * 分成两个函数是因为这件事只能有一处决定：一处 `+1`、另一处忘了，
 * 页面上就会出现"第 118 步"和"第 117 步"同时指向同一个 span。
 */
function stepLabel(index: number): string {
  return `第 ${index + 1} 步`
}

/** 毫秒的可读化。`formatDuration` 要秒，所以秒以下不进它。 */
function formatMs(ms: number): string {
  const seconds = Math.round(ms / 1000)
  return seconds < 60 ? `${formatInt(ms)}ms` : formatDuration(seconds)
}

/** 相对时间线起点的偏移。`m:ss`，够 249 步这种量级用。 */
function formatOffset(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const minutes = Math.floor(total / 60)
  return `${minutes}:${String(total % 60).padStart(2, '0')}`
}

export default function TracePage() {
  const { taskId = '' } = useParams()
  const [params] = useSearchParams()
  const reportId = params.get('report') ?? ''

  const trace = useAsync(() => api.taskTrace(taskId), [taskId])
  // 没有 `?report=` 时给 `null` 而不是跳过这次 useAsync：hook 不能有条件地调用，
  // 而这里 `Promise.resolve(null)` 的表达力刚好够——"这次没有证据可对照"。
  const report = useAsync(
    () => (reportId ? api.getReport(reportId) : Promise.resolve(null)),
    [reportId],
  )

  /**
   * 游标。**存的是"哪个任务的第几步"，不是一个裸下标。**
   *
   * 裸下标 + 一个 `useEffect` 归零是常见写法，但它在换任务时有真实的空档：
   * 先按旧下标渲染一帧（于是"第 41 步"指的其实是第 40 步），effect 跑完才归零。
   * 把任务 id 一起存进来，渲染时发现对不上就当 0 用——不经过 effect，
   * 也就没有那一次错渲染。
   */
  const [cursorState, setCursorState] = useState({ taskId, index: 0 })
  const cursor = cursorState.taskId === taskId ? cursorState.index : 0
  const setCursor = (index: number) => setCursorState({ taskId, index })

  const roots = trace.data?.spans
  const timeline = useMemo(() => buildTimeline(roots ?? []), [roots])

  const evidenceByHost = useMemo(
    () => groupEvidencesByHost(report.data?.data.evidences ?? []),
    [report.data],
  )

  // `spanId → 步序`。**这是树与滑杆之间唯一的对应关系**，当场算出来，
  // 而不是假设 `steps[i]` 就是树的第 i 行（那是两个顺序，见文件头第二点）。
  // 先到先得：游离 span 的 spanId 是空串，理论上会撞在一起。
  const stepIndexById = useMemo(() => {
    const map = new Map<string, number>()
    for (const step of timeline.steps) {
      if (!map.has(step.span.spanId)) map.set(step.span.spanId, step.index)
    }
    return map
  }, [timeline])

  const frame = replayFrame(timeline, cursor, evidenceByHost)
  // 走完全程的那一帧就是"终点有多少条证据"，拿它当分母。
  // 不另算一遍"所有证据"：那样分母会包含**没有任何 span 对应**的证据
  // （纯搜索摘要），分子永远追不上，进度条一辈子走不到头。
  const fullFrame = replayFrame(timeline, timeline.steps.length - 1, evidenceByHost)

  const totalEvidences = report.data?.data.evidences?.length ?? 0
  const matchedEvidences = fullFrame.reached
  const firstFetch = firstFetchIndex(timeline)

  const summary = trace.data?.summary

  return (
    <div className="flex h-screen flex-col">
      {/* ---- 顶栏 ---- */}
      <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-line bg-panel px-4 py-2">
        <Link
          to={reportId ? `/report/${reportId}` : `/workspace/${taskId}`}
          className="shrink-0 text-[12px] text-brand hover:underline"
        >
          ← {reportId ? '回报告' : '回工作台'}
        </Link>
        <h1 className="shrink-0 text-sm text-fg">决策回放</h1>
        <span className="shrink-0 font-mono text-[11px] text-fg-faint">{taskId}</span>

        <div className="ml-auto flex shrink-0 items-center gap-3 text-[11px] text-fg-faint">
          {summary && (
            <>
              <span title="这条时间线有多少个台阶。滑杆的上界就是它减一">
                {formatInt(summary.spanCount)} 次调用
              </span>
              <span title="所有 span 的耗时相加。**并发扇出时会远大于任务耗时**，它不是耗时">
                总占用 {formatMs(summary.spanDurationMs)}
              </span>
              <span title="span 覆盖的墙钟窗口。也不是任务耗时">
                窗口 {formatMs(summary.spanWindowMs)}
              </span>
              <span title="总占用 ÷ 窗口。≈1 是串行链，4.3 是同时开了四路">
                并发 {summary.concurrency.toFixed(2)}×
              </span>
              <span title="Σ trace.cost_usd">{formatCost(summary.costUsd)}</span>
            </>
          )}
          <Link
            to={`/workspace/${taskId}`}
            className="rounded-lg border border-line px-2.5 py-1 text-fg-muted hover:border-line-strong hover:text-fg"
          >
            看工作台
          </Link>
        </div>
      </header>

      {trace.error ? (
        <div className="mx-auto max-w-3xl px-8 py-16">
          <ErrorNote error={trace.error} onRetry={trace.reload} />
        </div>
      ) : trace.loading && !trace.data ? (
        <div className="mx-auto max-w-3xl px-8 py-16">
          <Loading what="调用轨迹" />
        </div>
      ) : timeline.steps.length === 0 ? (
        <div className="mx-auto max-w-3xl px-8 py-16">
          <Empty
            title="这次任务没有留下调用记录"
            hint={
              <>
                流水线的每一次模型调用、搜索、抓取都会记一条 span。
                一条都没有，通常意味着任务还没开始跑，或者跑的时候没绑 tracer。
              </>
            }
          />
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(0,360px)]">
          {/* ---- 左：回放在上，span 树在下 ---- */}
          <div className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] border-r border-line">
            <ReplayBar
              frame={frame}
              timeline={timeline}
              cursor={cursor}
              onCursor={setCursor}
              firstFetch={firstFetch}
            />

            <Panel
              title="调用轨迹"
              aside={
                <span className="font-mono text-[11px] text-fg-faint">
                  {formatInt(summary?.spanCount ?? timeline.steps.length)} 条
                </span>
              }
              scroll
            >
              <SpanTree
                nodes={roots ?? []}
                depth={0}
                stepIndexById={stepIndexById}
                activeSpanId={frame.step?.span.spanId ?? ''}
                onPick={setCursor}
              />
              {/* 当前数据里这棵树是一片森林（没有一行有 parent_id），
                  所以下面这段说明是给"看不到缩进"的读者一个交代，
                  而不是报错。它同时预告了：一旦真有嵌套，缩进就会出现。 */}
              {roots?.every((node) => (node.children ?? []).length === 0) && (
                <p className="mt-2 border-t border-line pt-2 text-[11px] leading-relaxed text-fg-faint">
                  这次运行的 span 全部是平级的——系统里没有一处把
                  <code className="mx-1 rounded bg-raised px-1 font-mono">span</code>
                  嵌在另一处里面，所以取父 span 的那个绑定从来没取到过东西。
                  左边不显示缩进是数据如此，不是渲染丢了层级。
                </p>
              )}
            </Panel>
          </div>

          {/* ---- 右：证据对照 ---- */}
          <EvidencePanel
            frame={frame}
            evidenceByHost={evidenceByHost}
            hasReport={Boolean(reportId)}
            reportLoading={report.loading}
            reportError={report.error}
            totalEvidences={totalEvidences}
            matchedEvidences={matchedEvidences}
          />
        </div>
      )}
    </div>
  )
}

// ============================================================
// 回放条
// ============================================================

function ReplayBar({
  frame,
  timeline,
  cursor,
  onCursor,
  firstFetch,
}: {
  frame: ReplayFrame
  timeline: ReplayTimeline
  cursor: number
  onCursor: (value: number) => void
  firstFetch: number | null
}) {
  const step = frame.step
  const last = timeline.steps.length - 1
  // 第一条件为真的 fetch 在整条时间线上的位置，用来说"前 47% 都没在抓取"。
  const fetchPercent =
    firstFetch !== null && last > 0 ? Math.round((firstFetch / last) * 100) : null

  return (
    <Panel
      title="回放"
      aside={
        <span className="font-mono text-[11px] text-fg-faint">
          {stepLabel(cursor)} / 共 {formatInt(timeline.steps.length)} 步
        </span>
      }
    >
      <input
        type="range"
        min={0}
        max={last}
        step={1}
        value={Math.min(cursor, last)}
        onChange={(event) => onCursor(Number(event.target.value))}
        aria-label="回放游标"
        className="w-full accent-[var(--color-brand)]"
      />
      <p className="mt-1 text-[11px] text-fg-faint">
        拖动走完这次运行；聚焦滑杆后可以用 ← → 逐条走。
      </p>

      {firstFetch !== null && (
        <p className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-fg-faint">
          <span>
            第 {formatInt(firstFetch + 1)} 步（{fetchPercent}%）才有第一次抓取，
            前面都是需求解析、专家调度与检索。
          </span>
          <button
            type="button"
            onClick={() => onCursor(firstFetch)}
            className="rounded border border-line px-1.5 py-0.5 text-fg-muted hover:border-line-strong hover:text-fg"
          >
            跳到那一步
          </button>
        </p>
      )}

      {step ? (
        <div className="mt-2 border-t border-line pt-2">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="rounded bg-raised px-1.5 py-0.5 text-[11px] text-fg-muted">
              {SPAN_KIND_LABEL[step.span.kind]}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs text-fg" title={step.span.name}>
              {step.span.purpose || step.span.name}
            </span>
            <span className="font-mono text-[11px] text-fg-faint">
              起点 +{formatOffset(step.offsetMs)}
            </span>
          </div>

          <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px] text-fg-faint">
            <span className="font-mono">{step.span.spanId}</span>
            {step.span.parentId && (
              <span className="font-mono" title="父 span。当前数据里不会出现">
                ↳ {step.span.parentId}
              </span>
            )}
            <span>{formatMs(step.span.durationMs)}</span>
            {step.span.costUsd > 0 && <span>{formatCost(step.span.costUsd)}</span>}
            {step.span.totalTokens > 0 && (
              <span title="命中前缀缓存的部分是子集，不是增量">
                {formatInt(step.span.totalTokens)} token
                {step.span.cachedPromptTokens > 0 &&
                  `（命中 ${formatInt(step.span.cachedPromptTokens)}）`}
              </span>
            )}
            {step.span.model && <span className="font-mono">{step.span.model}</span>}
            <span className={STATUS_STYLE[step.span.status]}>
              {STATUS_LABEL[step.span.status]}
            </span>
          </div>

          {step.span.error && (
            <p className="mt-1 break-words text-[11px] text-danger">{step.span.error}</p>
          )}

          {/* 祖先链。当前数据里恒为空——递归写在这儿是为了真有嵌套时
              不会"少显示一层"，而不是现在就指望看得见。 */}
          {step.ancestors.length > 0 && (
            <p className="mt-1 font-mono text-[11px] text-fg-faint">
              属于：{step.ancestors.map((span) => span.purpose || span.name).join(' › ')}
            </p>
          )}

          {frame.hosts.length > 0 && (
            <p className="mt-2 flex flex-wrap items-center gap-1 border-t border-line pt-2">
              <span className="text-[11px] text-fg-faint">
                到这一步碰到过 {formatInt(frame.hosts.length)} 个主机：
              </span>
              {frame.hosts.map((host) => (
                <span
                  key={host}
                  className={[
                    'rounded px-1.5 py-0.5 font-mono text-[11px]',
                    frame.freshHosts.includes(host)
                      ? 'bg-brand/15 text-brand'
                      : 'bg-raised text-fg-muted',
                  ].join(' ')}
                  title={frame.freshHosts.includes(host) ? '这一步新碰到的主机' : undefined}
                >
                  {host}
                </span>
              ))}
            </p>
          )}
        </div>
      ) : null}

      <ByKindTable timeline={timeline} />
    </Panel>
  )
}

/**
 * 按类型的耗时与花费。**用的是整条时间线的 span，不是到游标为止的**——
 * 这个表的职责是"这次运行的力气花在哪一类调用上"，与滑杆停在哪无关。
 */
function ByKindTable({ timeline }: { timeline: ReplayTimeline }) {
  const buckets = new Map<string, { count: number; ms: number; cost: number }>()
  for (const step of timeline.steps) {
    const bucket = buckets.get(step.span.kind) ?? { count: 0, ms: 0, cost: 0 }
    bucket.count += 1
    bucket.ms += step.span.durationMs
    bucket.cost += step.span.costUsd
    buckets.set(step.span.kind, bucket)
  }
  if (buckets.size === 0) return null
  const maxMs = Math.max(...[...buckets.values()].map((bucket) => bucket.ms))

  return (
    <div className="mt-2 border-t border-line pt-2">
      <p className="mb-1 text-[11px] text-fg-faint">按类型（整条时间线）</p>
      <ul className="space-y-1">
        {[...buckets.entries()].map(([kind, bucket]) => (
          <li key={kind}>
            <div className="flex items-baseline justify-between gap-2 text-[11px]">
              <span className="text-fg-muted">
                {SPAN_KIND_LABEL[kind as keyof typeof SPAN_KIND_LABEL] ?? kind}
                <span className="ml-1 text-fg-faint">×{bucket.count}</span>
              </span>
              <span className="font-mono text-fg-faint">
                {formatCost(bucket.cost)}
                <span className="ml-2">{formatMs(bucket.ms)}</span>
              </span>
            </div>
            {/* 条长按最长的那一类归一。这是**相对**长度，所以旁边必须带数字，
                否则读者会把条长当成绝对值去比两次运行。 */}
            <div className="mt-0.5 h-0.5 overflow-hidden rounded-full bg-raised">
              <div
                className="h-full rounded-full bg-brand/60"
                style={{ width: `${maxMs > 0 ? (bucket.ms / maxMs) * 100 : 0}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ============================================================
// span 树
// ============================================================

/**
 * 递归渲染 span 树。每一行左边印着它**在时间线上的步号**——
 * 这一列是整棵树与滑杆之间唯一的联系，也是唯一能证明"两个顺序当前一致"
 * 的东西（不一致时它会跳号，而不是悄悄错位）。
 */
function SpanTree({
  nodes,
  depth,
  stepIndexById,
  activeSpanId,
  onPick,
}: {
  nodes: SpanNode[]
  depth: number
  stepIndexById: Map<string, number>
  activeSpanId: string
  onPick: (index: number) => void
}) {
  return (
    <ul className={depth > 0 ? 'ml-2 border-l border-line pl-2' : ''}>
      {nodes.map((node) => {
        const index = stepIndexById.get(node.spanId)
        const active = node.spanId !== '' && node.spanId === activeSpanId
        const children = node.children ?? []
        return (
          <li key={node.spanId || `${depth}-${node.name}`}>
            <button
              type="button"
              disabled={index === undefined}
              onClick={() => index !== undefined && onPick(index)}
              title={node.error || node.purpose || node.name}
              className={[
                'flex w-full items-baseline gap-2 rounded px-1 py-0.5 text-left text-[11px]',
                active ? 'bg-brand/10' : 'hover:bg-raised',
                index === undefined ? 'cursor-default' : '',
              ].join(' ')}
            >
              <span className="w-12 shrink-0 text-right font-mono text-[10px] text-fg-faint">
                {index === undefined ? '—' : `#${index + 1}`}
              </span>
              <span className="w-8 shrink-0 text-fg-muted">
                {SPAN_KIND_LABEL[node.kind] ?? node.kind}
              </span>
              <span className="min-w-0 flex-1 truncate text-fg">
                {node.purpose || node.name}
              </span>
              <span
                className={['shrink-0 font-mono text-[10px]', STATUS_STYLE[node.status]].join(' ')}
              >
                {formatInt(node.durationMs)}ms
              </span>
            </button>
            {children.length > 0 && (
              <SpanTree
                nodes={children}
                depth={depth + 1}
                stepIndexById={stepIndexById}
                activeSpanId={activeSpanId}
                onPick={onPick}
              />
            )}
          </li>
        )
      })}
    </ul>
  )
}

// ============================================================
// 证据对照
// ============================================================

function EvidencePanel({
  frame,
  evidenceByHost,
  hasReport,
  reportLoading,
  reportError,
  totalEvidences,
  /**
   * **能被配对上的证据总数**（= 走完全程那一帧的 `reached`）。
   *
   * 由调用方算好传进来，而不是在这里写 `evidenceByHost` 的键总数：
   * 那是**第二个定义**——它把"证据数与主机名规则一致"这件事又实现了一遍，
   * 而两处一旦不一致，分母会比分子大，进度永远到不了 100%，
   * 页面上看不出来是哪一边错了。
   */
  matchedEvidences,
}: {
  frame: ReplayFrame
  evidenceByHost: Map<string, ReportEvidence[]>
  hasReport: boolean
  reportLoading: boolean
  reportError: Error | null
  totalEvidences: number
  matchedEvidences: number
}) {
  // 域名对不上任何 `fetch` span 的证据：它们只有搜索摘要，没有抓取记录。
  // 这一条必须说出来——不说的话，读者会以为右边少显示了东西。
  const unmatched = Math.max(0, totalEvidences - matchedEvidences)
  const matchPercent =
    totalEvidences > 0 ? Math.round((matchedEvidences / totalEvidences) * 100) : 0

  return (
    <Panel
      title="证据对照"
      aside={
        hasReport && !reportLoading ? (
          <span className="font-mono text-[11px] text-fg-faint">
            {formatInt(frame.reached)} / {formatInt(matchedEvidences)}
          </span>
        ) : null
      }
      scroll
    >
      {!hasReport ? (
        <Empty
          title="没有带报告 id，看不到证据对照"
          hint={
            <>
              左栏仍然可以看到每一步碰到了哪些主机。要联动证据，
              地址上需要带 <code className="rounded bg-raised px-1 font-mono">?report=&lt;报告 id&gt;</code>
              ——从报告页顶栏的「看它怎么跑的」进来就会带上。
            </>
          }
        />
      ) : reportLoading ? (
        <Loading what="证据" />
      ) : reportError ? (
        <ErrorNote error={reportError} />
      ) : (
        <>
          <p className="mb-2 text-[11px] leading-relaxed text-fg-faint">
            这里的联动是
            <span className="text-fg-muted">按域名配对</span>
            的：<code className="rounded bg-raised px-1 font-mono">fetch</code> span 的
            域名与证据 URL 的主机名对齐，
            <span className="text-fg-muted">不是引用级联动</span>
            ——span 里根本没有 evidence id。所以面板跟着游标
            <span className="text-fg-muted">只增不减</span>
            ，而不是"每一步只剩这一步的"。
          </p>

          {frame.hosts.length === 0 ? (
            <p className="py-6 text-center text-xs text-fg-faint">
              还没走到任何一次抓取。
            </p>
          ) : (
            <ul className="space-y-4">
              {frame.hosts.map((host) => {
                const rows = evidenceByHost.get(host) ?? []
                const fresh = frame.freshHosts.includes(host)
                return (
                  <li key={host}>
                    <div className="flex items-baseline gap-2 border-b border-line pb-1">
                      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg">
                        {host}
                      </span>
                      {fresh && (
                        <span className="shrink-0 rounded bg-brand/15 px-1 text-[10px] text-brand">
                          新
                        </span>
                      )}
                      <span className="shrink-0 font-mono text-[11px] text-fg-faint">
                        {rows.length > 0 ? `${formatInt(rows.length)} 条` : '抓取无结果'}
                      </span>
                    </div>
                    <ul className="mt-1 space-y-1.5">
                      {rows.slice(0, 4).map((evidence) => (
                        <li key={evidence.evidenceId} className="flex items-start gap-2">
                          <span
                            className={[
                              'shrink-0 rounded px-1 py-0.5 font-mono text-[10px]',
                              CREDIBILITY_TIER_STYLE[credibilityTier(evidence.credibility)],
                            ].join(' ')}
                            title="可信度 0–100。分档阈值与后端 evidence/credibility.py 一致"
                          >
                            {evidence.credibility}
                          </span>
                          <a
                            href={evidence.url}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="min-w-0 flex-1 text-[11px] leading-snug text-fg-muted hover:text-brand"
                            title={`${evidence.title}\n${evidence.url}`}
                          >
                            {evidence.title || evidence.url}
                            {evidence.degraded && (
                              <span className="ml-1 text-warn" title="正文没抓到，只有搜索摘要">
                                · 降级
                              </span>
                            )}
                          </a>
                        </li>
                      ))}
                      {rows.length > 4 && (
                        <li className="text-[10px] text-fg-faint">
                          还有 {formatInt(rows.length - 4)} 条在同一域名下
                        </li>
                      )}
                    </ul>
                  </li>
                )
              })}
            </ul>
          )}

          {unmatched > 0 && (
            <p className="mt-3 border-t border-line pt-2 text-[11px] leading-relaxed text-fg-faint">
              这份报告里还有 {formatInt(unmatched)} 条证据没有对应的抓取记录
              （只有搜索摘要，从未被抓取），它们不属于任何一次抓取，所以不会出现在上面。
            </p>
          )}

          {/* 分子与分母**都从 `replayFrame` 出来**，只是游标不同：分子的终点
              就是分母。这样进度条一定走得到头——拿"全部证据数"当分母的话，
              那几条纯摘要的证据永远追不上，右上角那个分数一辈子不满。 */}
          <p className="mt-2 text-[11px] text-fg-faint">
            走到最后会覆盖 {formatInt(matchedEvidences)} 条
            {totalEvidences > 0 &&
              `（占全部 ${formatInt(totalEvidences)} 条的 ${matchPercent}%）`}
            。
          </p>
        </>
      )}
    </Panel>
  )
}
