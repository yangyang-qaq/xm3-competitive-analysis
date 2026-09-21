/**
 * 加载 / 出错 / 空 三种状态。
 *
 * 三个页面共用一份，理由和 `PageHeader` 一样；但这里还有一个更实际的：
 * **"出错"必须每次都把后端地址写出来**。这个项目的常见故障是
 * "后端没起来"，而那时页面上一句"加载失败"会让人去怀疑数据、
 * 怀疑筛选条件，唯独不会想到是服务没开。把那条命令印在错误块里，
 * 一次就能排除掉最常见的那种可能。
 */
import type { ReactNode } from 'react'

export function Loading({ what = '数据' }: { what?: string }) {
  return <p className="py-10 text-center text-[13px] text-fg-faint">正在读取{what}…</p>
}

export function ErrorNote({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="rounded-card border border-danger/40 bg-danger/5 px-4 py-3">
      <p className="text-[13px] text-danger">连不上后端：{error.message}</p>
      <p className="mt-1.5 text-[11px] leading-relaxed text-fg-faint">
        确认后端已在 8020 端口启动：
        <code className="mx-1 rounded bg-raised px-1.5 py-0.5 font-mono">
          .venv/Scripts/python.exe -m uvicorn app.main:app --port 8020
        </code>
      </p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-2.5 rounded-lg border border-line px-3 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
        >
          重试
        </button>
      )}
    </div>
  )
}

export function Empty({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="rounded-card border border-dashed border-line-strong px-6 py-14 text-center">
      <p className="text-[13px] text-fg-muted">{title}</p>
      {hint && <p className="mt-2 text-[11px] leading-relaxed text-fg-faint">{hint}</p>}
    </div>
  )
}
