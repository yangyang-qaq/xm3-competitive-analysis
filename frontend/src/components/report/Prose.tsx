/**
 * 一节正文。段落按空行切，`[证据: EV-x]` 渲染成**可点的角标**。
 *
 * 角标是这一页的主交互：读者读到一个论断，想看看它是拿什么推出来的，
 * 于是点一下 `[3]`，右边的证据面板滚到第 3 条并高亮。
 * 所以角标必须是 `<button>` 而不是 `<sup>`——键盘用户也要能用，
 * 而 `<sup>` 加上 `onClick` 是一个不能聚焦、也不能按回车的东西。
 *
 * 引用不存在的证据时显示 `[?]`
 * ---------------------------
 * 导出那边也是这么做的（`export.py` 的 `_CitationIndex.number`），
 * 理由一样：**看得见的坏比看不见的坏好。** 一个被悄悄吃掉的角标会让
 * 读者以为这句话本来就不需要证据，而它曾经引用过一条（可能已被删掉的）证据。
 *
 * `[?]` **不可点**：它没有可显示的卡片，点开是一片空白，
 * 而空白会被读成"加载失败"。它带一个 `title` 说明为什么。
 */
import type { Segment } from '../../lib/reportCitation'
import { splitCitations } from '../../lib/reportCitation'

export interface ProseProps {
  text: string
  numbers: Map<string, number>
  /** 右边面板当前高亮的那一条。角标据此显示选中态 */
  activeEvidenceId: string | null
  onCite: (evidenceId: string) => void
}

function Citation({
  segment,
  activeEvidenceId,
  onCite,
}: {
  segment: Extract<Segment, { kind: 'citation' }>
  activeEvidenceId: string | null
  onCite: (evidenceId: string) => void
}) {
  if (segment.number === null) {
    return (
      <span
        className="mx-0.5 cursor-help rounded border border-warn/40 bg-warn/10 px-1 align-[1px] font-mono text-[10px] text-warn"
        title={`这条引用指向 ${segment.evidenceId}，但证据表里没有它`}
      >
        ?
      </span>
    )
  }

  const active = segment.evidenceId === activeEvidenceId
  return (
    <button
      type="button"
      onClick={() => onCite(segment.evidenceId)}
      title={`跳到第 ${segment.number} 条证据`}
      aria-label={`跳到第 ${segment.number} 条证据`}
      className={[
        'mx-0.5 cursor-pointer rounded border px-1 align-[1px] font-mono text-[10px] tabular transition-colors',
        active
          ? 'border-brand bg-brand text-canvas'
          : 'border-brand/40 bg-brand/10 text-brand hover:bg-brand/20',
      ].join(' ')}
    >
      {segment.number}
    </button>
  )
}

/**
 * 按空行切段。
 *
 * **不加 `white-space: pre-wrap` 来省这一步**：正文里最长的一段有七八百字，
 * 靠浏览器折行没有问题，但空行会变成一个看不见的分段——而"段落"是这段
 * 文字唯一的层次。切成 `<p>` 之后，段间距是布局管的，不是字符管的。
 *
 * 段内的单个换行按空格处理：那是模型输出的习惯，不是排版意图。
 */
export function Prose({ text, numbers, activeEvidenceId, onCite }: ProseProps) {
  const paragraphs = text
    .split(/\n\s*\n/)
    .map((block) => block.trim())
    .filter(Boolean)

  if (paragraphs.length === 0) {
    return <p className="text-[13px] text-fg-faint">这一节没有正文。</p>
  }

  return (
    <div className="flex flex-col gap-3">
      {paragraphs.map((paragraph, index) => (
        <p key={index} className="text-[13.5px] leading-[1.85] text-fg">
          {splitCitations(paragraph, numbers).map((segment, position) =>
            segment.kind === 'citation' ? (
              <Citation
                key={position}
                segment={segment}
                activeEvidenceId={activeEvidenceId}
                onCite={onCite}
              />
            ) : (
              // 纯文本片段直接渲染成字符串，**不包一层 `<span>`**。
              // 包了没有任何好处，而浏览器在元素边界处会多出一个换行机会
              // （中文正文逐字断行，影响很小；但正文里混着 `Notion`、
              // `Remotely Save` 这类英文词时，悬挂行会出现得毫无道理）。
              segment.text
            ),
          )}
        </p>
      ))}
    </div>
  )
}
