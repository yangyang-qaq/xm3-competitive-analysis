/**
 * 对比矩阵的朝向。**与后端 `analysis/matrix.py` 是同一条规则**，
 * 改一处就要改另一处。
 *
 * 为什么前端也得判一次
 * ------------------
 * 采集侧现在会把转置的矩阵翻回来（并记一条降级），但那只对**新跑的**
 * 调研生效。库里那份 7 维度 × 5 品牌的报告是在那之前跑出来的，
 * 它的 `scores` 是 7 行 × 5 列——**转置的**。报告页读的是库里的数据，
 * 所以它必须自己判一次。
 *
 * 判错的后果
 * ---------
 * 一张转置的表格和一张正确的表格**长得一模一样**：有表头、有行列、
 * 分数都在 1–5 之间。区别只是每个分数挂在错的品牌/维度上，
 * 而读者无从核对。所以判不出来时**不画**——见 `orientationOf` 的 `unknown`。
 *
 * 为什么"方阵算正常"
 * ----------------
 * 品牌数 == 维度数时两个条件同时成立，形状上不可证伪。这时猜"转置了"
 * 会把一份本来正确的矩阵转坏，而且转坏之后同样看不出来。所以方阵
 * 一律按正常处理——**不确定时不动它**。
 */
import type { ReportMatrix } from '../types/report'

export type MatrixOrientation = 'brandMajor' | 'dimensionMajor' | 'unknown'

/**
 * 按形状判断 `scores` 的行是品牌还是维度。
 *
 * 三个结论的区别决定了页面上的三种表现：
 * - `brandMajor` —— 直接画。
 * - `dimensionMajor` —— 转置后画（**能确定**它反了，所以修正是安全的）。
 * - `unknown` —— 不画表，显示一句说明。宽高都对不上时，一个猜测出来的
 *   表格比一张空表更坏：空表至少诚实。
 */
export function orientationOf(matrix: ReportMatrix | undefined): MatrixOrientation {
  if (!matrix) return 'unknown'
  const { dimensions, brands, scores } = matrix
  if (dimensions.length === 0 || brands.length === 0 || scores.length === 0) return 'unknown'

  const widths = new Set(scores.map((row) => row.length))
  // 每行长度不一致时，连"几行几列"都说不清。
  if (widths.size !== 1) return 'unknown'
  const columns = [...widths][0] ?? 0

  // **方阵先判。** 两个条件同时成立时按正常处理，理由见文件头。
  if (scores.length === brands.length && columns === dimensions.length) return 'brandMajor'
  if (scores.length === dimensions.length && columns === brands.length) return 'dimensionMajor'
  return 'unknown'
}

/**
 * 品牌优先的分数表。判不出来时返回 `null`——调用方据此**不画表**。
 *
 * 返回 `null` 而不是原样返回：原样返回会画出一张转置的表，
 * 而它和正确的表分不出来。把"说不清"这件事往上抛给调用方，
 * 是唯一能让它在界面上变成一句话的办法。
 */
export function brandMajorScores(matrix: ReportMatrix | undefined): number[][] | null {
  const orientation = orientationOf(matrix)
  if (!matrix || orientation === 'unknown') return null
  if (orientation === 'brandMajor') return matrix.scores
  // 转置：`rows[品牌][维度]` ← 原来的 `rows[维度][品牌]`。
  //
  // **外面这层按 `brands` 走，不是按 `dimensions` 走。** 结果的行数
  // 是品牌数（每一行代表一个品牌），列数才等于维度数。写反的话
  // 得到的是同样大小的一份转置结果，所以第一版就是这么写的、
  // 也照样能跑——是"翻过来应该是 5 行 × 7 列"那条用例把它拦下的。
  //
  // `?? 0` 取不到（`orientationOf` 已经保证每行等长），但
  // `noUncheckedIndexedAccess` 让 `row[i]` 的类型是 `number | undefined`。
  // 这里不写 `as number`：真取不到时给 0 是"这个格子没数据"，
  // 而断言会把一个越界读变成一次静默的错位。
  return matrix.brands.map((_, brandIndex) =>
    matrix.scores.map((row) => row[brandIndex] ?? 0),
  )
}
