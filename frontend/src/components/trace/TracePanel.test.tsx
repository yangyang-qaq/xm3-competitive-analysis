/**
 * 悬浮 trace 面板：三个数字，三种算法。
 *
 * 这一页值得单独测，是因为它**算错了不会有人发现**。界面照常渲染，
 * 数字照常好看，只是含义变了。三种典型的错法：
 *
 * 1. **求和写成取最后一个**（成本、token）。数字仍然是"一个合理的数"，
 *    只是它是"最后一次调用的成本"，而标签写着"累计成本"。
 * 2. **"最慢的一次"写成"最后一次"**。span 是按开始时间落的，不是按耗时，
 *    所以这两个在真实数据里几乎总是不等——但界面上看不出区别。
 * 3. **"命中 0"显示成 `0` 而不是 `—`**。不区分缓存的 provider 恒为 0，
 *    显示 `0` 等于告诉用户"这条链路一次都没命中"，而事实是"这件事没被测量"。
 *    （`0` 与「未知」的塌陷在本项目里已经出过一次，见问题记录 6。）
 *
 * 断言都盯**具体的数**，不盯"页面渲染出来了"。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { SpanKind, SpanStatus, TraceSpan } from '../../types/domain'
import { TracePanel } from './TracePanel'

function span(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return {
    spanId: 'SP-1',
    parentId: '',
    kind: 'llm' as SpanKind,
    name: 'deepseek.chat',
    purpose: 'analyze',
    provider: 'deepseek',
    model: 'deepseek-chat',
    startedAt: '2026-09-18T10:00:00+08:00',
    endedAt: '2026-09-18T10:00:01+08:00',
    durationMs: 1000,
    promptTokens: 100,
    completionTokens: 50,
    totalTokens: 150,
    costUsd: 0.002,
    cachedPromptTokens: 0,
    status: 'ok' as SpanStatus,
    error: '',
    detail: {},
    ...overrides,
  }
}

/** 读"累计成本"那一格。按标签定位，不按顺序——顺序改了就测不到了。 */
function field(label: string): string {
  const labelEl = screen.getByText(label)
  return labelEl.parentElement?.querySelector('p:last-child')?.textContent ?? ''
}

describe('聚合的是总和，不是某一个', () => {
  it('成本与 token 是全部 span 的和', () => {
    render(
      <TracePanel
        spans={[
          span({ spanId: 'A', costUsd: 0.002, totalTokens: 150 }),
          span({ spanId: 'B', costUsd: 0.003, totalTokens: 250 }),
          span({ spanId: 'C', costUsd: 0.001, totalTokens: 100 }),
        ]}
      />,
    )

    // 0.002 + 0.003 + 0.001 = 0.006 —— 取最后一个的话是 $0.0010，
    // 取第一个是 $0.0020，三者互不相等，所以这条断言真的在选。
    expect(field('累计成本')).toBe('$0.0060')
    expect(field('总 token')).toBe('500')
    // 次数用数组长度，不是"某个桶的 count"
    expect(field('调用次数')).toBe('3')
  })

  it('成本为 0 时显示 $0，而不是空或 NaN', () => {
    render(<TracePanel spans={[span({ costUsd: 0 })]} />)
    // mock 的定价是 0，所以这是**最常见的真实情况**，不是边角。
    expect(field('累计成本')).toBe('$0')
  })

  it('命中缓存为 0 时显示破折号，因为"没命中"与"没测量"是两件事', () => {
    render(<TracePanel spans={[span({ cachedPromptTokens: 0 })]} />)
    expect(field('其中命中缓存')).toBe('—')
  })

  it('命中缓存非 0 时显示真实数字', () => {
    render(
      <TracePanel
        spans={[
          span({ spanId: 'A', cachedPromptTokens: 800, totalTokens: 1000 }),
          span({ spanId: 'B', cachedPromptTokens: 200, totalTokens: 500 }),
        ]}
      />,
    )
    // 是 totalTokens 的**子集**，所以两个数各算各的、不能混。
    expect(field('其中命中缓存')).toBe('1,000')
    expect(field('总 token')).toBe('1,500')
  })
})

