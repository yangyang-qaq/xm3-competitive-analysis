/**
 * 悬浮 trace 面板：此刻花了多少钱、多少 token、慢在哪一步。
 *
 * **数字全部从 span 流里来，前端不另算一份。** 例如"最慢的一条"是
 * `Math.max(durationMs)`，而不是"端到端耗时"——这两个数差得很远，
 * 而后者不告诉你去优化什么。
 *
 * 缓存的命中量与未命中量单独显示：命中价与未命中价差 50 倍，
 * 一个笼统的"总 token"说明不了"为什么这么便宜"。
 */
import { SPAN_KIND_LABEL, formatCost, formatInt } from '../../lib/format'
import type { SpanKind, TraceSpan } from '../../types/domain'

export function TracePanel({ spans }: { spans: TraceSpan[] }) {
  if (spans.length === 0) {
    return <p className="py-4 text-center text-xs text-fg-faint">还没有调用记录。</p>
  }

  const byKind = new Map<SpanKind, { count: number; durationMs: number; costUsd: number }>()
  let cost = 0
  let tokens = 0
  let cached = 0
  // `| null` 而不是拿 `spans[0]` 打头：上面那句"空数组就返回"在类型上
  // 收窄不了，而用 `!` 断言等于把这件事交给运行时。多一个 null 判断，
  // 换编译器真的帮你看着这个数组。
  let slowest: TraceSpan | null = null

  for (const span of spans) {
    cost += span.costUsd
    tokens += span.totalTokens
    cached += span.cachedPromptTokens
    if (slowest === null || span.durationMs > slowest.durationMs) slowest = span
    const bucket = byKind.get(span.kind) ?? { count: 0, durationMs: 0, costUsd: 0 }
    bucket.count += 1
    bucket.durationMs += span.durationMs
    bucket.costUsd += span.costUsd
    byKind.set(span.kind, bucket)
  }

  return (
    <div className="space-y-2 text-xs">
      <div className="grid grid-cols-2 gap-x-3 gap-y-1">
        <Field label="累计成本" value={formatCost(cost)} />
        <Field label="调用次数" value={formatInt(spans.length)} />
        <Field label="总 token" value={formatInt(tokens)} />
        <Field
          label="其中命中缓存"
          value={cached > 0 ? formatInt(cached) : '—'}
          hint="前缀缓存命中的部分。不区分缓存的 provider 恒为 0"
        />
      </div>

      <div className="border-t border-line pt-2">
        <p className="mb-1 text-[11px] text-fg-faint">按类型</p>
        <ul className="space-y-0.5">
          {[...byKind.entries()].map(([kind, bucket]) => (
            <li key={kind} className="flex items-baseline justify-between gap-2">
              <span className="text-fg-muted">
                {SPAN_KIND_LABEL[kind]}
                <span className="ml-1 text-fg-faint">×{bucket.count}</span>
              </span>
              <span className="tabular font-mono text-fg-faint">
                {formatCost(bucket.costUsd)}
                <span className="ml-2">{formatInt(bucket.durationMs)}ms</span>
              </span>
            </li>
          ))}
        </ul>
      </div>

      {/* `spans` 非空时 `slowest` 必定有值——但那是**推理**，不是编译器
          知道的事。这里多一个判断，代价是一次空比较；换成 `!` 断言的话，
          将来谁把上面那句"空数组就返回"删掉，这里就变成运行时崩溃。 */}
      {slowest && (
        <div className="border-t border-line pt-2">
          <p className="mb-1 text-[11px] text-fg-faint">最慢的一次</p>
          <p className="truncate text-fg-muted" title={slowest.name}>
            {slowest.purpose || slowest.name}
          </p>
          <p className="tabular font-mono text-[11px] text-fg-faint">
            {formatInt(slowest.durationMs)}ms · {SPAN_KIND_LABEL[slowest.kind]}
            {slowest.model && ` · ${slowest.model}`}
          </p>
        </div>
      )}
    </div>
  )
}

function Field({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div title={hint}>
      <p className="text-[11px] text-fg-faint">{label}</p>
      <p className="tabular font-mono text-sm text-fg">{value}</p>
    </div>
  )
}
