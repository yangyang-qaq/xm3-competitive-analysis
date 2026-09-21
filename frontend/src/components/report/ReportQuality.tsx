/**
 * 质检明细。页头上那个"质检通过 / 未通过"徽章点开就是这里。
 *
 * `passed` 与 `publishable` 是**两个判断**，不能互相顶替
 * -------------------------------------------------
 * - `passed`：质量门过没过。看的是有没有 blocker / 超阈值的 major。
 * - `publishable`：这份报告够不够完整到能发出去。看的是完整度。
 *
 * 两者会不一致，而且不一致是有意义的：一份**没有硬伤**但**只覆盖了
 * 一半维度**的报告，`passed` 是 true、`publishable` 是 false。
 * 只印一个的话，"质检通过"会被读成"可以发"——而它可能只是"没有错误"。
 *
 * `coverage` 与 `completeness` 同样不是一回事
 * ----------------------------------------
 * 前者是**维度**覆盖率（计划 8 个维度、采到 5 个 → 0.625），
 * 后者是整份报告的完整度（有没有摘要、有没有定价、画像够不够）。
 * 名字像、数也都在 0–1 之间，所以这一页把它们并排印、各自写清口径。
 *
 * `review` 是**模型判的**
 * --------------------
 * `quality.review` 里是逐维度的 1–5 分与评语，来自一次质检调用。
 * 它是模型的意见，不是实测指标——所以它单独一块，标题里写明，
 * 而且不参与上面那些数字。
 */
import { formatPercent } from '../../lib/format'
import type { ReportQuality as QualityData } from '../../types/report'

function Line({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2 border-t border-line py-1 first:border-t-0">
      <span className="text-[11.5px] text-fg-muted">{label}</span>
      <span className="ml-auto shrink-0 text-[11.5px] tabular text-fg">{value}</span>
    </div>
  )
}

export interface ReportQualityProps {
  quality: QualityData | undefined
  onClose: () => void
}