describe('最慢的一次是取最大值，不是取最后一条', () => {
  it('慢的那条在中间时也能找出来', () => {
    render(
      <TracePanel
        spans={[
          span({ spanId: 'A', name: 'first', purpose: 'plan', durationMs: 100 }),
          span({ spanId: 'B', name: 'slowest', purpose: 'write', durationMs: 9000 }),
          span({ spanId: 'C', name: 'last', purpose: 'audit', durationMs: 200 }),
        ]}
      />,
    )

    // 取最后一条（`spans[spans.length-1]`）会得到 'audit'，取第一条得到 'plan'。
    expect(screen.getByText('write')).toBeInTheDocument()
    expect(screen.getByText(/9,000ms/)).toBeInTheDocument()
  })

  it('耗时相同时不崩，且给出一条', () => {
    render(
      <TracePanel
        spans={[
          span({ spanId: 'A', purpose: 'one', durationMs: 500 }),
          span({ spanId: 'B', purpose: 'two', durationMs: 500 }),
        ]}
      />,
    )
    // 并列时取哪条无所谓，但必须**有**一条——这里不锁定具体是哪条，
    // 因为那是实现的自由，锁死了只会让无害的重构变红。
    expect(screen.getByText(/500ms/)).toBeInTheDocument()
  })

  it('`purpose` 为空时退回 `name`，不显示空白行', () => {
    render(
      <TracePanel spans={[span({ purpose: '', name: 'collect.search', durationMs: 700 })]} />,
    )
    expect(screen.getByText('collect.search')).toBeInTheDocument()
  })
})

describe('按类型分组', () => {
  it('每种类型一行，次数与耗时都是该类之和', () => {
    render(
      <TracePanel
        spans={[
          span({ spanId: 'A', kind: 'llm', durationMs: 1000, costUsd: 0.002 }),
          span({ spanId: 'B', kind: 'llm', durationMs: 500, costUsd: 0.001 }),
          span({ spanId: 'C', kind: 'search', durationMs: 300, costUsd: 0 }),
        ]}
      />,
    )

    // 模型一行、检索一行，且**没有**抓取/阶段两行（它们一个 span 都没有）。
    expect(screen.getByText('模型')).toBeInTheDocument()
    expect(screen.getByText('检索')).toBeInTheDocument()
    expect(screen.queryByText('抓取')).not.toBeInTheDocument()
    expect(screen.queryByText('阶段')).not.toBeInTheDocument()

    // llm 桶：2 次、1500ms、$0.003；检索桶：1 次、300ms。
    // 用标签定位到那一行再读整行文本，避免"1,500"和别处的数字撞上。
    const llmRow = screen.getByText('模型').closest('li')
    expect(llmRow?.textContent).toContain('×2')
    expect(llmRow?.textContent).toContain('$0.0030')
    expect(llmRow?.textContent).toContain('1,500ms')

    const searchRow = screen.getByText('检索').closest('li')
    expect(searchRow?.textContent).toContain('×1')
    expect(searchRow?.textContent).toContain('300ms')
  })

  it('同一类型不重复出行——分组是聚合，不是逐条渲染', () => {
    render(
      <TracePanel
        spans={[
          span({ spanId: 'A', kind: 'fetch' }),
          span({ spanId: 'B', kind: 'fetch' }),
          span({ spanId: 'C', kind: 'fetch' }),
        ]}
      />,
    )
    // 每个 span 单独一行的话这里会找到 3 条，而 `getByText` 在多条时抛错。
    expect(screen.getAllByText('抓取')).toHaveLength(1)
    expect(screen.getByText('抓取').closest('li')?.textContent).toContain('×3')
  })
})

describe('空输入', () => {
  it('没有 span 时说清楚，而不是渲染一堆 0', () => {
    render(<TracePanel spans={[]} />)
    expect(screen.getByText('还没有调用记录。')).toBeInTheDocument()
    // 一个 0 都不该出现：那会让人以为"跑过了，但什么都没花"。
    expect(screen.queryByText('累计成本')).not.toBeInTheDocument()
  })
})
