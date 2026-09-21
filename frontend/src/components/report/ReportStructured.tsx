/**
 * 结构化块：市场份额、五力、趋势。
 *
 * 这三块的共同点是**它们都是数字，而数字的单位不由前端决定**。
 * 这一页的态度是：宁可显示一个带问号的数，不显示一个看起来很确定的错数。
 * 两个具体的例子（都来自库里的真实报告）：
 *
 * - `marketShare[].share` 里有一份存着 `2022.0`，而 `basis` 字段自己写着
 *   "Notion 用户规模 3000 万"——模型把人数填进了份额位。份额不可能超过
 *   整体，所以越界就是单位错了。这里**照原值显示并标出来**，不除、不猜。
 *   （后端 `charts.py` 的份额闸据此不出饼图。）
 * - `fiveForces[].intensity` 缺失时后端原先补 `3.0`，于是"没判出强度"
 *   在报告里变成"强度中等"。现在补 `0`，这一页把 0 显示成"未判定"。
 *
 * 空数组照实说
 * ----------
 * 最大的那份报告里 `marketShare` 和 `trends` 都是 `[]`。整块不渲染的话，
 * 读者不知道是"这次没做这一项"还是"做了但没采到"——所以每块都渲染，
 * 空了就说一句。（`degraded` 里通常也有一条对应的说明。）
 */
import type { ReactNode } from 'react'

import { formatPercent } from '../../lib/format'
import type { FiveForceItem, MarketShareItem, TrendSeries } from '../../types/report'
import { ReportPanel } from './ReportPanel'

/** 份额是不是一个像样的占比。判据与后端 `charts._market_share_chart` 的份额闸相同。 */
function isPlausibleShare(share: number): boolean {
  return share > 0 && share <= 100
}

function Nothing({ children }: { children: ReactNode }) {
  return <p className="text-[12px] text-fg-faint">{children}</p>
}

// ---------------------------------------------------------------- 市场份额

