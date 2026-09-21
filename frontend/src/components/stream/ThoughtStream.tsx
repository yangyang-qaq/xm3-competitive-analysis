/**
 * 专家思维流。工作台的主视图。
 *
 * 三条与"看起来只是列表"不同的地方：
 *
 * 1. **自动贴底，但只在你已经在底部的时候。** 无条件贴底会让用户
 *    往回翻的时候被拽回底部——而"往回翻"正是这个面板唯一的交互。
 * 2. **上限之后丢的是最旧的**（在 store 里做的）。这里只负责渲染，
 *    不再截一次：两处各截一次，两处的上限迟早不一样。
 * 3. 名字用的是 `level` 而不是 id 前缀。名册里查不到那位专家时
 *    `roleTitle` 是空串——那时**不渲染那个空标签**，留白比一个
 *    "未知角色"的占位更诚实。
 */
import { useEffect, useRef } from 'react'

import { formatRelative } from '../../lib/format'
import type { Thought } from '../../types/domain'

const LEVEL_COLOR: Record<string, string> = {
  L3: 'text-l3',
  L2: 'text-l2',
  L1: 'text-l1',
}

function ThoughtRow({ thought }: { thought: Thought }) {
  const levelColor = LEVEL_COLOR[thought.level] ?? 'text-fg-faint'
  return (
    <li className="border-b border-line/60 py-2 last:border-b-0">
      <div className="flex items-baseline gap-2">
        <span className={['shrink-0 font-mono text-[11px]', levelColor].join(' ')}>
          {thought.level || '—'}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-fg">
          {thought.expertName}
        </span>
        <span className="shrink-0 text-[11px] text-fg-faint">{formatRelative(thought.at)}</span>
      </div>
      <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-fg-muted">
        {thought.text}
      </p>
    </li>
  )
}

export function ThoughtStream({ thoughts }: { thoughts: Thought[] }) {
  const boxRef = useRef<HTMLDivElement>(null)
  /**
   * 用户此刻是不是贴着底部。
   *
   * **必须由滚动事件维护，不能在 `useEffect` 里现算。** effect 跑在渲染
   * 提交之后，那时新的一条已经进了 DOM，`scrollHeight` 也涨了——
   * 于是"刚才到底贴不贴底"这个信息已经没了，现算出来的是**加完之后**
   * 的距离，永远大于余量，结果就是再也不自动跟随。
   */
  const stickRef = useRef(true)
  // 严格等于底部是判不到的：浏览器在 sub-pixel 上有误差，
  // 用 `<= 0` 会有一半的时候判成"不在底部"。
  const STICK_SLACK = 24

  useEffect(() => {
    const box = boxRef.current
    if (!box || !stickRef.current) return
    box.scrollTop = box.scrollHeight
  }, [thoughts.length])

  if (thoughts.length === 0) {
    return (
      <p className="py-6 text-center text-xs text-fg-faint">
        还没有思维记录。流水线跑到采集阶段之后会陆续出现。
      </p>
    )
  }

  return (
    <div
      ref={boxRef}
      className="h-full overflow-y-auto"
      onScroll={(event) => {
        const box = event.currentTarget
        stickRef.current = box.scrollHeight - box.scrollTop - box.clientHeight <= STICK_SLACK
      }}
    >
      <ul>
        {thoughts.map((thought) => (
          <ThoughtRow key={thought.id} thought={thought} />
        ))}
      </ul>
    </div>
  )
}
