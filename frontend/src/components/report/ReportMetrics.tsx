/**
 * 指标面板。这份报告是怎么跑出来的、跑得怎么样。
 *
 * 顶上一排用 `StatCard`（大数字），其余用紧凑的行
 * -------------------------------------------
 * 四个头条数字（引用忠实度相关的那几个）值得一眼看到，所以给大卡片；
 * 剩下二十几个数是**查证用的**——读者带着"这个 71% 是怎么来的"
 * 这个问题来翻，不是来扫的。给它们同样大的卡片会让整页变成仪表盘，
 * 而报告页不是仪表盘。
 *
 * 每个数都带一句算式
 * ----------------
 * `StatCard` 的 `hint` 就是干这个的。一个孤零零的"交叉验证率 66%"
 * 没法验证也没法反驳；配上"经 ≥2 个独立来源印证的论点占比"，
 * 读的人才知道它在说什么，也才能在它不对的时候指出来。
 *
 * 返工前后要**并排**显示
 * -------------------
 * `metrics.rework` 里有 `before` / `after` / `delta`。三个值分开印的话，
 * "返工是有效的"这句话就只能靠读者自己算。并排两列 + 差值，
 * 读者一眼能看出哪几项变好了、哪几项没动——而"没动"同样重要
 * （有的返工只是把正文重写了一遍，证据一条没多）。
 */
import type { ReactNode } from 'react'

import { formatCost, formatDuration, formatInt, formatPercent } from '../../lib/format'
import { StatCard } from '../primitives/StatCard'
import type { ReportMetrics as MetricsData } from '../../types/report'

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-baseline gap-2 border-t border-line py-1 first:border-t-0">
      <span className="text-[11.5px] text-fg-muted">{label}</span>
      {hint && <span className="text-[10px] text-fg-faint">{hint}</span>}
      <span className="ml-auto shrink-0 text-[11.5px] tabular text-fg">{value}</span>
    </div>
  )
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <p className="mb-1 text-[10.5px] font-medium tracking-wide text-fg-faint">{title}</p>
      {children}
    </div>
  )
}

/** 只有真的算得出来才显示。`undefined` 与 `0` 在这里是两件事：
 *  `0 条降级`是个结论，`没有这个数`不是。 */
function maybe(metrics: MetricsData, key: keyof MetricsData): number | undefined {
  const value = metrics[key]
  return typeof value === 'number' ? value : undefined
}

/**
 * 返工提前结束的原因码 → 中文。
 *
 * 这是**前端唯一一处**给后端枚举建映射表，所以要写清为什么这里可以建：
 * 它是一个封闭的、由我们自己的编排层写下的原因码（`orchestrator.py`），
 * 不是模型输出的、会随时长出新值的字段。`?? entry.stopped` 的兜底也在——
 * 将来多一个原因码，界面显示那个码本身，而不是一片空白。
 */
const STOPPED_LABEL: Record<string, string> = {
  'no-new-evidence': '补采没有新增证据，再跑也不会有新结论',
}

/**
 * 返工对比表里那六个指标中，**哪两个是比率**（0–1，其余四个是条数）。
 *
 * 不格式化的话，同一列里会出现 `188` 和 `0.1667`，而读者只会看到
 * "这一项返工后从 0.1667 变成了 0.25"——那个数得自己脑补成 25%。
 * 格式化之后是 `16.7%` → `25.0%`。
 *
 * 这份名单照抄后端 `metrics.rework_delta()` 里的 `keys` 元组。是耦合，
 * 所以写在这里而不是藏进一个"猜类型"的启发式（"小于 1 就当比率"会把
 * 恰好等于 1 的条数也吞掉，而 `dimensionCoverage` 返工后正好是 1）。
 * 后端加了新指标、这里没加，表现是那一项按原值显示——**不是错的，只是不好看**。
 */
const RATE_KEYS = new Set(['crossValidationRate', 'dimensionCoverage'])

/** 返工对比表里一个数的显示形式。 */
function reworkValue(key: string, value: number | undefined): string {
  if (value === undefined) return '—'
  return RATE_KEYS.has(key) ? formatPercent(value, 1) : String(value)
}

