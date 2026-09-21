/**
 * 报告页的卡片容器。
 *
 * 和 `primitives/Panel` **不是同一个东西**，所以没有合并：
 * 那个是工作台的面板（密铺、题目走 header 横条、`flat` 去投影），
 * 这个报告页的卡片（稀疏排布、有投影、题目在内容里、`p-4` 更松）。
 * 两者的差别是刻意的版面决定，不是同一种东西的两种写法。
 * 合并的话得给 `primitives/Panel` 加三个开关，而它已经被工作台
 * 六块面板用着——改它等于同时改工作台。
 *
 * `id` + `scroll-mt-[88px]` 是给 TOC 定位用的：跳转落点要留出上边距
 * （页头并不在滚动容器里，所以这不是"抵消页头"，理由见
 * `ReportSection.tsx` 的注释）。
 *
 * `count` 只是 `aside` 的糖：右上的条数徽章在七块里长得一模一样，
 * 每处各写一遍 `<span className="text-[11px] tabular text-fg-faint">`
 * 就会在第三次改动时漂开。要放别的东西（比如"18 张"）用 `aside`。
 */
import type { ReactNode } from 'react'

export function ReportPanel({
  id,
  title,
  count,
  aside,
  children,
}: {
  id: string
  title: ReactNode
  /** 右上角的条数。与 `aside` 二选一 */
  count?: number
  aside?: ReactNode
  children: ReactNode
}) {
  return (
    <section
      id={id}
      className="scroll-mt-[88px] rounded-card border border-line bg-panel p-4 shadow-card"
    >
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[13px] font-medium text-fg">{title}</h3>
        {count !== undefined && (
          <span className="shrink-0 text-[11px] tabular text-fg-faint">{count}</span>
        )}
        {aside}
      </div>
      <div className="mt-2.5">{children}</div>
    </section>
  )
}
