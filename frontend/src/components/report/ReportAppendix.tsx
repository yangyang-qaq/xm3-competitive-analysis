/**
 * 附录：证据统计、模块完整度、术语表、图集、专家名单。
 *
 * 附录不是"正文放不下的东西"，是**用来复核正文的东西**。
 * 读者看完一份报告想验证某个数，要能在这里找到它的构成：
 * 证据从哪些来源来（`evidenceStats`）、哪几块是缺的（`completeness`）、
 * "幻觉引用率"到底是什么（`glossary`）。
 *
 * 图集默认折叠
 * ----------
 * `gallery` 里是证据页面带出来的配图，一份报告可能有几十张。
 * 展开的话这一节会比正文还长，而它按定义是**次要材料**。
 * 用 `<details>` 折起来，想看的点开——但**计数要露在外面**，
 * 否则读者不知道里面有东西。
 *
 * 术语表的定义**原样显示，不做二次加工**
 * ----------------------------------
 * 后端那张表里写的是公式（"模型输出中指向不存在证据的引用数 ÷ 模型输出
 * 的引用总数"）。把它改写成一句人话看起来更友好，但那样读者就没法
 * 自己复算了——而"能被读者自己复算"正是这张表存在的理由。
 */
import { Link } from 'react-router-dom'

import { formatPercent, sourceTypeLabel } from '../../lib/format'
import type {
  Completeness,
  EvidenceStats,
  GalleryItem,
  GlossaryEntry,
  TeamRoster,
} from '../../types/report'
import { ReportPanel } from './ReportPanel'

/** 模块状态的中文与配色。`degraded` 记半分，所以它不是"坏"。 */
const STATE_LABEL: Record<string, string> = {
  filled: '完整',
  degraded: '有降级',
  missing: '缺失',
}

const STATE_TONE: Record<string, string> = {
  filled: 'text-ok',
  degraded: 'text-warn',
  missing: 'text-danger',
}

export interface ReportAppendixProps {
  evidenceStats: EvidenceStats | undefined
  completeness: Completeness | undefined
  glossary: GlossaryEntry[] | undefined
  gallery: GalleryItem[] | undefined
  team: TeamRoster | undefined
}

/**
 * 归一化条形。`format` 是把后端原值翻成中文的函数——**由调用方决定**，
 * 因为两张表的取值域不同：来源类型要过 `sourceTypeLabel`，
 * 品牌名是用户数据、不能过任何映射表（真有个竞品叫 `unknown` 就会被改掉）。
 */
function Bars({
  rows,
  format,
}: {
  rows: Array<{ value: string; count: number }>
  format?: (value: string) => string
}) {
  const max = Math.max(...rows.map((row) => row.count), 1)
  return (
    <ul className="flex flex-col gap-1">
      {rows.map((row) => (
        <li key={row.value} className="flex items-center gap-2">
          <span className="w-24 shrink-0 truncate text-[11px] text-fg-muted" title={row.value}>
            {format ? format(row.value) : row.value}
          </span>
          <span className="h-2 flex-1 overflow-hidden rounded-full bg-raised">
            {/* 条形宽度按**最大值**归一，不是按总和。
                按总和归一时，最大的那一项也只有三分之一宽——
                而这张图要说的是"哪一类最多"，不是"占了多少"。 */}
            <span
              className="block h-full rounded-full bg-brand/60"
              style={{ width: `${(row.count / max) * 100}%` }}
            />
          </span>
          <span className="w-8 shrink-0 text-right text-[11px] tabular text-fg">{row.count}</span>
        </li>
      ))}
    </ul>
  )
}

