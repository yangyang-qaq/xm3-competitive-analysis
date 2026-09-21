/**
 * 知识图谱数据层的测试。
 *
 * 这一份盯的是**两种不会报错的错**：
 *
 * 1. **数字与图形对不上**。点的大小和边的粗细如果来自两套算法，
 *    它们会各说各话，而页面上都"看着合理"。所以不变量 1 要当场断言。
 * 2. **图看起来是全集**。只有能被归类的证据才进得了图，剩下的必须被数出来
 *    并显示出来。少了几十条证据而图上看不出来，是最坏的一种——
 *    读者会用这张图去下结论。
 */
import { describe, expect, it } from 'vitest'

import {
  GRAPH_HEIGHT,
  GRAPH_WIDTH,
  buildGraph,
  layoutGraph,
  multiDimensionHits,
  pickEvidences,
} from './graph'
import type { ReportGraph } from './graph'
import type { ReportBody, ReportEvidence } from '../types/report'

function evidence(overrides: Partial<ReportEvidence> = {}): ReportEvidence {
  return {
    evidenceId: 'EV-1',
    url: 'https://example.com/a',
    title: '标题',
    snippet: '摘要',
    fullText: '',
    brand: '甲',
    sourceType: 'web',
    siteName: 'example.com',
    publishedAt: '',
    capturedAt: '2026-09-19T10:00:00.000+00:00',
    matchedDimensions: ['定价'],
    query: 'q',
    provider: 'mock',
    rank: 1,
    credibility: 60,
    degraded: false,
    images: [],
    ...overrides,
  }
}

function body(overrides: Partial<ReportBody> = {}): ReportBody {
  return {
    brands: ['甲', '乙'],
    dimensions: ['定价', '功能', '舆情'],
    evidences: [
      evidence({ evidenceId: 'EV-1', brand: '甲' }),
      evidence({ evidenceId: 'EV-2', brand: '甲' }),
      evidence({ evidenceId: 'EV-3', brand: '乙', matchedDimensions: ['功能'] }),
    ],
    ...overrides,
  }
}

function node(graph: ReportGraph, id: string) {
  const found = graph.nodes.find((item) => item.id === id)
  if (!found) throw new Error(`没有这个节点：${id}`)
  return found
}