export function ReportMetrics({ metrics }: { metrics: MetricsData | undefined }) {
  if (!metrics) {
    return (
      <section className="rounded-card border border-line bg-panel p-4 shadow-card">
        <h3 className="text-[13px] font-medium text-fg">指标</h3>
        <p className="mt-2 text-[12px] text-fg-faint">这份报告没有存指标（早于指标面板的版本）。</p>
      </section>
    )
  }

  const claims = maybe(metrics, 'claims')
  const unsupported = maybe(metrics, 'unsupportedClaimRate')
  const hallucination = maybe(metrics, 'hallucinationRate')
  const crossRate = maybe(metrics, 'crossValidationRate')
  const coverage = maybe(metrics, 'dimensionCoverage')

  const evidenceRows: Array<[string, number | undefined, string?]> = [
    ['采到证据', maybe(metrics, 'evidences'), '过滤后进入分析池的条数'],
    ['其中降级', maybe(metrics, 'degradedEvidences'), '正文没抓到、只有摘要'],
    ['原始命中', maybe(metrics, 'rawHits')],
    ['过滤后', maybe(metrics, 'filteredHits'), '判为不相关而丢弃'],
    ['抓取成功', maybe(metrics, 'fetchedOk')],
    ['抓取降级', maybe(metrics, 'fetchedDegraded')],
    ['因预算跳过', maybe(metrics, 'fetchSkippedByBudget'), '检索额度用尽'],
    // 适配层的账。搜索源不支持按站点过滤时，平台约束被折进查询词里
    // ——结果通常更宽。这个数非零表示有几次检索的精度打了折，
    // 而**不显示它，读者就分不出"没降级"和"降级了没说"**。
    ['平台筛选折进查询词', maybe(metrics, 'siteFilterFolded'), '搜索源不支持站点过滤'],
  ].map(([label, value, hint]) => [label, value, hint] as [string, number | undefined, string?])

  // 主 LLM 换过才说。**没换过时整行不出现**——那一行如果恒在，
  // 写着"0 次切换"，一份正常的报告就会看起来像是出过事。
  const llmFallback = metrics.llmFallback?.degraded ? metrics.llmFallback : undefined

  const claimRows: Array<[string, number | undefined, string?]> = [
    ['论点总数', claims],
    ['有证据支撑', maybe(metrics, 'verifiedClaims')],
    ['交叉验证过', maybe(metrics, 'crossValidatedClaims'), '≥2 个独立来源印证'],
    ['被剔除的编造引用', maybe(metrics, 'phantomCitations'), '正文里已不存在'],
    ['独立信源', maybe(metrics, 'independentDomains'), '去重域名'],
    ['平台覆盖', maybe(metrics, 'platformCount')],
  ]

  return (
    <section id="metrics" className="scroll-mt-[88px] flex flex-col gap-3">
      <h3 className="text-[13px] font-medium text-fg">指标</h3>

      {/* ---- 头条四个 ---- */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {unsupported !== undefined && (
          <StatCard
            label="无证据立论率"
            value={formatPercent(unsupported, 1)}
            hint="没有任何证据支撑的论点占比。越低越好"
          />
        )}
        {crossRate !== undefined && (
          <StatCard
            label="交叉验证率"
            value={formatPercent(crossRate, 0)}
            hint="经 ≥2 个独立来源相互印证的论点占比"
          />
        )}
        {coverage !== undefined && (
          <StatCard
            label="维度覆盖率"
            value={formatPercent(coverage, 0)}
            hint={`计划 ${metrics.dimensionsPlanned ?? '?'} 个维度，${
              metrics.dimensionsCovered ?? '?'
            } 个采到了证据`}
          />
        )}
        {hallucination !== undefined && (
          <StatCard
            label="幻觉引用率"
            value={formatPercent(hallucination, 1)}
            hint="模型输出的 evidence_id 里查不到的比例。被剔除后正文里已不含这些引用"
          />
        )}
      </div>

      <div className="rounded-card border border-line bg-panel p-4 shadow-card">
        <div className="grid gap-4 md:grid-cols-3">
          <Group title="证据">
            {evidenceRows.map(([label, value, hint]) =>
              value === undefined ? null : (
                <Row key={label} label={label} value={formatInt(value)} hint={hint} />
              ),
            )}
          </Group>

          <Group title="论点">
            {claimRows.map(([label, value, hint]) =>
              value === undefined ? null : (
                <Row key={label} label={label} value={formatInt(value)} hint={hint} />
              ),
            )}
          </Group>

          <Group title="成本与耗时">
            {metrics.totalCostUsd !== undefined && (
              <Row label="模型成本" value={formatCost(metrics.totalCostUsd)} />
            )}
            {metrics.totalTokens !== undefined && (
              <Row label="总 token" value={formatInt(metrics.totalTokens)} />
            )}
            {metrics.llmCalls !== undefined && (
              <Row label="模型调用" value={formatInt(metrics.llmCalls)} />
            )}
            {llmFallback && (
              <Row
                label="主模型故障切换"
                value={`${llmFallback.fallbackCalls ?? 0} 次`}
                hint={
                  llmFallback.latched
                    ? `${llmFallback.primary ?? '主'} 已熔断，后续全部走 ${llmFallback.secondary ?? '备用'}`
                    : `${llmFallback.primary ?? '主'} 失败时改走 ${llmFallback.secondary ?? '备用'}`
                }
              />
            )}
            {metrics.llmOptionalFailures !== undefined && metrics.llmOptionalFailures > 0 && (
              <Row
                label="可选调用失败"
                value={formatInt(metrics.llmOptionalFailures)}
                hint="对应内容按降级处理"
              />
            )}
            {metrics.searchCalls !== undefined && (
              <Row label="检索次数" value={formatInt(metrics.searchCalls)} />
            )}
            {metrics.searchErrors !== undefined && metrics.searchErrors > 0 && (
              <Row label="检索失败" value={formatInt(metrics.searchErrors)} />
            )}
            {metrics.durationMs !== undefined && (
              <Row label="总耗时" value={formatDuration(metrics.durationMs / 1000)} />
            )}
            {metrics.firstEvidenceMs !== undefined && (
              <Row
                label="首次采到证据"
                value={formatDuration(metrics.firstEvidenceMs / 1000)}
                hint="从开跑到第一次看见证据的时间"
              />
            )}
            {metrics.slowestCallMs !== undefined && (
              <Row label="最慢一次调用" value={formatDuration(metrics.slowestCallMs / 1000)} />
            )}
          </Group>
        </div>

        {/* ---- 按用途拆的模型调用 ---- */}
        {metrics.byPurpose && Object.keys(metrics.byPurpose).length > 0 && (
          <div className="mt-4 border-t border-line pt-3">
            <Group title="模型调用按用途">
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {Object.entries(metrics.byPurpose)
                  .sort((left, right) => right[1] - left[1])
                  .map(([purpose, count]) => (
                    <span key={purpose} className="text-[11px] text-fg-muted">
                      <span className="font-mono text-[10.5px] text-fg-faint">{purpose}</span>
                      <span className="ml-1.5 tabular text-fg">{count}</span>
                    </span>
                  ))}
              </div>
            </Group>
          </div>
        )}

        {/* ---- 维度覆盖明细 ---- */}
        {metrics.dimensionCoverageDetail &&
          Object.keys(metrics.dimensionCoverageDetail).length > 0 && (
            <div className="mt-4 border-t border-line pt-3">
              <Group title="各维度采到的证据数">
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {Object.entries(metrics.dimensionCoverageDetail).map(([dimension, count]) => (
                    <span
                      key={dimension}
                      className={[
                        'text-[11px]',
                        // 0 条要标出来：覆盖率算的就是这些维度的占比，
                        // 而一列"功能对比 12"里夹一个"定价 0"很容易被扫过去。
                        count === 0 ? 'text-warn' : 'text-fg-muted',
                      ].join(' ')}
                    >
                      {dimension}
                      <span className="ml-1.5 tabular">{count}</span>
                    </span>
                  ))}
                </div>
              </Group>
            </div>
          )}

        {/* ---- 返工前后 ---- */}
        {metrics.rework && (
          <div className="mt-4 border-t border-line pt-3">
            <Group title={`返工前后（共 ${metrics.reworkRounds ?? 0} 轮）`}>
              {/* 后端自己判的那句话印在最上面（`rework.improved`：看覆盖与
                  独立信源，**不看成本**——返工必然更贵，把成本算进去结论
                  永远是"没有改善"）。它比让读者自己从六行数里推要可靠：
                  逐行的绿/红是"这一项动了"，而这一句是"这一轮值不值"。 */}
              {metrics.rework.improved !== undefined && (
                <p
                  className={[
                    'mb-1.5 text-[11px]',
                    metrics.rework.improved ? 'text-ok' : 'text-fg-muted',
                  ].join(' ')}
                >
                  {metrics.rework.improved
                    ? '这一轮返工带来了提升（按覆盖与独立信源判定）'
                    : '这一轮返工没有改变覆盖与独立信源'}
                  {metrics.rework.costDelta !== undefined && metrics.rework.costDelta > 0 && (
                    <span className="text-fg-faint">
                      {' '}
                      · 多花 ${metrics.rework.costDelta.toFixed(4)}
                    </span>
                  )}
                </p>
              )}
              <table className="border-collapse text-[11.5px]">
                <thead>
                  <tr className="text-fg-faint">
                    <th className="pr-4 text-left font-medium">指标</th>
                    <th className="pr-4 text-right font-medium">返工前</th>
                    <th className="pr-4 text-right font-medium">返工后</th>
                    <th className="text-right font-medium">变化</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.keys(metrics.rework.after).map((key) => {
                    // 逐项对比在 `changes` 里，**不是** `before`/`after`/`delta`
                    // 三个并排的 map——早先照后一种读过，在第一份真的
                    // 返工过的报告上就崩了（见 `types/report.ts` 的注释）。
                    const before = metrics.rework?.before[key]
                    const after = metrics.rework?.after[key]
                    const delta = metrics.rework?.changes?.[key]?.delta
                    return (
                      <tr key={key} className="border-t border-line">
                        <th className="py-0.5 pr-4 text-left font-normal font-mono text-[10.5px] text-fg-muted">
                          {key}
                        </th>
                        <td className="py-0.5 pr-4 text-right tabular text-fg-muted">
                          {reworkValue(key, before)}
                        </td>
                        <td className="py-0.5 pr-4 text-right tabular text-fg">
                          {reworkValue(key, after)}
                        </td>
                        <td
                          className={[
                            'py-0.5 text-right tabular',
                            // 变好是绿、变坏是红、"没动"是灰。
                            // 灰色那一类是有信息的：它说明这一轮返工
                            // 没有改变这个数，而这正是"返工是不是有效"
                            // 这个问题的一半答案。
                            (delta ?? 0) > 0
                              ? 'text-ok'
                              : (delta ?? 0) < 0
                                ? 'text-danger'
                                : 'text-fg-faint',
                          ].join(' ')}
                        >
                          {delta === undefined
                            ? '—'
                            : RATE_KEYS.has(key)
                              ? `${delta > 0 ? '+' : ''}${formatPercent(delta, 1)}`
                              : delta > 0
                                ? `+${delta}`
                                : delta}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </Group>
          </div>
        )}

        {/* ---- 返工日志 ---- */}
        {metrics.reworkLog && metrics.reworkLog.length > 0 && (
          <div className="mt-4 border-t border-line pt-3">
            <Group title="返工做了什么">
              <ul className="flex flex-col gap-1.5">
                {metrics.reworkLog.map((entry, index) => (
                  <li key={index} className="text-[11px] leading-relaxed">
                    <div className="flex flex-wrap items-baseline gap-2">
                      <span className="shrink-0 rounded bg-raised px-1.5 py-0.5 text-[10px] text-fg-muted">
                        第 {entry.round} 轮
                      </span>
                      <span className="tabular text-fg-muted">
                        补采 {entry.targets} 个目标 · 新增 {entry.added} 条证据
                      </span>
                      {entry.qualityPassed !== undefined && (
                        <span className={entry.qualityPassed ? 'text-ok' : 'text-warn'}>
                          {entry.qualityPassed ? '质检通过' : '质检未通过'}
                        </span>
                      )}
                      {/* 提前结束的那一轮没有 `reason`，只有 `stopped`。
                          不写这一行的话，那一轮会显示成"新增 0 条证据"，
                          读起来像"补采失败"；实际原因恰恰是**没必要再补**
                          （补了也没有新证据），是一条正面结论。 */}
                      {entry.stopped && (
                        <span className="text-fg-faint">
                          提前结束：{STOPPED_LABEL[entry.stopped] ?? entry.stopped}
                        </span>
                      )}
                    </div>
                    {entry.reason && (
                      <p className="mt-0.5 text-fg-muted">{entry.reason}</p>
                    )}
                  </li>
                ))}
              </ul>
            </Group>
          </div>
        )}
      </div>
    </section>
  )
}
