/**
 * 指标卡。仪表盘用一整排，其它页面也用。
 *
 * `hint` 不是可有可无的注释——**它是这张卡片的可信度来源**。
 * 一个孤零零的"交叉验证率 66%"没法验证，也没法反驳；
 * 配上"经 ≥2 个独立来源相互印证的结论占比"，读的人才知道
 * 这个数是怎么算的，也才能在它不对的时候指出来。
 * 凡是算出来的数，都该说得出算式；说不出的就不该印在仪表盘上。
 */
import type { ReactNode } from 'react'

export function StatCard({
  icon,
  label,
  value,
  unit,
  hint,
}: {
  icon?: ReactNode
  label: string
  value: ReactNode
  /** 单位跟着数字走（"33%""83×"），单独放是为了让它小一号 */
  unit?: string
  hint?: string
}) {
  return (
    <section className="rounded-card border border-line bg-panel px-4 py-4 shadow-card">
      {icon !== undefined && (
        <span className="mb-2.5 flex size-8 items-center justify-center rounded-[10px] bg-brand/10 text-brand">
          {icon}
        </span>
      )}
      <p className="tabular text-[26px] font-semibold leading-none tracking-tight text-fg">
        {value}
        {unit && <span className="ml-0.5 text-[15px] font-medium text-fg-muted">{unit}</span>}
      </p>
      <p className="mt-2 text-[13px] font-medium text-fg">{label}</p>
      {hint && <p className="mt-1 text-[11px] leading-relaxed text-fg-faint">{hint}</p>}
    </section>
  )
}
