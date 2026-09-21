import { describe, expect, it } from 'vitest'

import { credibilityTier, formatCost, formatDuration, formatPercent } from './format'

describe('formatDuration', () => {
  it('一分钟以内用秒', () => {
    expect(formatDuration(45)).toBe('45 秒')
  })

  it('整分钟不显示多余的 0 秒', () => {
    expect(formatDuration(180)).toBe('3 分')
  })

  it('分秒混排', () => {
    expect(formatDuration(192)).toBe('3 分 12 秒')
  })

  it('非法输入不抛异常，返回占位符', () => {
    expect(formatDuration(Number.NaN)).toBe('—')
    expect(formatDuration(-1)).toBe('—')
  })
})

describe('formatCost', () => {
  it('零成本不显示 $0.0000', () => {
    expect(formatCost(0)).toBe('$0')
  })

  it('小额成本保留四位，否则全是 $0.00 看不出差别', () => {
    expect(formatCost(0.0032)).toBe('$0.0032')
  })

  it('常规金额两位小数', () => {
    expect(formatCost(1.2345)).toBe('$1.23')
  })
})

describe('formatPercent', () => {
  it('把 0–1 的比例转成百分比', () => {
    expect(formatPercent(0.713)).toBe('71%')
  })

  it('支持指定小数位', () => {
    expect(formatPercent(0.713, 1)).toBe('71.3%')
  })
})

describe('credibilityTier', () => {
  it('分档边界与后端语义一致（70 / 45）', () => {
    expect(credibilityTier(70)).toBe('high')
    expect(credibilityTier(69)).toBe('medium')
    expect(credibilityTier(45)).toBe('medium')
    expect(credibilityTier(44)).toBe('low')
  })
})
