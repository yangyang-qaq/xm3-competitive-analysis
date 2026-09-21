/**
 * 我的调研：做过的每一次调研。
 *
 * 列的是**报告**而不是任务
 * --------------------
 * 116 个任务里只有 12 个跑出了报告，其余是被中断的。任务列表会把
 * 那 104 条噪声混进来，而它们的"成果"是零。这一页回答的是
 * "我手里有什么"，所以以报告为单位；想看某次运行的完整经过，
 * 卡片上有通往工作台的入口。
 *
 * `subject` 为空时**退回显示 query**
 * ------------------------------
 * 早先 `tasks.subject` 从来没被写过（只在澄清那条路上写），
 * 界面上因此长期显示"（尚未解析出调研对象）"。任务行那条路已经修了，
 * 但**历史上那 12 份报告里的 subject 是好的**（那是 `assemble` 写的，
 * 另一条路）。这里对空值做兜底：显示 query 而不是一个空标题——
 * 一份报告总有 query，用它不会出现"一行什么都没有"。
 */
import { Link } from 'react-router-dom'

import { PageHeader } from '../components/primitives/PageHeader'
import { Empty, ErrorNote, Loading } from '../components/primitives/States'
import { useAsync } from '../hooks/useAsync'
import { api, reportExportUrl } from '../lib/api'
import { MODE_LABEL, formatInt, formatRelative } from '../lib/format'

export default function LibraryPage() {
  const { data, error, loading, reload } = useAsync(() => api.listReports({ limit: 50 }))

  return (
    <div className="mx-auto max-w-5xl px-8 py-8">
      <PageHeader
        title="我的调研"
        sub="做过的每一次调研。点开看报告，或回到工作台看它当初是怎么跑出来的。"
        aside={
          data && (
            <span className="text-[12px] text-fg-muted">
              共 {formatInt(data.total)} 份报告
            </span>
          )
        }
      />

      {loading && <Loading what="报告列表" />}
      {error && <ErrorNote error={error} onRetry={reload} />}

      {data && data.items.length === 0 && (
        <Empty
          title="还没有跑出过报告"
          hint={
            <>
              去 <Link to="/" className="text-brand hover:underline">工作台</Link>{' '}
              发起第一次调研。快速档大约两分钟。
            </>
          }
        />
      )}

      {data && data.items.length > 0 && (
        <ul className="flex flex-col gap-3">
          {data.items.map((report) => (
            <li key={report.reportId}>
              <article className="rounded-card border border-line bg-panel px-5 py-4 shadow-card transition-shadow hover:shadow-pop">
                <div className="flex items-start justify-between gap-6">
                  <div className="min-w-0">
                    {/* 标题本身就通向报告页。它是一张报告卡片的标题，
                        点标题进报告是通用约定；另外给一个按钮是
                        "有两条路通向同一个地方"的多余。 */}
                    <h2 className="truncate text-[14px] font-medium text-fg">
                      <Link to={`/report/${report.reportId}`} className="hover:text-brand">
                        {report.subject || report.query}
                      </Link>
                    </h2>
                    <p className="mt-1 line-clamp-2 text-[12px] leading-relaxed text-fg-muted">
                      {report.query}
                    </p>
                  </div>
                  <span className="shrink-0 rounded-full bg-raised px-2.5 py-0.5 text-[11px] text-fg-muted">
                    {MODE_LABEL[report.mode] ?? report.mode}
                  </span>
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-fg-faint">
                  <span className="tabular">{formatInt(report.evidenceCount)} 条证据</span>
                  <span className="tabular">
                    {formatInt(Number(report.metrics?.claims ?? 0))} 条结论
                  </span>
                  {report.degradedCount > 0 && (
                    // 降级**要说出来**。藏着的话，一份用了降级数据的报告
                    // 看起来和一份完整的一模一样。
                    <span className="text-warn">{report.degradedCount} 处降级</span>
                  )}
                  <span>{formatRelative(report.generatedAt)}</span>
                  {/* 质量门与"能不能发"是**两个判断**（见后端 quality 的注释）：
                      过了门但完整度不够时，两个标签会一绿一灰。这里都显示，
                      只显示一个会让那份报告看起来自相矛盾。 */}
                  {report.quality.passed && (
                    <span className="text-ok">质量门通过</span>
                  )}
                  {report.quality.publishable === false && (
                    <span title="完整度未达可发布标准">未达可发布</span>
                  )}
                </div>

                <div className="mt-3.5 flex flex-wrap items-center gap-2">
                  <Link
                    to={`/report/${report.reportId}`}
                    className="rounded-lg bg-brand/12 px-3 py-1 text-[11px] text-brand hover:bg-brand/20"
                  >
                    读报告
                  </Link>
                  <Link
                    to={`/workspace/${report.taskId}`}
                    className="rounded-lg border border-line px-3 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
                  >
                    看它怎么跑的
                  </Link>
                  {/* 导出用 `<a download>` 而不是 fetch：
                      报告是几 MB 的文本，走 fetch 要先把整份读进内存
                      再造一个 Blob 下载，白搭一倍内存。 */}
                  <a
                    href={reportExportUrl(report.reportId, 'md')}
                    className="rounded-lg border border-line px-3 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
                  >
                    导出 Markdown
                  </a>
                  <a
                    href={reportExportUrl(report.reportId, 'json')}
                    className="rounded-lg border border-line px-3 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
                  >
                    导出 JSON
                  </a>
                  <span className="ml-auto font-mono text-[10px] text-fg-faint">
                    {report.reportId}
                  </span>
                </div>
              </article>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