describe('构图', () => {
  it('维度节点的 weight 等于它所有边的权重之和', () => {
    // 不变量 1。维度这一侧是**严格相等**：一条证据命中同一个维度两次不会发生。
    const graph = buildGraph(body())
    for (const item of graph.nodes.filter((n) => n.kind === 'dimension')) {
      const sum = graph.links
        .filter((l) => l.kind === 'brand-dimension' && (l.source === item.id || l.target === item.id))
        .reduce((total, l) => total + l.weight, 0)
      expect(sum, `${item.id} 的点大小与线粗细对不上`).toBe(item.weight)
    }
  })

  it('品牌节点的 weight 是证据条数，差额是"多记的笔数"（不是"多维度证据的条数"）', () => {
    // 不变量 2。**这条以前写成"品牌也等于边权之和"，测试还是绿的——碰巧。**
    // 夹具里每条证据只命中一个维度，两个定义给出同一个数；换成真数据
    // （一个品牌 45 条证据）当场分开。
    //
    // 差额的两种说法在这里**必须**分开，否则又是一个"同一个问题两个答案"：
    //   - 多维度证据的**条数**：F、G 各算 1 → 2
    //   - 多记的**笔数**：F 多记 1（2 维），G 多记 2（3 维）→ 3
    // 真报告上这两种数法差过 1（Notion 那份 3 条 vs 4 笔）。函数返回的是后者。
    const graph = buildGraph(
      body({
        brands: ['甲'],
        dimensions: ['定价', '功能', '舆情'],
        evidences: [
          evidence({ evidenceId: 'A', brand: '甲', matchedDimensions: ['定价'] }),
          evidence({ evidenceId: 'B', brand: '甲', matchedDimensions: ['定价'] }),
          evidence({ evidenceId: 'C', brand: '甲', matchedDimensions: ['功能'] }),
          evidence({ evidenceId: 'D', brand: '甲', matchedDimensions: ['舆情'] }),
          evidence({ evidenceId: 'E', brand: '甲', matchedDimensions: ['定价'] }),
          // 命中 2 个维度：多记 1 笔
          evidence({ evidenceId: 'F', brand: '甲', matchedDimensions: ['定价', '功能'] }),
          // 命中 3 个维度：多记 2 笔
          evidence({ evidenceId: 'G', brand: '甲', matchedDimensions: ['定价', '功能', '舆情'] }),
        ],
      }),
    )

    const brand = node(graph, 'brand:甲')
    expect(brand.weight).toBe(7) // 7 条证据，点开就是 7 行
    expect(multiDimensionHits(graph, brand.id)).toBe(3) // 1 + 2 笔，不是 2 条
    // 差额不能凭空出现在维度那一侧，也不能凭空出现在对象上。
    expect(multiDimensionHits(graph, 'dimension:定价')).toBe(0)
    expect(multiDimensionHits(graph, 'dimension:功能')).toBe(0)
    expect(multiDimensionHits(graph, 'dimension:舆情')).toBe(0)
    expect(multiDimensionHits(graph, 'subject')).toBe(0)
  })

  it('边上的条数就是那个品牌×维度下的证据条数', () => {
    const graph = buildGraph(
      body({
        evidences: [
          evidence({ evidenceId: 'A', brand: '甲', matchedDimensions: ['定价'] }),
          evidence({ evidenceId: 'B', brand: '甲', matchedDimensions: ['定价'] }),
          evidence({ evidenceId: 'C', brand: '甲', matchedDimensions: ['功能'] }),
        ],
      }),
    )

    const pricing = graph.links.find((l) => l.source === 'brand:甲' && l.target === 'dimension:定价')
    const feature = graph.links.find((l) => l.source === 'brand:甲' && l.target === 'dimension:功能')

    expect(pricing?.weight).toBe(2)
    expect(feature?.weight).toBe(1)
  })

  it('一条证据命中多个维度时，每个维度都记一笔', () => {
    // 一条证据同时说明两件事，是常态而不是例外。只记第一个维度会让
    // 另外那些维度看起来"没采到"——那正是覆盖率这个指标要回答的问题。
    const graph = buildGraph(
      body({
        evidences: [evidence({ brand: '甲', matchedDimensions: ['定价', '功能'] })],
      }),
    )

    expect(node(graph, 'dimension:定价').weight).toBe(1)
    expect(node(graph, 'dimension:功能').weight).toBe(1)
  })

  it('进不了的证据按原因分开数，两种原因互斥且加起来是总数', () => {
    // 落不到任何一条边上：边是"品牌×维度"，缺哪一边都成不了边。
    // **数出来是必须的**——不数的话，图看起来就是全部证据的画像。
    // **分开数也是必须的**——"品牌名单不全"和"证据没归到维度上"
    // 要读者做的事完全不同。
    const graph = buildGraph(
      body({
        evidences: [
          evidence({ evidenceId: 'A', brand: '甲' }), // 进图
          evidence({ evidenceId: 'B', brand: '丙' }), // 品牌不在名单
          evidence({ evidenceId: 'C', brand: '甲', matchedDimensions: [] }), // 没标注维度
          evidence({ evidenceId: 'D', brand: '甲', matchedDimensions: ['出海'] }), // 维度不在名单
          // 品牌和维度**同时**不合格：只能算一类，否则总数会超过证据总数。
          evidence({ evidenceId: 'E', brand: '丙', matchedDimensions: ['出海'] }),
        ],
      }),
    )

    expect(node(graph, 'brand:甲').weight).toBe(1)
    expect(graph.droppedNoBrand).toBe(2) // B、E
    expect(graph.droppedNoDimension).toBe(2) // C、D
    expect(graph.droppedUnknownDimension).toBe(1) // D（有标注但名字对不上）
    expect(graph.droppedNoBrand + graph.droppedNoDimension).toBe(4) // == 5 条减去进图的 1 条
  })

  it('品牌名与维度名同名时是两个节点', () => {
    // id 带种类前缀就是为了这个。用标签当 id 的话，两个节点会缩成一个，
    // 边也会自己接上自己——图上看起来只是"少了一个点"。
    const graph = buildGraph(
      body({ brands: ['定价'], dimensions: ['定价'], evidences: [] }),
    )

    expect(graph.nodes.map((n) => n.id)).toEqual([
      'subject',
      'brand:定价',
      'dimension:定价',
    ])
  })

  it('一条证据都没有的维度仍然是个节点，并标为 uncovered', () => {
    // 铁律 1：没有证据本身是个结论。把它从图上抹掉，
    // 读者会以为"这个维度调研过、结论是没什么可说的"。
    const graph = buildGraph(body())

    expect(node(graph, 'dimension:舆情').uncovered).toBe(true)
    expect(node(graph, 'dimension:定价').uncovered).toBe(false)
  })

  it('专家名从思维流里取，没有说过话的退回 id', () => {
    // 以 `team` 为准：它记的是"派了谁"，`thoughts` 只记"谁说过话"。
    // 一句话没说的专家也参与了，不能因为他安静就从图上消失。
    const graph = buildGraph(
      body({
        team: { lead: ['L3-001'], strategists: ['L2-003', 'L2-004'], executors: [] },
        thoughts: [
          {
            id: 'TH-1',
            expertId: 'L2-003',
            expertName: '林晓',
            roleTitle: '定价策略师',
            level: 'L2',
            stage: 'analyze',
            text: '…',
            at: '2026-09-19T10:00:00.000+00:00',
          },
        ],
      }),
    )

    expect(node(graph, 'expert:L2-003').label).toBe('林晓')
    expect(node(graph, 'expert:L2-003').level).toBe('L2')
    // 没说过话的那位：退回 id，而不是从图上消失。
    expect(node(graph, 'expert:L2-004').label).toBe('L2-004')
    expect(node(graph, 'expert:L3-001').id).toBe('expert:L3-001')
  })

  it('同一份正文两次构图完全一致（含节点与边的顺序）', () => {
    // 顺序不稳定的话，"每次打开图长得不一样"会让人以为数据在变，
    // 而布局的确定性也建立在输入顺序稳定之上。
    const input = body()

    expect(JSON.stringify(buildGraph(input))).toBe(JSON.stringify(buildGraph(input)))
  })

  it('空正文不崩：只剩一个调研对象节点', () => {
    const graph = buildGraph({})

    expect(graph.nodes.map((n) => n.kind)).toEqual(['subject'])
    expect(graph.links).toEqual([])
    expect(graph.droppedNoBrand).toBe(0)
    expect(graph.droppedNoDimension).toBe(0)
  })
})

