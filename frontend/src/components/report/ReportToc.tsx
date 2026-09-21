/**
 * 左侧目录。跟着滚动高亮当前那一节。
 *
 * 用 `IntersectionObserver` 而不是监听 `scroll` 算位置
 * ------------------------------------------------
 * `scroll` 那条路的代价是：每一次滚动都要对所有章节调
 * `getBoundingClientRect()`，而 `getBoundingClientRect()` 会强制布局。
 * 一份报告有六到十二节，滚动时就是每帧十几次同步布局——在长报告上
 * 这就是那种"页面能滚，但滚起来发涩"的手感。
 *
 * `IntersectionObserver` 把这件事交给浏览器，回调只在**跨越阈值**时触发。
 *
 * `rootMargin` 的那个负数不是调参
 * ----------------------------
 * 视口最上面那 120px 里没有正文：页头占掉约 56px（它在滚动容器外面，
 * 见 `ReportSection.tsx`），剩下的是"标题刚刚擦过滚动区上沿"的那一段。
 * 不排除掉的话，一节刚露头就算"进入视口"，高亮会比读者的眼睛早半屏跳过去。
 *
 * 所以判定区是 `[120px, 视口高 × 30%]` 这一条带：只留底部 30%，
 * 效果是"读到哪一节，哪一节亮"。**这是一个手调的数**，
 * 页头高度或正文字号变了要跟着看一眼。
 *
 * jsdom 里没有 `IntersectionObserver`，所以先判存在性再建——
 * 组件测试（渲染整页的那种）会走到这个分支。
 */
import { useEffect, useState } from 'react'

import type { ReportSection } from '../../types/report'

export interface ReportTocProps {
  sections: ReportSection[]
  /** 章节之外还有结构化块与附录，它们的锚点也列在这里 */
  extraAnchors?: Array<{ id: string; label: string; count?: number }>
}

export function ReportToc({ sections, extraAnchors = [] }: ReportTocProps) {
  const [active, setActive] = useState<string>(sections[0]?.key ?? '')

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return

    const targets = [
      ...sections.map((section) => section.key),
      ...extraAnchors.map((anchor) => anchor.id),
    ]
      .map((id) => document.getElementById(id))
      .filter((element): element is HTMLElement => element !== null)

    if (targets.length === 0) return

    const observer = new IntersectionObserver(
      (entries) => {
        // 一次可能有多条同时进入判定区。取**最靠上**的那一条：
        // 读者关心的是"我现在读到哪儿了"，而判定区里最靠上的那个
        // 就是刚读完/正在读的那一节。
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top)
        const first = visible[0]
        if (first) setActive(first.target.id)
      },
      { rootMargin: '-120px 0px -70% 0px', threshold: 0 },
    )

    for (const target of targets) observer.observe(target)
    return () => observer.disconnect()
  }, [sections, extraAnchors])

  function jump(id: string) {
    // `scrollIntoView` 而不是 `location.hash = ...`：后者会在历史里
    // 留下一条记录，读者的"后退"变成了在目录里往回跳，而不是回到上一页。
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <nav className="flex flex-col gap-0.5">
      <p className="px-2 pb-1.5 text-[11px] font-medium tracking-wide text-fg-faint">目录</p>
      {sections.map((section) => (
        <button
          key={section.key}
          type="button"
          // `data-anchor` 把"这条点的是哪个 id"写进 DOM，好让测试能断言
          // **每一条目录项都对应一个真的存在的元素**。这条不变量在 jsdom 里
          // 没有别的方式可查：按钮的文字是给人看的，跳转走的是 JS，
          // 而"点了没反应"的目录项在渲染结果里和不死的一模一样。
          data-anchor={section.key}
          onClick={() => jump(section.key)}
          className={[
            'rounded-lg px-2 py-1 text-left text-[12px] transition-colors',
            active === section.key
              ? 'bg-brand/12 font-medium text-brand'
              : 'text-fg-muted hover:bg-raised hover:text-fg',
          ].join(' ')}
        >
          {section.title}
          {section.reworked && (
            <span className="ml-1.5 text-[10px] text-fg-faint" title="这一节被深化改写过">
              ·改过
            </span>
          )}
        </button>
      ))}

      {extraAnchors.length > 0 && (
        <>
          <p className="px-2 pb-1.5 pt-3 text-[11px] font-medium tracking-wide text-fg-faint">
            其他
          </p>
          {extraAnchors.map((anchor) => (
            <button
              key={anchor.id}
              type="button"
              data-anchor={anchor.id}
              onClick={() => jump(anchor.id)}
              className={[
                'rounded-lg px-2 py-1 text-left text-[12px] transition-colors',
                active === anchor.id
                  ? 'bg-brand/12 font-medium text-brand'
                  : 'text-fg-muted hover:bg-raised hover:text-fg',
              ].join(' ')}
            >
              {anchor.label}
              {anchor.count !== undefined && (
                <span className="ml-1.5 text-[10px] text-fg-faint tabular">{anchor.count}</span>
              )}
            </button>
          ))}
        </>
      )}
    </nav>
  )
}
