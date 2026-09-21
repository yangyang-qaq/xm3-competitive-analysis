/**
 * 对比矩阵的朝向。
 *
 * 这组用例的价值在于它守的是一个**看不出来**的缺陷：转置的表格与正确的
 * 表格有一样的行列、一样的 1–5 分，区别只是每个分数挂在错的东西上，
 * 而读者无从核对。
 *
 * 末了一组用的是**库里真实那份转置报告的形状**（7 维度 × 5 品牌，
 * 存成 7 行 × 5 列，报告 id `RP-b1e819bfd098`）。
 */
import { describe, expect, it } from 'vitest'

import { brandMajorScores, orientationOf } from './reportMatrix'
import type { ReportMatrix } from '../types/report'

/** 品牌优先（约定）：2 行品牌 × 3 列维度。 */
const BRAND_FIRST: ReportMatrix = {
  dimensions: ['规模', '定价', '生态'],
  brands: ['甲', '乙'],
  scores: [
    [4, 3, 2],
    [1, 5, 3],
  ],
  evidenceIds: [],
}

/** 同一个矩阵转置着给：3 行维度 × 2 列品牌。 */
const DIMENSION_FIRST: ReportMatrix = {
  dimensions: ['规模', '定价', '生态'],
  brands: ['甲', '乙'],
  scores: [
    [4, 1],
    [3, 5],
    [2, 3],
  ],
  evidenceIds: [],
}

describe('判朝向', () => {
  it('行数等于品牌数时是品牌优先', () => {
    expect(orientationOf(BRAND_FIRST)).toBe('brandMajor')
  })

  it('行列数正好反过来时是转置', () => {
    expect(orientationOf(DIMENSION_FIRST)).toBe('dimensionMajor')
  })

  it('方阵算正常', () => {
    // 2 品牌 × 2 维度时两个条件同时成立，形状上不可证伪。
    // 猜"转置了"会把一份本来正确的矩阵转坏，而转坏之后同样看不出来。
    const square: ReportMatrix = {
      dimensions: ['规模', '定价'],
      brands: ['甲', '乙'],
      scores: [
        [4, 3],
        [1, 5],
      ],
      evidenceIds: [],
    }
    expect(orientationOf(square)).toBe('brandMajor')
  })

  it('每行长度不一时说不清', () => {
    const ragged: ReportMatrix = {
      ...BRAND_FIRST,
      scores: [
        [4, 3, 2],
        [1, 5],
      ],
    }
    expect(orientationOf(ragged)).toBe('unknown')
  })

  it('宽高都对不上时说不清', () => {
    const odd: ReportMatrix = {
      dimensions: ['规模', '定价', '生态'],
      brands: ['甲', '乙'],
      scores: [
        [1, 2, 3, 4],
        [5, 6, 7, 8],
      ],
      evidenceIds: [],
    }
    expect(orientationOf(odd)).toBe('unknown')
  })

  it('缺一边、或者整个没有矩阵时说不清', () => {
    expect(orientationOf(undefined)).toBe('unknown')
    expect(orientationOf({ ...BRAND_FIRST, dimensions: [] })).toBe('unknown')
    expect(orientationOf({ ...BRAND_FIRST, brands: [] })).toBe('unknown')
    expect(orientationOf({ ...BRAND_FIRST, scores: [] })).toBe('unknown')
  })
})

describe('取出品牌优先的分数表', () => {
  it('品牌优先的原样返回', () => {
    expect(brandMajorScores(BRAND_FIRST)).toEqual([
      [4, 3, 2],
      [1, 5, 3],
    ])
  })

  it('转置的翻成品牌优先', () => {
    // 翻对的定义：结果与直接写的那份**逐值相同**。
    expect(brandMajorScores(DIMENSION_FIRST)).toEqual(brandMajorScores(BRAND_FIRST))
  })

  it('说不清时返回 null，而不是原样返回', () => {
    // **返回原样是最坏的做法**：那会画出一张转置的表，
    // 而它和正确的表分不出来。返回 null 让调用方有机会说一句话。
    const ragged: ReportMatrix = { ...BRAND_FIRST, scores: [[4, 3, 2], [1, 5]] }
    expect(brandMajorScores(ragged)).toBeNull()
    expect(brandMajorScores(undefined)).toBeNull()
  })
})

describe('库里那份真实的转置矩阵', () => {
  // RP-b1e819bfd098：7 个维度、5 个品牌，`scores` 给了 7 行 × 5 列。
  // 这是采集侧加形状校验之前跑出来的，所以它**还在库里**——
  // 报告页必须能正确处理它，而不只是处理以后新跑的那些。
  const REAL: ReportMatrix = {
    dimensions: ['d1', 'd2', 'd3', 'd4', 'd5', 'd6', 'd7'],
    brands: ['Notion', 'Obsidian', 'Coda', 'Slite', 'Nuclino'],
    scores: [
      [4, 1, 1, 1, 1],
      [3, 1, 1, 1, 1],
      [3, 1, 1, 1, 1],
      [3, 1, 1, 1, 1],
      [1, 1, 1, 1, 1],
      [2, 1, 1, 1, 1],
      [2, 1, 1, 1, 1],
    ],
    evidenceIds: [],
  }

  it('被认出来是转置的', () => {
    expect(orientationOf(REAL)).toBe('dimensionMajor')
  })

  it('翻过来之后是 5 行 × 7 列', () => {
    const scores = brandMajorScores(REAL)
    expect(scores).not.toBeNull()
    expect(scores).toHaveLength(5)
    expect(scores?.every((row) => row.length === 7)).toBe(true)
    // Notion（第 0 行）在每个维度上的分数，就是原来每一行的第 0 个数。
    expect(scores?.[0]).toEqual([4, 3, 3, 3, 1, 2, 2])
    // Obsidian（第 1 行）全是 1。
    expect(scores?.[1]).toEqual([1, 1, 1, 1, 1, 1, 1])
  })
})