describe('布局', () => {
  it('两次布局的坐标完全相同', () => {
    // 这条是**关于 d3-force 的一个断言**，不是关于我们代码的：
    // 节点初始位置由下标算出来（不用随机数），唯一的随机源 `jiggle`
    // 取的是 d3 固定种子的 LCG。哪天升级 d3 换掉了那个种子源，
    // 或者这里有人图省事把初始位置改回 `Math.random()`，这条会红。
    const graph = buildGraph(body())

    const first = layoutGraph(graph)
    const second = layoutGraph(graph)

    expect(first.nodes.map((n) => [n.id, n.x, n.y])).toEqual(second.nodes.map((n) => [n.id, n.x, n.y]))
  })

  it('布局不改动传进来的图', () => {
    // `forceLink` 会**就地**把 `source`/`target` 从 id 换成节点对象。
    // 传的是同一个数组的话，调用方手里的 `GraphLink` 就变成了
    // "类型上写着 string、运行时是对象"，而错误会在很远的画线代码里出现。
    const graph = buildGraph(body())
    const before = JSON.stringify(graph.links)

    layoutGraph(graph)

    expect(JSON.stringify(graph.links)).toBe(before)
  })

  it('返回的边端点仍然是 id 字符串，可以直接画', () => {
    const layout = layoutGraph(buildGraph(body()))
    const drawn = new Set(layout.nodes.map((n) => n.id))

    for (const link of layout.links) {
      expect(typeof link.source).toBe('string')
      expect(drawn.has(link.source)).toBe(true)
      expect(drawn.has(link.target)).toBe(true)
    }
  })

  it('默认画布尺寸与 svg 用的是同一份常量', () => {
    // 两处各写一份的话，图会画到画布外面去，而那是"看起来有点怪"。
    const layout = layoutGraph(buildGraph(body()))

    expect(layout.width).toBe(GRAPH_WIDTH)
    expect(layout.height).toBe(GRAPH_HEIGHT)
    for (const item of layout.nodes) {
      expect(Number.isFinite(item.x)).toBe(true)
      expect(Number.isFinite(item.y)).toBe(true)
    }
  })
})

