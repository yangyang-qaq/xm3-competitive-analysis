/**
 * 正文里的一节。
 *
 * `id` 就是 `section.key`——目录的锚点、"深化本节"回填后滚动的位置、
 * 批注的 `sectionKey`，三处用的是同一个字符串。**不能让它们各算各的**：
 * 批注按 `sectionKey` 存、目录按 `id` 跳，一旦两者对不上，
 * 批注会挂到另一节下面，而报告照常渲染。
 *
 * `degraded` 与 `reworked` 是两个不同的徽章
 * ----------------------------------------
 * - `degraded`：**这一节的材料不够**，写法上已经收敛（少下结论、多说明缺口）。
 * - `reworked`：这一节被返工或深化**重写过**。
 *
 * 两个都要显示：前者告诉读者"别拿这节的结论当定论"，后者告诉读者
 * "这一节的措辞和别处不是同一次写出来的"。只显示一个都会漏掉一半信息。
 *
 * `claimIds` 与 `evidenceIds` 是**这一节声明用到的**，不是正文里出现的
 * 引用编号——正文里的 `[证据: EV-x]` 由 `Prose` 自己解析。
 * 两个数不一样是正常的（声明了但没写进正文，也算数）。
 */
import type { ReactNode } from 'react'

import { Prose } from './Prose'
import type { ReportSection as SectionData } from '../../types/report'

export interface ReportSectionProps {
  section: SectionData
  numbers: Map<string, number>
  activeEvidenceId: string | null
  onCite: (evidenceId: string) => void
  /** 批注工具条。由页面传入，这样这一节本身不关心怎么提交 */
  annotator?: ReactNode
}

export function ReportSectionBody({
  section,
  numbers,
  activeEvidenceId,
  onCite,
  annotator,
}: ReportSectionProps) {
  return (
    <section
      id={section.key}
      // `scroll-mt` 是"跳转落点留出的上边距"。
      //
      // **不是**为了抵消页头：页头不在滚动容器里（它是 `h-screen flex-col`
      // 的第一行 `shrink-0`，滚动发生在下面的 `<main overflow-y-auto>`），
      // 所以 `scrollIntoView` 本来就不会把标题藏到它后面。
      // 留这 88px 是因为贴着滚动区上沿落下会显得很挤——尤其是页头就在
      // 正上方、中间只隔一条分割线的时候。
      //
      // 页面上每一条目录项的目标都得有它，`ReportMatrix` 那两处此前漏了
      // （见 `问题记录.md` 问题 32.4）。
      className="scroll-mt-[88px] rounded-card border border-line bg-panel p-5 shadow-card"
    >
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-[15px] font-medium text-fg">{section.title}</h2>

        {section.degraded && (
          <span
            className="rounded bg-warn/12 px-1.5 py-0.5 text-[10px] text-warn"
            title="这一节的材料不足以支撑完整论述"
          >
            材料不足
          </span>
        )}
        {section.reworked && (
          <span
            className="rounded bg-brand/12 px-1.5 py-0.5 text-[10px] text-brand"
            title="这一节被返工或按批注深化重写过"
          >
            已重写
          </span>
        )}

        <span className="ml-auto shrink-0 text-[10px] text-fg-faint tabular">
          {section.evidenceIds.length} 条证据 · {section.claimIds.length} 条论点
        </span>
      </div>

      <div className="mt-3">
        <Prose
          text={section.content}
          numbers={numbers}
          activeEvidenceId={activeEvidenceId}
          onCite={onCite}
        />
      </div>

      {annotator}
    </section>
  )
}
