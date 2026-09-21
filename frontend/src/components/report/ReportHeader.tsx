/**
 * 报告页头：这份报告是什么、什么时候跑的、跑成什么样。
 *
 * 降级横幅放在最上面，而且**不可折叠**
 * ----------------------------------
 * `degraded` 非空表示这次运行有东西没拿到（某项分析没做、某个来源没采到、
 * 某次调用失败了）。它必须出现在读者读到第一个结论**之前**——理由很直白：
 * 一份因为只采到两个来源而显得"竞争不激烈"的报告，和一份真的不激烈的
 * 报告，在正文里长得一模一样。降级说明是这两者之间唯一的区别。
 *
 * 不做折叠是刻意的：折叠等于给读者一个"以后再看"的选项，而这类信息
 * 一旦被折叠就等于不存在。
 *
 * 导出按钮给的是**链接**不是 `fetch`
 * --------------------------------
 * 导出走浏览器直接下载（后端返 `Content-Disposition`），用 `fetch` 拿到
 * 的是一坨文本，还得自己造 `Blob` 和 `<a download>`——多写二十行换来
 * 一个和浏览器行为不一样的东西。所以 `reportExportUrl()` 返回的是 URL。
 */
import type { ReactNode } from 'react'

import { reportExportUrl } from '../../lib/api'
import {
  formatCost,
  formatDateTime,
  formatDuration,
  formatInt,
  modeLabel,
} from '../../lib/format'
import type { ReportBody, ReportQuality } from '../../types/report'

export interface ReportHeaderProps {
  reportId: string
  body: ReportBody
  quality: ReportQuality | undefined
  /** 已提交的批注条数。与「人工修正率」是同一个数的分子 */
  feedbackCount: number
  onOpenQuality: () => void
}

function Chip({ children, tone = 'plain' }: { children: ReactNode; tone?: 'plain' | 'warn' }) {
  return (
    <span
      className={[
        'rounded-full px-2 py-0.5 text-[11px]',
        tone === 'warn' ? 'bg-warn/12 text-warn' : 'bg-raised text-fg-muted',
      ].join(' ')}
    >
      {children}
    </span>
  )
}

export function ReportHeader({
  reportId,
  body,
  quality,
  feedbackCount,
  onOpenQuality,
}: ReportHeaderProps) {
  const degraded = body.degraded ?? []
  // `label` 是后端写好的，优先用它；`modeLabel` 只是 `key` 那半边缺席时的兜底。
  const mode = body.mode?.label || modeLabel(body.mode?.key ?? '')
  const passed = quality?.passed

  return (
    <header className="flex flex-col gap-3">
      {/* ---- 降级横幅 ---- */}
      {degraded.length > 0 && (
        <section className="rounded-card border border-warn/50 bg-warn/8 px-4 py-3">
          <h2 className="text-[12.5px] font-medium text-warn">
            这次运行有 {degraded.length} 处降级，报告里的相应内容可能不完整
          </h2>
          <ul className="mt-1.5 flex flex-col gap-0.5">
            {degraded.map((item, index) => (
              <li key={index} className="text-[11.5px] leading-relaxed text-fg-muted">
                · {item}
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* ---- 标题 ---- */}
      <div>
        <h1 className="text-[19px] font-medium leading-snug text-fg">
          {body.subject || body.query || '（这份报告没有标题）'}
        </h1>
        {body.query && body.query !== body.subject && (
          <p className="mt-1 text-[12px] text-fg-muted">调研问题：{body.query}</p>
        )}
      </div>

      {/* ---- 品牌 ---- */}
      {(body.brands ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {(body.brands ?? []).map((brand) => (
            <span
              key={brand}
              className="rounded-full border border-line bg-panel px-2.5 py-0.5 text-[11.5px] text-fg"
            >
              {brand}
            </span>
          ))}
        </div>
      )}

      {/* ---- 元信息 ---- */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-fg-faint">
        {mode && <Chip>{mode}</Chip>}
        {body.dimensions && body.dimensions.length > 0 && (
          <span>{body.dimensions.length} 个维度</span>
        )}
        <span>生成于 {formatDateTime(body.generatedAt ?? '')}</span>
        {body.durationMs !== undefined && <span>耗时 {formatDuration(body.durationMs / 1000)}</span>}
        {body.metrics?.evidences !== undefined && (
          <span className="tabular">{formatInt(body.metrics.evidences)} 条证据</span>
        )}
        {body.metrics?.totalCostUsd !== undefined && body.metrics.totalCostUsd > 0 && (
          <span className="tabular">成本 {formatCost(body.metrics.totalCostUsd)}</span>
        )}

        {/* 质量门。**点开看细节**——这里只给一个结论，
            以及"这个结论是不是可发"。两者是两件事：
            `passed` 看的是有没有 blocker，`publishable` 看的是完整度。 */}
        {quality && (
          <button
            type="button"
            onClick={onOpenQuality}
            className={[
              'rounded-full px-2 py-0.5 text-[11px] transition-colors',
              passed ? 'bg-ok/12 text-ok hover:bg-ok/20' : 'bg-danger/12 text-danger hover:bg-danger/20',
            ].join(' ')}
          >
            {passed ? '质检通过' : '质检未通过'}
            {quality.publishable === false && '（不建议直接发布）'}
          </button>
        )}

        {feedbackCount > 0 && <span>已提交 {feedbackCount} 条批注</span>}
      </div>

      {/* ---- 导出 ---- */}
      <div className="flex gap-2">
        {(
          [
            ['md', '导出 Markdown'],
            ['json', '导出 JSON'],
          ] as const
        ).map(([format, label]) => (
          // `<a download>` 而不是 `window.open`：后者会被弹窗拦截，
          // 而用户点了按钮什么都没发生，看起来像导出坏了。
          <a
            key={format}
            href={reportExportUrl(reportId, format)}
            className="rounded-lg border border-line bg-panel px-3 py-1 text-[11.5px] text-fg-muted transition-colors hover:border-line-strong hover:text-fg"
          >
            {label}
          </a>
        ))}
      </div>
    </header>
  )
}
