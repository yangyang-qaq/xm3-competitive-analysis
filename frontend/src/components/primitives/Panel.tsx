/**
 * 工作台的基本容器。面板多，布局靠它统一。
 *
 * `scroll` 单独一个开关而不是让调用方自己加 `overflow-y-auto`：
 * 一个面板**要么**整体滚动**要么**整体不滚，两样都做会出现
 * "外层滚一下、内层再滚一下"的双滚动条，而这种界面没人愿意用。
 *
 * `flat` 是给**密铺**场景准备的（工作台里六块面板贴在一起）：
 * 每块都带投影时，阴影会互相压在一起，界面看起来是脏的而不是有层次的。
 * 卡片式布局（报告页、文库）用默认值。
 */
import type { ReactNode } from 'react'

export function Panel({
  title,
  aside,
  children,
  scroll = false,
  flat = false,
  className = '',
}: {
  title?: ReactNode
  aside?: ReactNode
  children: ReactNode
  scroll?: boolean
  flat?: boolean
  className?: string
}) {
  return (
    <section
      className={[
        'flex min-h-0 flex-col rounded-card border border-line bg-panel',
        flat ? '' : 'shadow-card',
        className,
      ].join(' ')}
    >
      {title !== undefined && (
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-line px-3 py-2">
          <h2 className="text-xs font-medium tracking-wide text-fg-muted">{title}</h2>
          {aside}
        </header>
      )}
      <div className={['min-h-0 flex-1 px-3 py-2', scroll ? 'overflow-y-auto' : ''].join(' ')}>
        {children}
      </div>
    </section>
  )
}
