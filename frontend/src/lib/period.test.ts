/**
 * 时间标签解析与排序。
 *
 * 这组用例里**真正承重的是跨写法比较**（季度 vs 月份 vs 年份）和
 * **"排不了"的那两条**。单个格式自己跟自己比（`2024Q1` vs `2024Q2`）
 * 就算实现里只是拿字符串比大小也常常能对——那种用例证明不了什么。
 */
import { describe, expect, it } from 'vitest'

import { orderPeriods, periodKey } from './period'

describe('认得出来的写法', () => {
  it.each([
    ['2024-01', 202401],
    ['2024/01', 202401],
    ['2024.1', 202401],
    ['2024年1月', 202401],
    ['2024 年 12 月', 202412],
    ['2024', 202400],
  ])('%s → %i', (text, key) => {
    expect(periodKey(text)).toBe(key)
  })

  it('季度折成它的第一个月', () => {
    // `2024Q1` 与 `2024年1月` 必须落在同一个键上，否则两种写法混用时
    // 会排出一段错的先后。
    expect(periodKey('2024Q1')).toBe(202401)
    expect(periodKey('2024q3')).toBe(202407)
    expect(periodKey('2024年第4季度')).toBe(202410)
    expect(periodKey('2024年第2季')).toBe(202404)
  })
})

describe('认不出来的写法', () => {
  it.each([['近期'], ['上个月'], ['H1 2024'], [''], ['   '], ['2024年'], ['24Q1']])(
    '%s → null',
    (text) => {
      // 相对说法（"近期"）刻意不认：它们相对的是**写作时间**，而报告里
      // 至少有三个不同的"现在"（证据发布时间、报告生成时间、读者的当前时间）。
      // 挑一个映射过去就等于替报告编了一条时间轴。
      expect(periodKey(text)).toBeNull()
    },
  )

  it('不存在的月份返回 null', () => {
    // 放过去的话，"13 月"会排到 12 月之后、次年 1 月之前——
    // 一个不存在的位置，而它在轴上看起来完全正常。
    expect(periodKey('2024-13')).toBeNull()
    expect(periodKey('2024-00')).toBeNull()
  })
})

describe('排序', () => {
  it('跨年、跨写法都能排对', () => {
    // 这条用例只拿字符串比大小是做不出来的：'2024-01' < '2024Q1' 是
    // 字母序的巧合，而 '2024-12' < '2024Q1' 按字母序**成立**、按时间不成立。
    const { periods, ordered } = orderPeriods(['2024Q1', '2023年12月', '2024-03', '2024'])
    expect(ordered).toBe(true)
    expect(periods).toEqual(['2023年12月', '2024', '2024Q1', '2024-03'])
  })

  it('排不了时原样返回并报告 ordered: false', () => {
    // 一部分认得出、一部分认不出——**这是最危险的一种**：能排的排好了，
    // 认不出的原地不动，于是整条轴看着有序、实际错乱。所以调用方要能
    // 从返回值里看出来"这不是时间序"。
    const input = ['2024-03', '近期', '2024-01']
    const { periods, ordered } = orderPeriods(input)
    expect(ordered).toBe(false)
    expect(periods).toEqual(input)
  })

  it('全部认不出时也是 ordered: false，而不是"排好了"', () => {
    // 空数组是唯一的例外（trivially ordered）——没有轴要画，
    // 谈不上排不出序。见下一条。
    const { ordered } = orderPeriods(['第一阶段', '第二阶段'])
    expect(ordered).toBe(false)
  })

  it('空数组算排好了', () => {
    expect(orderPeriods([])).toEqual({ periods: [], ordered: true })
  })

  it('键相同时保持原顺序', () => {
    // 同一个时间点用两种写法写出来（模型会这么干）。不稳定排序会让
    // 同一份数据两次渲染出不同的轴顺序——而两次都对不上，就很难查。
    const { periods } = orderPeriods(['2024Q1', '2024-01', '2024年1月'])
    expect(periods).toEqual(['2024Q1', '2024-01', '2024年1月'])
  })

  it('不改动传进来的数组', () => {
    // 原地排序的话，调用方手里那份数据会被悄悄改掉。
    const input = ['2024-03', '2024-01']
    orderPeriods(input)
    expect(input).toEqual(['2024-03', '2024-01'])
  })
})