function MarketShare({ items }: { items: MarketShareItem[] }) {
  if (items.length === 0) {
    return <Nothing>这次没有采到可用的市场份额数据。</Nothing>
  }

  // 整个块是"单位存疑"还是"只有个别越界"，决定提示写在哪。
  const suspect = items.filter((item) => !isPlausibleShare(item.share))

  return (
    <div className="flex flex-col gap-2">
      {suspect.length > 0 && (
        <p className="rounded-lg border border-warn/40 bg-warn/8 px-3 py-2 text-[11px] leading-relaxed text-fg-muted">
          有 <span className="tabular">{suspect.length}</span> 条的数值不在 0–100 区间里，
          <span className="font-medium">不可能是占比</span>。下方按原值显示并标出来
          ——不换算、不猜单位，因为正确的数我们并不知道。
        </p>
      )}
      <ul className="flex flex-col">
        {items.map((item) => (
          <li
            key={`${item.brand}-${item.share}`}
            className="flex items-baseline gap-2 border-t border-line py-1.5 first:border-t-0"
          >
            <span className="min-w-[6em] shrink-0 text-[12px] text-fg">{item.brand}</span>
            {isPlausibleShare(item.share) ? (
              // `share` 已经是 0–100 的数（41.0 表示 41%），
              // 而 `formatPercent` 收的是 0–1 的比例，所以要除 100。
              // 直接拼 `${share}%` 会更直白，但那样小数位就由数据决定了
              // （41.0 → "41%"、27.44 → "27.44%"），一列数上下对不齐。
              <span className="shrink-0 text-[12px] tabular text-fg">
                {formatPercent(item.share / 100, 1)}
              </span>
            ) : (
              <span
                className="shrink-0 rounded border border-warn/40 bg-warn/10 px-1.5 text-[12px] tabular text-warn"
                title="这个数值不在 0–100 区间，不是占比——按原值显示"
              >
                {item.share}
              </span>
            )}
            <span className="text-[11px] leading-relaxed text-fg-faint">{item.basis}</span>
            {item.evidenceIds.length > 0 && (
              <span className="ml-auto shrink-0 text-[10px] text-fg-faint tabular">
                {item.evidenceIds.length} 条
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

// ------------------------------------------------------------------- 五力

function FiveForces({ items }: { items: FiveForceItem[] }) {
  if (items.length === 0) {
    return <Nothing>这次没有做五力分析（快速档位不做）。</Nothing>
  }

  return (
    <ul className="flex flex-col">
      {items.map((item) => {
        const known = item.intensity > 0
        return (
          <li key={item.force} className="border-t border-line py-2 first:border-t-0">
            <div className="flex items-baseline gap-2">
              <span className="text-[12px] text-fg">{item.force}</span>
              {known ? (
                // 五格刻度比一句"强度 4"更好读：读者要的是"强不强"，
                // 而不是"4 分"。
                <span className="flex items-center gap-0.5" title={`强度 ${item.intensity} / 5`}>
                  {[1, 2, 3, 4, 5].map((step) => (
                    <span
                      key={step}
                      className={[
                        'h-1.5 w-3 rounded-sm',
                        step <= item.intensity ? 'bg-brand' : 'bg-raised',
                      ].join(' ')}
                    />
                  ))}
                </span>
              ) : (
                // **0 分是"没判出强度"，不是"强度为零"。** 后端原先缺省补 3.0，
                // 那时这里会画出一根三格的条——一个看起来很确定的判断，
                // 而它是默认值。现在缺省补 0，页面终于分得清这两件事。
                <span className="rounded bg-raised px-1.5 text-[10px] text-fg-faint">
                  未判定
                </span>
              )}
              {item.evidenceIds.length > 0 && (
                <span className="ml-auto shrink-0 text-[10px] text-fg-faint tabular">
                  {item.evidenceIds.length} 条
                </span>
              )}
            </div>
            {/* `analysis` 可以是空串（模型给了 name 却没写分析），
                这时不占一行空白——空段落看起来像渲染坏了。 */}
            {item.analysis && (
              <p className="mt-1 text-[11.5px] leading-relaxed text-fg-muted">{item.analysis}</p>
            )}
          </li>
        )
      })}
    </ul>
  )
}

// ------------------------------------------------------------------- 趋势

function Trends({ items }: { items: TrendSeries[] }) {
  if (items.length === 0) {
    return <Nothing>这次没有采到可比的趋势数据。</Nothing>
  }

  return (
    <div className="flex flex-col gap-3">
      {items.map((item) => (
        <div key={item.name}>
          <div className="flex items-baseline gap-2">
            <span className="text-[12px] text-fg">{item.name}</span>
            {item.unit && <span className="text-[10px] text-fg-faint">单位：{item.unit}</span>}
            {item.evidenceIds.length > 0 && (
              <span className="ml-auto shrink-0 text-[10px] text-fg-faint tabular">
                {item.evidenceIds.length} 条
              </span>
            )}
          </div>
          {/* 表格给的是**确切的数**，图给的是形状。两点数据画成折线看起来
              像一条趋势，而它其实只是两个点——所以数一定要有地方看。 */}
          <table className="mt-1 border-collapse text-[11.5px]">
            <thead>
              <tr className="text-fg-faint">
                {item.points.map((point) => (
                  <th key={point.period} className="pr-4 text-left font-medium">
                    {point.period}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                {item.points.map((point) => (
                  <td key={point.period} className="pr-4 tabular text-fg">
                    {point.value}
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
        </div>
      ))}
    </div>
  )
}

// ------------------------------------------------------------------ 出口

export interface ReportStructuredProps {
  marketShare: MarketShareItem[] | undefined
  fiveForces: FiveForceItem[] | undefined
  trends: TrendSeries[] | undefined
}

export function ReportStructured({ marketShare, fiveForces, trends }: ReportStructuredProps) {
  return (
    <>
      <ReportPanel id="market-share" title="市场份额" count={marketShare?.length}>
        <MarketShare items={marketShare ?? []} />
      </ReportPanel>
      <ReportPanel id="five-forces" title="波特五力" count={fiveForces?.length}>
        <FiveForces items={fiveForces ?? []} />
      </ReportPanel>
      <ReportPanel id="trends" title="趋势" count={trends?.length}>
        <Trends items={trends ?? []} />
      </ReportPanel>
    </>
  )
}
