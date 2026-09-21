/**
 * 工作台：跑到一半的时候看的地方。
 *
 * 它要回答的是三个问题，三栏就是照这三个问题分的：
 *   左  —— **它跑到哪一步了**（DAG + 进度轨）
 *   中  —— **它现在在想什么**（思维流）
 *   右  —— **它拿到了什么**（证据流 / 调用轨迹）
 *
 * 页面本身**不持有任务状态**：全部从 `useTaskStore` 读，由 SSE 事件推进。
 * 用一个局部 `useState` 存一份的话，重连之后那两份就会不一致，
 * 而界面上分不出哪一份是对的。
 */
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { EvidenceStream } from '../components/evidence/EvidenceStream'
import { StageDag } from '../components/pipeline/StageDag'
import { ConnectionBadge } from '../components/primitives/ConnectionBadge'
import { Panel } from '../components/primitives/Panel'
import { ThoughtStream } from '../components/stream/ThoughtStream'
import { TracePanel } from '../components/trace/TracePanel'
import { useAsync } from '../hooks/useAsync'
import { useTaskStream } from '../hooks/useTaskStream'
import { api } from '../lib/api'
import { formatCost, formatInt, formatPercent } from '../lib/format'
import { useTaskStore } from '../store/taskStore'
import type { TaskStatus } from '../types/domain'

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: '排队中',
  running: '调研中',
  awaiting_clarify: '等待回答',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const STATUS_STYLE: Record<TaskStatus, string> = {
  pending: 'text-fg-muted',
  running: 'text-brand',
  awaiting_clarify: 'text-warn',
  done: 'text-ok',
  failed: 'text-danger',
  cancelled: 'text-fg-faint',
}

