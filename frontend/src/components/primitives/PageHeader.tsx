/**
 * 页头。四个页面共用一份，不是各写各的。
 *
 * 理由很实际：标题的字号、和正文的间距、`sub` 那一行的颜色，
 * 四处各写一遍就会在第三次改动时漂开，而"几个页面的标题不一样大"
 * 是那种谁都说不上哪里不对、但看起来就是不对劲的问题。
 */
import type { ReactNode } from 'react'

export function PageHeader({
  title,
  sub,
  aside,
}: {
  title: string
  /** 一句话说清这页是干嘛的。**不是装饰**——四个入口的名字彼此很近，
   *  这行字是用户判断"我点对了没有"的唯一依据 */
  sub?: ReactNode
  aside?: ReactNode
}) {
  return (
    <header className="mb-6 flex items-start justify-between gap-6">
      <div className="min-w-0">
        <h1 className="text-[22px] font-semibold tracking-tight text-fg">{title}</h1>
        {sub !== undefined && (
          <p className="mt-1.5 text-[13px] leading-relaxed text-fg-muted">{sub}</p>
        )}
      </div>
      {aside !== undefined && <div className="shrink-0">{aside}</div>}
    </header>
  )
}