export function ReportQuality({ quality, onClose }: ReportQualityProps) {
  if (!quality) return null

  const review = quality.review
  const thresholds = quality.thresholds
  const failedBecause = quality.failedBecause ?? []

  return (
    <section className="rounded-card border border-line bg-panel p-5 shadow-card">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-[15px] font-medium text-fg">质检明细</h2>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg px-2 py-0.5 text-[11px] text-fg-faint hover:bg-raised hover:text-fg-muted"
        >
          收起
        </button>
      </div>

      {/* ---- 两个结论并排 ---- */}
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div
          className={[
            'rounded-lg px-3 py-2',
            quality.passed ? 'bg-ok/10' : 'bg-danger/8',
          ].join(' ')}
        >
          <p className="text-[10.5px] text-fg-faint">质量门</p>
          <p
            className={[
              'text-[15px] font-medium',
              quality.passed ? 'text-ok' : 'text-danger',
            ].join(' ')}
          >
            {quality.passed ? '通过' : '未通过'}
          </p>
          <p className="mt-0.5 text-[10.5px] leading-relaxed text-fg-faint">
            看的是有没有硬伤（blocker 与超阈值的 major）
          </p>
        </div>
        <div
          className={[
            'rounded-lg px-3 py-2',
            quality.publishable === false ? 'bg-warn/10' : 'bg-raised',
          ].join(' ')}
        >
          <p className="text-[10.5px] text-fg-faint">可发布性</p>
          <p className="text-[15px] font-medium text-fg">
            {quality.publishable === undefined
              ? '没有这个数'
              : quality.publishable
                ? '可以发'
                : '不建议直接发'}
          </p>
          <p className="mt-0.5 text-[10.5px] leading-relaxed text-fg-faint">
            看的是内容够不够完整
            {quality.completeness !== undefined &&
              `（完整度 ${formatPercent(quality.completeness, 0)}）`}
          </p>
        </div>
      </div>

      {failedBecause.length > 0 && (
        <div className="mt-3 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2">
          <p className="text-[11.5px] font-medium text-danger">未通过的原因</p>
          <ul className="mt-1 flex flex-col gap-0.5">
            {failedBecause.map((reason, index) => (
              <li key={index} className="text-[11px] leading-relaxed text-fg-muted">
                · {reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ---- 计数与覆盖 ---- */}
      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <div>
          <p className="mb-1 text-[10.5px] font-medium tracking-wide text-fg-faint">问题计数</p>
          {quality.blockers !== undefined && <Line label="blocker" value={String(quality.blockers)} />}
          {quality.major !== undefined && <Line label="major" value={String(quality.major)} />}
          {quality.minor !== undefined && <Line label="minor" value={String(quality.minor)} />}
        </div>
        <div>
          <p className="mb-1 text-[10.5px] font-medium tracking-wide text-fg-faint">覆盖</p>
          {quality.coverage !== undefined && (
            <Line label="维度覆盖率" value={formatPercent(quality.coverage, 0)} />
          )}
          {quality.dimensionsPlanned !== undefined && (
            <Line label="计划维度" value={String(quality.dimensionsPlanned)} />
          )}
          {quality.dimensionsCovered !== undefined && (
            <Line label="采到证据的维度" value={String(quality.dimensionsCovered)} />
          )}
          {quality.completeness !== undefined && (
            <Line label="完整度" value={formatPercent(quality.completeness, 0)} />
          )}
        </div>
      </div>

      {(quality.uncoveredDimensions ?? []).length > 0 && (
        <p className="mt-2 text-[11px] text-warn">
          没采到证据的维度：{(quality.uncoveredDimensions ?? []).join('、')}
        </p>
      )}

      {/* ---- 阈值。**印出来**：一个"未通过"必须能被读者自己复核，
             否则它只是一个我们说了算的判断。 ---- */}
      {thresholds && Object.keys(thresholds).length > 0 && (
        <div className="mt-3 border-t border-line pt-2">
          <p className="mb-1 text-[10.5px] font-medium tracking-wide text-fg-faint">
            门槛（判定用的数就是上面那些，这里是可以自己复核的标准）
          </p>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {Object.entries(thresholds).map(([key, value]) => (
              <span key={key} className="text-[11px] text-fg-muted">
                <span className="font-mono text-[10.5px] text-fg-faint">{key}</span>
                <span className="ml-1.5 tabular text-fg">{value}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* ---- 模型评语 ---- */}
      {review && (
        <div className="mt-4 border-t border-line pt-3">
          <p className="mb-1.5 text-[10.5px] font-medium tracking-wide text-fg-faint">
            模型评语（由一次质检调用生成，是模型的意见，不是实测指标）
          </p>

          {review.summary && (
            <p className="text-[12px] leading-relaxed text-fg">{review.summary}</p>
          )}

          {review.dimensions.length > 0 && (
            <table className="mt-2 w-full border-collapse text-[11.5px]">
              <tbody>
                {review.dimensions.map((item) => (
                  <tr key={item.dimension} className="border-t border-line align-top">
                    <th className="w-24 py-1 pr-3 text-left font-normal text-fg">
                      {item.dimension}
                      {(review.weakDimensions ?? []).includes(item.dimension) && (
                        <span className="ml-1 rounded bg-warn/14 px-1 text-[10px] text-warn">
                          偏弱
                        </span>
                      )}
                    </th>
                    <td className="w-10 py-1 pr-3 text-right tabular text-fg-muted">
                      {item.score}
                    </td>
                    <td className="py-1 leading-relaxed text-fg-muted">{item.comment}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {review.averageScore !== undefined && (
            <p className="mt-2 text-[11px] text-fg-muted">
              平均分 <span className="tabular text-fg">{review.averageScore}</span>
              <span className="text-fg-faint">（满分 5）</span>
            </p>
          )}
        </div>
      )}
    </section>
  )
}
