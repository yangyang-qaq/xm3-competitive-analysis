/**
 * 流水线的 DAG：每个阶段一个节点，状态实时跟随事件流。
 *
 * **阶段列表从后端来**（`GET /api/pipeline/stages`），不在这里写一份。
 * 写一份的话，后端加一个阶段、前端不显示，而没有任何东西会报错——
 * 这正是契约测试存在的理由，而这里能靠"不写第二份清单"绕开它。
 *
 * 节点状态里 `degraded` 必须是**看得出来的第三态**，不能并进 `done`：
 * "跑完了但不完整"是这套系统的常态之一（降级必须可见）。
 */
import type { PipelineStage } from '../../lib/api'
import type { StageId, StageState } from '../../types/domain'

const STATE_STYLE: Record<StageState, { ring: string; dot: string; text: string; label: string }> = {
  pending: { ring: 'border-line', dot: 'bg-fg-faint', text: 'text-fg-faint', label: '等待' },
  running: { ring: 'border-brand', dot: 'bg-brand animate-pulse', text: 'text-brand', label: '进行中' },
  done: { ring: 'border-ok/50', dot: 'bg-ok', text: 'text-ok', label: '完成' },
  degraded: { ring: 'border-warn/60', dot: 'bg-warn', text: 'text-warn', label: '降级' },
  error: { ring: 'border-danger', dot: 'bg-danger', text: 'text-danger', label: '失败' },
}

export function StageDag({
  stages,
  nodes,
  stage,
  round,
  onSelect,
  selected,
}: {
  stages: PipelineStage[]
  nodes: Partial<Record<StageId, StageState>>
  stage: StageId | ''
  round: number
  onSelect?: (stage: StageId) => void
  selected?: StageId | ''
}) {
  if (stages.length === 0) {
    return <p className="py-4 text-center text-xs text-fg-faint">阶段列表还没加载出来</p>
  }

  return (
    <ol className="flex flex-col gap-1">
      {stages.map((item) => {
        // 没被事件更新过的节点是 `pending`。**不是 `idle`**：
        // 后端只有 `pending` 这一个词，两处各起一个名字会让
        // "DAG 的初始状态"有两处定义。
        const state: StageState = nodes[item.key] ?? 'pending'
        const style = STATE_STYLE[state]
        const isCurrent = stage === item.key
        return (
          <li key={item.key}>
            <button
              type="button"
              onClick={() => onSelect?.(item.key)}
              className={[
                'flex w-full items-center gap-2.5 rounded border px-2.5 py-1.5 text-left transition-colors',
                style.ring,
                isCurrent ? 'bg-raised' : 'bg-transparent hover:bg-raised/60',
                selected === item.key ? 'ring-1 ring-brand/40' : '',
              ].join(' ')}
            >
              <span className={['size-1.5 shrink-0 rounded-full', style.dot].join(' ')} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-medium text-fg">{item.label}</span>
                {isCurrent && item.key === 'rework' && round > 0 && (
                  <span className="block text-[11px] text-fg-faint">第 {round} 轮</span>
                )}
              </span>
              <span className={['shrink-0 text-[11px]', style.text].join(' ')}>{style.label}</span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}
