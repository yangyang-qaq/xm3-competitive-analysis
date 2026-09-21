/**
 * 竞争情报中心：历次调研汇成的总体盘子。
 *
 * 这一页的纪律是**每个数都说得出算式**
 * ---------------------------------
 * 仪表盘最容易变成一块编数字的地方——"效率提升 83×""节省 5 小时"
 * 这种数看着唬人，但被问一句"怎么算的"就没了。这一页上的每一个数
 * 后面都跟着它自己的口径（`StatCard` 的 `hint`），而且**都能在
 * `routes_dashboard.py` 里找到对应的那一行**。
 *
 * 三个数必须成对出现，否则会自相矛盾
 * ------------------------------
 * 1. `sources`（266 条去重来源）与 `mentions`（2426 次引用）是两个口径
 * 2. `passed`（过了质量门）与 `publishable`（完整度够不够发）是两个判断
 * 3. `reports`（跑出报告的）与 `tasks`（发起过的）是两个分母
 * 只显示其中一个，用户就会在别处看到另一个然后以为是 bug。
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { Icon } from '../components/primitives/Icon'
import { PageHeader } from '../components/primitives/PageHeader'
import { StatCard } from '../components/primitives/StatCard'
import { Empty, ErrorNote, Loading } from '../components/primitives/States'
import { useAsync } from '../hooks/useAsync'
import { api } from '../lib/api'
import {
  MODE_LABEL,
  formatCost,
  formatInt,
  formatPercent,
  formatRelative,
} from '../lib/format'

export default function DashboardPage() {
  const { data, error, loading, reload } = useAsync(() => api.dashboard())

  return (
    <div className="mx-auto max-w-6xl px-8 py-8">
      <PageHeader
        title="竞争情报中心"
        sub="把每一次调研沉淀成可复用的竞争记忆 —— 覆盖广度、信源结构、结论质量与持续追踪。"
      />

      {loading && <Loading what="统计" />}
      {error && <ErrorNote error={error} onRetry={reload} />}

      {data && data.runs.reports === 0 && (
        <Empty
          title="还没有可以统计的调研"
          hint={
            <>
              去 <Link to="/" className="text-brand hover:underline">工作台</Link>{' '}
              跑一次，这一页就会有数了。
            </>
          }
        />
      )}

      {data && data.runs.reports > 0 && (
        <>
          {/* ---------- 覆盖 ---------- */}
          <section className="mb-8">
            <SectionTitle
              icon="radar"
              title="覆盖与取证"
              sub="这些数来自每一份报告的 metrics 列，是全量重算的，不是缓存"
            />
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <StatCard
                icon={<Icon name="radar" size={16} />}
                label="覆盖竞品"
                value={formatInt(data.coverage.brands)}
                hint={`已有情报沉淀的品牌：${
                  data.coverage.brandNames.slice(0, 3).join('、') || '—'
                }${data.coverage.brandNames.length > 3 ? ' 等' : ''}`}
              />
              <StatCard
                icon={<Icon name="knowledge" size={16} />}
                label="情报来源"
                value={formatInt(data.coverage.sources)}
                unit="条"
                hint={`去重后的独立来源。同一批证据在历次调研里被反复采到，累计引用 ${formatInt(
                  data.coverage.mentions,
                )} 次，平均每份报告 ${data.coverage.avgEvidencesPerReport} 条。`}
              />
              <StatCard
                icon={<Icon name="library" size={16} />}
                label="产出结论"
                value={formatInt(data.coverage.claims)}
                unit="条"
                hint="全部报告输出的分析结论总数。每条结论都必须锚定证据——无证据的会被质量门拦下。"
              />
              <StatCard
                icon={<Icon name="spark" size={16} />}
                label="交叉验证率"
                value={formatPercent(data.coverage.crossValidationRate)}
                hint={`经 ≥2 个独立来源相互印证的结论占比（${
                  data.coverage.crossValidatedClaims
                } / ${data.coverage.claims}）。**加权算的**：按结论数汇总后相除，不是逐份报告求平均。`}
              />
            </div>
          </section>

          {/* ---------- 运行 ---------- */}
          <section className="mb-8">
            <SectionTitle
              icon="library"
              title="运行概览"
              sub="成本与耗时都是实测的，来自每次调用的埋点合计"
            />
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <StatCard
                icon={<Icon name="library" size={16} />}
                label="跑出报告的调研"
                value={formatInt(data.runs.reports)}
                unit="次"
                hint={`共发起过 ${formatInt(data.runs.tasks)} 个任务，其余是被中断或失败的。`}
              />
              <StatCard
                icon={<Icon name="spark" size={16} />}
                label="质量门通过率"
                value={formatPercent(data.runs.passRate)}
                hint={`${data.runs.passed} / ${data.runs.reports} 份报告通过了证据与覆盖度阈值。`}
              />
              <StatCard
                icon={<Icon name="download" size={16} />}
                label="累计成本"
                value={formatCost(data.runs.totalCostUsd)}
                hint={`合计 ${formatInt(data.runs.totalTokens)} tokens。定价表在 provider 的适配器里，是可审计的常量。`}
              />
              <StatCard
                icon={<Icon name="radar" size={16} />}
                label="平均耗时"
                value={Math.round(data.runs.avgDurationMs / 1000)}
                unit="秒"
                hint="从需求理解到报告落库的端到端墙钟时间。"
              />
            </div>
          </section>

          {/* ---------- 质量口径 ---------- */}
          <section className="mb-8">
            <SectionTitle
              icon="experts"
              title="质量与人工介入"
              sub="两个必须一起看的判断"
            />
            <div className="rounded-card border border-line bg-panel px-5 py-4 shadow-card">
              <div className="grid grid-cols-1 gap-5 sm:grid-cols-3">
                <Field
                  label="质量门通过"
                  value={`${data.runs.passed} / ${data.runs.reports}`}
                  hint="证据数量、维度覆盖、独立信源三项阈值"
                />
                <Field
                  label="达到可发布"
                  value={`${data.runs.publishable} / ${data.runs.reports}`}
                  // **这两个判断是故意分开的**：过了门但完整度不够的报告
                  // 存在，而且很常见。只显示一个会让人以为另一个不存在。
                  hint="完整度评分达标 —— 它比质量门更严"
                />
                <Field
                  label="人工修正率"
                  value={formatPercent(data.correction.correctionRate)}
                  hint={`${data.correction.annotatedReports} 份报告上有批注，共 ${formatInt(
                    data.correction.totalFeedbacks,
                  )} 条`}
                />
              </div>
              {data.runs.passed > data.runs.publishable && (
                <p className="mt-4 border-t border-line pt-3 text-[11px] leading-relaxed text-fg-faint">
                  质量门通过 {data.runs.passed} 份、达到可发布 {data.runs.publishable} 份，
                  两个数不一样是正常的：**质量门管的是"证据够不够"，可发布管的是
                  "整篇报告有没有缺块"**。一份证据充足的报告完全可能因为某一章
                  没有产出而没达到可发布。
                </p>
              )}
            </div>
          </section>

          {/* ---------- 按档位 ---------- */}
          {data.byMode.length > 0 && (
            <section className="mb-8">
              <SectionTitle icon="library" title="按档位" sub="三档模式的实测产出对比" />
              <div className="overflow-hidden rounded-card border border-line bg-panel shadow-card">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr className="border-b border-line text-left text-[11px] text-fg-faint">
                      <th className="px-5 py-2.5 font-normal">档位</th>
                      <th className="px-5 py-2.5 text-right font-normal">调研次数</th>
                      <th className="px-5 py-2.5 text-right font-normal">平均证据</th>
                      <th className="px-5 py-2.5 text-right font-normal">质量门通过</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.byMode.map((row) => (
                      <tr key={row.mode} className="border-b border-line last:border-0">
                        <td className="px-5 py-3 text-fg">
                          {MODE_LABEL[row.mode] ?? row.mode}
                        </td>
                        <td className="tabular px-5 py-3 text-right text-fg-muted">
                          {formatInt(row.reports)}
                        </td>
                        <td className="tabular px-5 py-3 text-right text-fg-muted">
                          {row.avgEvidences}
                        </td>
                        <td className="tabular px-5 py-3 text-right text-fg-muted">
                          {formatInt(row.passed)} / {formatInt(row.reports)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {/* ---------- 最近 ---------- */}
          <section>
            <SectionTitle
              icon="library"
              title="最近的调研"
              sub="每份报告的关键数据"
              aside={
                <Link
                  to="/library"
                  className="flex items-center gap-1 text-[12px] text-brand hover:underline"
                >
                  全部调研 <Icon name="arrowRight" size={13} />
                </Link>
              }
            />
            <ul className="flex flex-col gap-2">
              {data.recent.map((row) => (
                <li key={row.reportId}>
                  <Link
                    to={`/workspace/${row.taskId}`}
                    className="flex items-center gap-4 rounded-card border border-line bg-panel px-5 py-3 shadow-card transition-shadow hover:shadow-pop"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[13px] text-fg">
                        {row.subject || row.query}
                      </p>
                      <p className="mt-0.5 truncate text-[11px] text-fg-faint">
                        {row.query}
                      </p>
                    </div>
                    <span className="tabular shrink-0 text-[11px] text-fg-muted">
                      {formatInt(row.evidences)} 证据
                    </span>
                    <span className="tabular shrink-0 text-[11px] text-fg-muted">
                      {formatInt(row.claims)} 结论
                    </span>
                    <span className="shrink-0 text-[11px] text-fg-faint">
                      {formatRelative(row.generatedAt)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  )
}

function SectionTitle({
  icon,
  title,
  sub,
  aside,
}: {
  icon: 'radar' | 'knowledge' | 'library' | 'experts'
  title: string
  sub?: string
  aside?: ReactNode
}) {
  return (
    <div className="mb-3 flex items-end justify-between gap-4">
      <div>
        <h2 className="flex items-center gap-2 text-[14px] font-medium text-fg">
          <span className="text-fg-faint">
            <Icon name={icon} size={15} />
          </span>
          {title}
        </h2>
        {sub && <p className="mt-0.5 text-[11px] text-fg-faint">{sub}</p>}
      </div>
      {aside}
    </div>
  )
}

function Field({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div>
      <p className="text-[11px] text-fg-muted">{label}</p>
      <p className="tabular mt-1 text-[20px] font-semibold text-fg">{value}</p>
      <p className="mt-1 text-[11px] leading-relaxed text-fg-faint">{hint}</p>
    </div>
  )
}
