/**
 * 功能评分矩阵。**这一页里唯一可能拒绝渲染的块。**
 *
 * 为什么不是"有就画"
 * ----------------
 * `scores` 的行到底是品牌还是维度，只在形状上看得出来：后端按
 * **品牌优先**约定写（`scores[品牌下标][维度下标]`），但这个约定
 * 没有任何东西守着，模型偶尔反着给。库里 14 份报告里有一份就是
 * 7 维度 × 5 品牌存成 7 行 × 5 列。
 *
 * 一张转置的表和一张正确的表长得一模一样——有表头、有行列、分数都在
 * 1–5 之间，只是每个数字挂在错的品牌/维度上，而读者**没有任何办法
 * 核对**。所以这里的态度是：
 *
 * - 形状能确定（品牌优先）→ 画。
 * - 形状能确定反了（维度优先）→ 翻过来画，**并且说一句**。
 *   不说的话，同一份数据在图表里和在表格里是两副样子，而读者会以为
 *   表格错了。
 * - 形状说不清 → **不画表**，把行列数字印出来，让读者知道是什么对不上。
 *   一个猜出来的表格比一张空表坏得多。
 *
 * 为什么表里的证据是一个总数而不是每格一条
 * --------------------------------------
 * 后端给的就是一个扁平的 `evidenceIds`（见 `pipeline/analyze.py` 的
 * `ctx.matrix`），没有逐格归属。这里**不编**一个"每格挂一条"的映射：
 * 编出来的映射看起来更精确，而它是假的。总数至少是真的。
 */
import { brandMajorScores, orientationOf } from '../../lib/reportMatrix'
import type { ReportMatrix as MatrixData } from '../../types/report'

/** 分数 → 底色深浅。1–5 的整数分制，越深越高。 */
const TINT: Record<number, string> = {
  1: 'bg-brand/6',
  2: 'bg-brand/14',
  3: 'bg-brand/24',
  4: 'bg-brand/38',
  5: 'bg-brand/55',
}

function cellTint(score: number): string {
  const bucket = Math.max(1, Math.min(5, Math.round(score)))
  return TINT[bucket] ?? 'bg-brand/6'
}

export function ReportMatrix({ matrix }: { matrix: MatrixData | undefined }) {
  if (!matrix) return null

  const orientation = orientationOf(matrix)
  const scores = brandMajorScores(matrix)

  if (scores === null) {
    const widths = matrix.scores.map((row) => row.length)
    const width = widths.length > 0 && new Set(widths).size === 1 ? widths[0] : null
    const shape = width === null || width === undefined
      ? `${matrix.scores.length} 行（各行长度还不一致）`
      : `${matrix.scores.length} 行 × ${width} 列`

    return (
      <section
        id="matrix"
        className="scroll-mt-[88px] rounded-card border border-line bg-panel p-4 shadow-card"
      >
        <h3 className="text-[13px] font-medium text-fg">功能评分矩阵</h3>
        <div className="mt-2 rounded-lg border border-warn/40 bg-warn/8 px-3 py-2.5">
          <p className="text-[12px] text-warn">这一版的矩阵形状对不上，不画表。</p>
          <p className="mt-1 text-[11px] leading-relaxed text-fg-muted">
            实际是 {shape}，而报告说 {matrix.dimensions.length} 个维度 ×{' '}
            {matrix.brands.length} 个品牌。
          </p>
          <p className="mt-1.5 text-[11px] leading-relaxed text-fg-faint">
            {/* 把"为什么不猜"写在页面上而不是只在代码里：
                读者看到一块空白会以为这是渲染坏了，而这是**故意的**。 */}
            行和列对不上时，转置与不转置都会得到一张看着正常的表，
            而里面每个分数都挂在错的地方——所以这里选择不画。
          </p>
        </div>
      </section>
    )
  }

  return (
    <section
      id="matrix"
      className="scroll-mt-[88px] rounded-card border border-line bg-panel p-4 shadow-card"
    >
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[13px] font-medium text-fg">功能评分矩阵</h3>
        <span className="shrink-0 text-[11px] text-fg-faint tabular">
          {matrix.evidenceIds.length} 条证据
        </span>
      </div>

      {orientation === 'dimensionMajor' && (
        // 说一句，而且是**具体的**一句。只写"数据已修正"等于没说：
        // 读者对照导出或别人的截图时会发现行列不一样，那时他需要知道
        // 是谁动了它、凭什么。
        <p className="mt-2 rounded-lg border border-warn/40 bg-warn/8 px-3 py-2 text-[11px] leading-relaxed text-fg-muted">
          库里这份数据存成了 <span className="tabular">{matrix.scores.length} 行 ×{' '}
          {matrix.dimensions.length} 列</span>（行是维度、列是品牌），
          与惯例相反。下表已按品牌优先翻回来，<span className="text-fg">行列以本表为准</span>。
        </p>
      )}

      <div className="mt-3 overflow-x-auto">
        <table className="w-full border-collapse text-[12px]">
          <thead>
            <tr>
              <th className="sticky left-0 bg-panel px-2 py-1.5 text-left font-medium text-fg-faint">
                品牌
              </th>
              {matrix.dimensions.map((dimension, index) => (
                <th
                  key={`${dimension}-${index}`}
                  className="px-2 py-1.5 text-center font-medium text-fg-muted"
                >
                  {dimension}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {matrix.brands.map((brand, brandIndex) => (
              <tr key={brand} className="border-t border-line">
                <th className="sticky left-0 bg-panel px-2 py-1.5 text-left font-medium text-fg">
                  {brand}
                </th>
                {(scores[brandIndex] ?? []).map((score, dimensionIndex) => (
                  <td
                    key={dimensionIndex}
                    title={`${brand} · ${matrix.dimensions[dimensionIndex] ?? '—'}：${score}`}
                    className={[
                      'px-2 py-1.5 text-center tabular text-fg',
                      cellTint(score),
                    ].join(' ')}
                  >
                    {/* 0 分是"没判出来"，不是"得了 0 分"。矩阵是 1–5 分制，
                        所以 0 落到这里只可能是缺值——印成 `—` 而不是 `0`，
                        免得读者以为这个维度真的很差。 */}
                    {score > 0 ? score : '—'}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