describe('取证据', () => {
  // 名单与维度是**必填**的，不是筛选条件——它们是"进图"这条判据的输入。
  const names = { brands: ['甲', '乙'], dimensions: ['定价', '功能'] }

  it('图上说有几条，这里就列出几条', () => {
    // **这条是真数据上抓到的 bug 的守卫。** 之前 `pickEvidences` 只按传进来的
    // 字段过滤，不管名单与计划维度，于是品牌的"证据条数"有两个答案：
    // 图上 45（只算命中了计划维度的），点开 72（那个品牌下的全部）。
    // 两个数各自都能自圆其说，合起来就是"别再信这张图"。
    const rows = [
      evidence({ evidenceId: 'A', brand: '甲', matchedDimensions: ['定价'] }), // 进图
      evidence({ evidenceId: 'B', brand: '甲', matchedDimensions: [] }), // 没命中维度
      evidence({ evidenceId: 'C', brand: '甲', matchedDimensions: ['出海'] }), // 维度不在计划里
      evidence({ evidenceId: 'D', brand: '丙', matchedDimensions: ['定价'] }), // 品牌不在名单
    ]
    const graph = buildGraph(body({ evidences: rows }))

    for (const item of graph.nodes.filter((n) => n.kind === 'brand' || n.kind === 'dimension')) {
      const picked =
        item.kind === 'brand'
          ? pickEvidences(rows, { ...names, brand: item.label })
          : pickEvidences(rows, { ...names, dimension: item.label })
      expect(picked.length, `${item.id} 的条数与列表行数对不上`).toBe(item.weight)
    }
    expect(pickEvidences(rows, { ...names, brand: '甲' })).toHaveLength(1)
  })

  it('品牌与维度两个条件是「与」', () => {
    const rows = [
      evidence({ evidenceId: 'A', brand: '甲', matchedDimensions: ['定价'] }),
      evidence({ evidenceId: 'B', brand: '乙', matchedDimensions: ['定价'] }),
      evidence({ evidenceId: 'C', brand: '甲', matchedDimensions: ['功能'] }),
    ]

    const ids = (filter: Parameters<typeof pickEvidences>[1]) =>
      pickEvidences(rows, filter).map((e) => e.evidenceId)

    expect(ids({ ...names, brand: '甲', dimension: '定价' })).toEqual(['A'])
    expect(ids({ ...names, brand: '甲' })).toEqual(['A', 'C'])
    expect(ids({ ...names, dimension: '定价' })).toEqual(['A', 'B'])
    expect(ids({ ...names })).toEqual(['A', 'B', 'C'])
  })

  it('按可信度降序，并列时保持原顺序', () => {
    const rows = [
      evidence({ evidenceId: 'A', credibility: 40 }),
      evidence({ evidenceId: 'B', credibility: 90 }),
      evidence({ evidenceId: 'C', credibility: 70 }),
      evidence({ evidenceId: 'D', credibility: 70 }),
    ]

    expect(pickEvidences(rows, { ...names }).map((e) => e.evidenceId)).toEqual(['B', 'C', 'D', 'A'])
  })
})
