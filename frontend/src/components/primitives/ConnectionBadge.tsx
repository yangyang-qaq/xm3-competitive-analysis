/**
 * 这条网线现在是什么状态。**与任务状态无关**——一个跑得好好的任务
 * 也可以正在重连，而一个已经跑完的任务的连接会正常关闭。
 *
 * 把这两件事混成一个指示灯，会让"重连中"看起来像"任务出问题了"。
 */
import type { ConnectionState } from '../../store/taskStore'

const STYLE: Record<ConnectionState, { className: string; label: string }> = {
  idle: { className: 'bg-fg-faint/15 text-fg-faint', label: '未连接' },
  connecting: { className: 'bg-warn/15 text-warn', label: '连接中' },
  live: { className: 'bg-ok/15 text-ok', label: '实时' },
  reconnecting: { className: 'bg-warn/15 text-warn', label: '重连中' },
  // 刻意不是绿的：任务结束不等于连接"健康"，只等于它按预期收完了。
  // 用绿色会让人以为还有后续推送在路上。
  ended: { className: 'bg-fg-faint/15 text-fg-faint', label: '已结束' },
  fatal: { className: 'bg-danger/15 text-danger', label: '连接已断开' },
}

export function ConnectionBadge({
  connection,
  note,
}: {
  connection: ConnectionState
  note: string
}) {
  const style = STYLE[connection]
  return (
    <span
      className={['rounded-full px-2 py-0.5 text-[11px] font-medium', style.className].join(' ')}
      title={note || undefined}
    >
      {style.label}
      {/* `fatal` 时把原因显示出来。只显示"断开"的话，用户不知道
          是任务没了、还是浏览器放弃了重连——这两种的下一步完全不同。 */}
      {connection === 'fatal' && note && <span className="ml-1.5 font-normal">{note}</span>}
    </span>
  )
}