export default function WorkspacePage() {
  const { taskId = '' } = useParams()
  const { load, error, connection, connectionNote } = useTaskStream(taskId)
  const { data: stageData } = useAsync(() => api.pipelineStages())

  const task = useTaskStore((state) => state.task)
  const subject = useTaskStore((state) => state.subject)
  const brands = useTaskStore((state) => state.brands)
  const query = useTaskStore((state) => state.query)

  const [rightTab, setRightTab] = useState<'evidence' | 'trace'>('evidence')

  if (load === 'notFound') {
    return (
      <Fallback title="任务不存在" detail={`找不到 ${taskId}。它可能被删掉了，或者 id 拼错了。`} />
    )
  }
  if (load === 'error') {
    return <Fallback title="读不到这个任务" detail={error} />
  }

  return (
    <div className="flex h-screen flex-col gap-2 p-3">
      {/* ---- 顶栏 ---- */}
      <header className="shrink-0 rounded-lg border border-line bg-panel px-3 py-2">
        <div className="flex items-center gap-3">
          <Link to="/" className="shrink-0 font-mono text-sm text-brand hover:underline">
            xm3
          </Link>
          <h1 className="min-w-0 flex-1 truncate text-sm text-fg" title={query || subject}>
            {subject || query || '（尚未解析出调研对象）'}
          </h1>
          <span className={['shrink-0 text-xs', STATUS_STYLE[task.status]].join(' ')}>
            {STATUS_LABEL[task.status]}
          </span>
          <ConnectionBadge connection={connection} note={connectionNote} />
        </div>

        <div className="mt-2 flex items-center gap-3">
          <div className="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-raised">
            <div
              className="h-full rounded-full bg-brand transition-[width] duration-500"
              style={{ width: `${Math.round(task.progress * 100)}%` }}
            />
          </div>
          <span className="tabular shrink-0 font-mono text-[11px] text-fg-faint">
            {formatPercent(task.progress)}
          </span>
          {task.stageLabel && (
            <span className="shrink-0 text-[11px] text-fg-muted">{task.stageLabel}</span>
          )}
        </div>

        {brands.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {brands.map((brand) => (
              <span
                key={brand}
                className="rounded bg-raised px-1.5 py-0.5 text-[11px] text-fg-muted"
              >
                {brand}
              </span>
            ))}
          </div>
        )}

        {/* 降级与失败都是**必须可见**的：一份静默降级的报告看起来
            和完整报告一样自信，而读者无从知道某一块其实没做成。 */}
        {task.degraded.length > 0 && (
          <ul className="mt-1.5 space-y-0.5">
            {task.degraded.map((item) => (
              <li key={item} className="text-[11px] text-warn">
                降级 · {item}
              </li>
            ))}
          </ul>
        )}
        {task.error && <p className="mt-1.5 text-[11px] text-danger">失败 · {task.error}</p>}
        {/* `problems` 与 `degraded` 是两件事：前者是出库校验发现的问题
            （报告仍然落库了），后者是某一块没做成。混在一起显示会让
            "报告有问题"和"报告不完整"变成一个意思。 */}
        {task.problems.length > 0 && (
          <ul className="mt-1.5 space-y-0.5">
            {task.problems.map((item) => (
              <li key={item} className="text-[11px] text-fg-muted">
                出库校验 · {item}
              </li>
            ))}
          </ul>
        )}

        {task.status === 'awaiting_clarify' && (
          <p className="mt-2 text-xs text-warn">
            这个需求信息不够，正在等你回答。
            <Link to={`/clarify/${taskId}`} className="ml-1 text-brand hover:underline">
              去回答
            </Link>
          </p>
        )}
        {task.status === 'done' && task.reportId && (
          <p className="mt-2 text-xs text-ok">
            报告已生成。
            <Link to={`/report/${task.reportId}`} className="ml-1 text-brand hover:underline">
              去看报告
            </Link>
          </p>
        )}
      </header>

      {/* ---- 三栏 ---- */}
      <div className="grid min-h-0 flex-1 grid-cols-[200px_minmax(0,1.4fr)_minmax(0,1fr)] gap-2">
        <Panel title="流水线" scroll>
          <StageDag
            stages={stageData?.stages ?? []}
            nodes={task.nodes}
            stage={task.stage}
            round={task.round}
          />
          {task.metrics && (
            <dl className="mt-3 space-y-1 border-t border-line pt-2 text-[11px]">
              <Row label="证据" value={formatInt(task.metrics.evidenceCount)} />
              <Row label="独立信源" value={formatInt(task.metrics.independentDomains)} />
              <Row label="耗时" value={`${formatInt(task.metrics.elapsedSeconds)}s`} />
              <Row label="成本" value={formatCost(task.metrics.costUsd)} />
            </dl>
          )}
        </Panel>

        <Panel
          title="专家思维流"
          aside={
            <span className="tabular font-mono text-[11px] text-fg-faint">
              {formatInt(task.thoughts.length)}
            </span>
          }
          className="overflow-hidden"
        >
          <ThoughtStream thoughts={task.thoughts} />
        </Panel>

        <Panel
          title={
            <span className="flex gap-3">
              <TabButton active={rightTab === 'evidence'} onClick={() => setRightTab('evidence')}>
                证据 {formatInt(task.evidences.length)}
              </TabButton>
              <TabButton active={rightTab === 'trace'} onClick={() => setRightTab('trace')}>
                调用轨迹 {formatInt(task.spans.length)}
              </TabButton>
            </span>
          }
          className="overflow-hidden"
        >
          {rightTab === 'evidence' ? (
            <EvidenceStream evidences={task.evidences} />
          ) : (
            <div className="h-full overflow-y-auto">
              {/* 这个面板是**概览**（花了多少、慢在哪一步），要一步步走着看
                  得去回放页。跑完之前没有报告 id，所以链接不带 `?report=`——
                  回放页会说明右栏为什么是空的，而不是显示 0 条。 */}
              <p className="mb-2 border-b border-line pb-2">
                <Link
                  to={
                    task.reportId
                      ? `/trace/${taskId}?report=${task.reportId}`
                      : `/trace/${taskId}`
                  }
                  className="text-[11px] text-brand hover:underline"
                >
                  逐步回放这次运行 →
                </Link>
              </p>
              <TracePanel spans={task.spans} />
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={['transition-colors', active ? 'text-fg' : 'text-fg-faint hover:text-fg-muted'].join(
        ' ',
      )}
    >
      {children}
    </button>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="text-fg-faint">{label}</dt>
      <dd className="tabular font-mono text-fg-muted">{value}</dd>
    </div>
  )
}

function Fallback({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="flex h-screen items-center justify-center px-6">
      <div className="max-w-md text-center">
        <h1 className="text-sm font-medium text-fg">{title}</h1>
        <p className="mt-2 break-words text-xs text-fg-muted">{detail}</p>
        <Link to="/" className="mt-4 inline-block text-xs text-brand hover:underline">
          回到首页
        </Link>
      </div>
    </div>
  )
}