export function ReportAppendix({
  evidenceStats,
  completeness,
  glossary,
  gallery,
  team,
}: ReportAppendixProps) {
  const teamSize =
    (team?.lead?.length ?? 0) +
    (team?.strategists?.length ?? 0) +
    (team?.executors?.length ?? 0)

  return (
    <>
      {/* ---- 证据统计 ---- */}
      {evidenceStats && (
        <ReportPanel id="evidence-stats" title="证据统计">
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
            {evidenceStats.total !== undefined && (
              <span className="text-fg-muted">
                共 <span className="tabular text-fg">{evidenceStats.total}</span> 条
              </span>
            )}
            {evidenceStats.independentDomains !== undefined && (
              <span className="text-fg-muted">
                独立信源 <span className="tabular text-fg">{evidenceStats.independentDomains}</span>
              </span>
            )}
            {evidenceStats.avgCredibility !== undefined && (
              <span className="text-fg-muted">
                平均可信度{' '}
                <span className="tabular text-fg">{evidenceStats.avgCredibility.toFixed(1)}</span>
              </span>
            )}
            {evidenceStats.degraded !== undefined && evidenceStats.degraded > 0 && (
              <span className="text-warn">
                正文没抓到 <span className="tabular">{evidenceStats.degraded}</span> 条
              </span>
            )}
          </div>

          <div className="mt-3 grid gap-4 md:grid-cols-2">
            {(evidenceStats.bySourceType ?? []).length > 0 && (
              <div>
                <p className="mb-1 text-[10.5px] font-medium tracking-wide text-fg-faint">
                  按来源类型
                </p>
                <Bars rows={evidenceStats.bySourceType ?? []} format={sourceTypeLabel} />
              </div>
            )}
            {(evidenceStats.byBrand ?? []).length > 0 && (
              <div>
                <p className="mb-1 text-[10.5px] font-medium tracking-wide text-fg-faint">
                  按品牌
                </p>
                <Bars rows={evidenceStats.byBrand ?? []} />
              </div>
            )}
          </div>
        </ReportPanel>
      )}

      {/* ---- 模块完整度 ---- */}
      {completeness && (
        <ReportPanel
          id="completeness"
          title="模块完整度"
          aside={
            completeness.score !== undefined && (
              <span className="text-[11px] text-fg-faint">
                加权得分 <span className="tabular text-fg">{formatPercent(completeness.score, 0)}</span>
              </span>
            )
          }
        >
          {/* 每个模块的状态**逐条列出来**。只给一个总分的话，
              读者知道"缺了一块"却不知道缺的是哪一块。 */}
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            {Object.entries(completeness.blocks ?? {}).map(([key, state]) => (
              <span key={key} className="flex items-center gap-1.5 text-[11px]">
                <span className={`text-[10px] ${STATE_TONE[state] ?? 'text-fg-faint'}`}>
                  {STATE_LABEL[state] ?? state}
                </span>
                {/* 模块名从 `labels` 里取（后端给的），前端不建映射表 */}
                <span className="text-fg-muted">{completeness.labels?.[key] ?? key}</span>
              </span>
            ))}
          </div>
          <p className="mt-2 text-[10.5px] leading-relaxed text-fg-faint">
            完整度按模块加权：完整记 1 分、有降级记 0.5 分、缺失记 0 分。
            有降级但齐全的报告仍然可用（读者能看到哪几块弱），缺模块的报告会误导。
          </p>
        </ReportPanel>
      )}

      {/* ---- 术语表 ---- */}
      {(glossary ?? []).length > 0 && (
        <ReportPanel id="glossary" title="指标口径">
          <dl className="flex flex-col">
            {(glossary ?? []).map((entry) => (
              <div key={entry.term} className="border-t border-line py-1.5 first:border-t-0">
                <dt className="text-[11.5px] font-medium text-fg">{entry.term}</dt>
                <dd className="mt-0.5 text-[11px] leading-relaxed text-fg-muted">
                  {entry.definition}
                </dd>
              </div>
            ))}
          </dl>
        </ReportPanel>
      )}

      {/* ---- 图集 ---- */}
      {(gallery ?? []).length > 0 && (
        <ReportPanel
          id="gallery"
          title="图集"
          aside={<span className="text-[11px] tabular text-fg-faint">{(gallery ?? []).length} 张</span>}
        >
          <details>
            <summary className="cursor-pointer text-[11.5px] text-fg-muted hover:text-fg">
              展开查看（来自证据页面的配图）
            </summary>
            <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
              {(gallery ?? []).map((item, index) => (
                <figure key={`${item.evidenceId}-${index}`} className="flex flex-col gap-1">
                  {/* 图是**证据页面上的图**，不是我们画的图。所以它指向
                      `sourceUrl`（那条证据），而不是直接链到图片文件——
                      读者要看的是"这张图出现在哪篇文章里"。 */}
                  <a
                    href={item.sourceUrl || item.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="block overflow-hidden rounded-lg border border-line bg-raised"
                  >
                    <img
                      src={item.url}
                      alt={item.alt || item.brand}
                      loading="lazy"
                      className="h-24 w-full object-cover transition-opacity hover:opacity-90"
                    />
                  </a>
                  <figcaption className="truncate text-[10px] text-fg-faint" title={item.siteName}>
                    {item.brand}
                    {item.siteName && ` · ${item.siteName}`}
                  </figcaption>
                </figure>
              ))}
            </div>
          </details>
        </ReportPanel>
      )}

      {/* ---- 专家名单 ---- */}
      {teamSize > 0 && (
        <ReportPanel
          id="team"
          title="参与本次调研的专家"
          aside={<span className="text-[11px] tabular text-fg-faint">{teamSize} 人</span>}
        >
          <div className="flex flex-col gap-1.5">
            {(
              [
                ['决策层', team?.lead],
                ['战略层', team?.strategists],
                ['执行层', team?.executors],
              ] as const
            ).map(([label, ids]) =>
              (ids ?? []).length > 0 ? (
                <div key={label} className="flex items-baseline gap-2">
                  <span className="w-14 shrink-0 text-[10.5px] text-fg-faint">{label}</span>
                  <span className="flex flex-wrap gap-1.5">
                    {(ids ?? []).map((expertId) => (
                      // 这里给的是**专家 id**，不是姓名——`team` 里存的就是 id。
                      // 想显示姓名要再查一次名册，而报告页不该为了三个字
                      // 去多打一次接口；id 至少是可追溯的。
                      //
                      // 用 `Link` 不是 `a`：这一处以前是 `a href`，而当时
                      // 那条路由**根本不存在**，点下去是整页刷新 + 一个
                      // "找不到页面"。id 是可追溯的，链接也得真的能到。
                      <Link
                        key={expertId}
                        to={`/experts/${expertId}`}
                        className="rounded bg-raised px-1.5 py-0.5 font-mono text-[10px] text-fg-muted hover:text-brand"
                      >
                        {expertId}
                      </Link>
                    ))}
                  </span>
                </div>
              ) : null,
            )}
          </div>
        </ReportPanel>
      )}
    </>
  )
}
