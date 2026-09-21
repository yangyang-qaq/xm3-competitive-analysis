/**
 * 一节底下的批注工具条：写一句批注，或者按这句批注深化这一节。
 *
 * 为什么是**一个**输入框而不是两个
 * ----------------------------
 * "提交批注"和"按批注深化"用的是同一句话。分成两个输入框的话，
 * 用户会以为它们的语义不同（一个留言、一个指令），于是分别写两句，
 * 而实际上后端最后存的都是 `report_feedback.content`。
 * 共用一句、两个按钮，语义就摆在按钮上：
 * **只留言**，或者**留言并重写**。
 *
 * 深化是这一页唯一会花几十秒的按钮
 * ----------------------------
 * 它要再搜一轮、再调一次模型。所以：
 * - 点下去之后按钮变成进行中的样子，两个按钮都禁用（不能重复提交）；
 * - **失败时不清空输入框**。整页里最气人的事是写了三百字批注、
 *   请求超时、字没了。所以只有在成功之后才清空。
 *
 * `search` 开关的文案要说清楚代价
 * ----------------------------
 * "再搜一轮新证据"多花检索与抓取；关掉则只重写。默认开着——深化
 * 的常见理由就是"这一节的证据不够"。但把开关写成 `search: on/off`
 * 没人知道那是什么意思，所以标签直接写它花什么、换什么。
 */
import { useState } from 'react'

import { api, ApiError } from '../../lib/api'
import type { RefineResult } from '../../lib/api'

export interface SectionAnnotatorProps {
  reportId: string
  sectionKey: string
  /** 深化成功后的回填。页面据此替换掉这一节并更新质量门 */
  onRefined: (result: RefineResult) => void
  /** 批注成功后的回调。页面据此更新"已提交 N 条批注" */
  onAnnotated: (count: number) => void
}

export function SectionAnnotator({
  reportId,
  sectionKey,
  onRefined,
  onAnnotated,
}: SectionAnnotatorProps) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [search, setSearch] = useState(true)
  const [busy, setBusy] = useState<'none' | 'annotate' | 'refine'>('none')
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const trimmed = text.trim()
  const canSubmit = trimmed.length > 0 && busy === 'none'

  async function annotate() {
    if (!canSubmit) return
    setBusy('annotate')
    setError(null)
    setNote(null)
    try {
      const result = await api.addFeedback(reportId, { content: trimmed, sectionKey })
      onAnnotated(result.count)
      setText('')
      setNote('批注已记录。')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy('none')
    }
  }

  async function refine() {
    if (!canSubmit) return
    setBusy('refine')
    setError(null)
    setNote(null)
    try {
      const result = await api.refineReport(reportId, {
        annotation: trimmed,
        sectionKey,
        search,
      })
      onRefined(result)
      setText('')
      // 深化**不一定**变得更好：可能搜不到新证据，于是这一节只是换了个
      // 说法。所以回执里把实际结果说出来，而不是笼统地写"已完成"。
      setNote(
        result.addedEvidences > 0
          ? `这一节已重写，新增 ${result.addedEvidences} 条证据。`
          : '这一节已重写，但没有采到新证据——只是换了个写法。',
      )
    } catch (err) {
      // **不清空 `text`**：写好的批注要能重试。
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy('none')
    }
  }

  if (!open) {
    return (
      <div className="mt-4 border-t border-line pt-2">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="rounded-lg px-2 py-1 text-[11px] text-fg-faint transition-colors hover:bg-raised hover:text-fg-muted"
        >
          批注 / 深化本节
        </button>
      </div>
    )
  }

  return (
    <div className="mt-4 border-t border-line pt-3">
      <textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        rows={2}
        maxLength={2000}
        disabled={busy !== 'none'}
        placeholder="这一节哪里不对？比如「只比较了功能，没提迁移成本」「价格那段的来源是 2024 年的，已经过期」"
        className="w-full resize-y rounded-lg border border-line bg-canvas px-3 py-2 text-[12px] leading-relaxed text-fg placeholder:text-fg-faint focus:border-brand focus:outline-none disabled:opacity-60"
      />

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={annotate}
          disabled={!canSubmit}
          className="rounded-lg border border-line px-3 py-1 text-[11.5px] text-fg-muted transition-colors hover:border-line-strong hover:text-fg disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy === 'annotate' ? '提交中…' : '只记批注'}
        </button>

        <button
          type="button"
          onClick={refine}
          disabled={!canSubmit}
          title="按这句批注重写这一节。会调用模型，需要几十秒"
          className="rounded-lg bg-brand px-3 py-1 text-[11.5px] text-canvas transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy === 'refine' ? '深化中…（要几十秒）' : '按批注深化'}
        </button>

        {/* 开关用 `<label>` 包住 `<input>`，点文字也能勾上——
            单点那个小方块是件很烦的事。 */}
        <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-fg-muted">
          <input
            type="checkbox"
            checked={search}
            disabled={busy !== 'none'}
            onChange={(event) => setSearch(event.target.checked)}
            className="accent-brand"
          />
          再搜一轮新证据（不勾则只重写）
        </label>

        <button
          type="button"
          onClick={() => {
            setOpen(false)
            setError(null)
            setNote(null)
          }}
          disabled={busy !== 'none'}
          className="ml-auto rounded-lg px-2 py-1 text-[11px] text-fg-faint hover:text-fg-muted disabled:opacity-50"
        >
          收起
        </button>
      </div>

      {error && (
        <p className="mt-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-1.5 text-[11px] text-danger">
          没成功：{error}
          <span className="text-fg-faint">（批注还在框里，可以直接重试）</span>
        </p>
      )}
      {note && !error && (
        <p className="mt-2 rounded-lg bg-raised px-3 py-1.5 text-[11px] text-fg-muted">{note}</p>
      )}
    </div>
  )
}
