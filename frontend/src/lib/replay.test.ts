/**
 * 决策回放的纯函数。
 *
 * 这个文件里最重要的两组断言，各自对应一个"不报错、只是不对"的失效模式：
 *
 * 1. **顺序**。回放的全部意义是"第几步"。而接口给的树是深度优先的，
 *    父 span 又一定晚于子 span 完成——直接用遍历顺序当步骤号，
 *    页面上看起来完全正常，只是"拖到第 5 步"和真实发生顺序对不上。
 *    所以有一条**三层嵌套**的用例，专门把"按开始时间排"与
 *    "按遍历顺序"分开。
 *
 * 2. **累计 vs 当步**。同一台主机可能被好几条 `fetch` span 先后碰到
 *    （同一个站的不同页面）。所以"到这一步为止手里有哪些站"与
 *    "这一步新碰到哪个站"是两个数，页面上分别用来说
 *    "已覆盖 12 个站点"和"刚刚拿到 zhihu.com"。合并成一个是错的，
 *    而错的后果只是那个高亮不再出现——比崩溃难发现得多。
 */
import { describe, expect, it } from 'vitest'

import {
  buildTimeline,
  evidenceHost,
  firstFetchIndex,
  flattenSpans,
  groupEvidencesByHost,
  parseTime,
  replayFrame,
  spanHosts,
  spanSequence,
} from './replay'
import type { SpanNode, TraceSpan } from '../types/domain'

function span(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return {
    spanId: 'SP-00001',
    parentId: '',
    kind: 'fetch',
    name: 'https://example.com/a',
    purpose: '',
    provider: 'mock',
    model: '',
    startedAt: '2026-09-18T14:47:26.000+00:00',
    endedAt: '2026-09-18T14:47:27.000+00:00',
    durationMs: 1000,
    promptTokens: 0,
    completionTokens: 0,
    totalTokens: 0,
    costUsd: 0,
    cachedPromptTokens: 0,
    status: 'ok',
    error: '',
    detail: {},
    ...overrides,
  }
}

function node(overrides: Partial<TraceSpan> = {}, children: SpanNode[] = []): SpanNode {
  return { ...span(overrides), children }
}

function fetchSpan(
  spanId: string,
  domain: string,
  startedAt: string,
  durationMs = 1000,
  endedAt = '2026-09-18T14:00:01.000+00:00',
): SpanNode {
  return node({ spanId, kind: 'fetch', startedAt, endedAt, durationMs, detail: { domain } })
}

describe('spanSequence', () => {
  it('取出编号', () => {
    expect(spanSequence('SP-00007')).toBe(7)
    expect(spanSequence('SP-12345')).toBe(12345)
  })

  it('六位数不会排到五位数前面去', () => {
    // 零填充是 5 位，到 10 万条就变 6 位。按字符串排会得到
    // 'SP-100000' < 'SP-09999'（'1' < '9'），顺序从此错位。
    expect(spanSequence('SP-100000')).toBeGreaterThan(spanSequence('SP-09999'))
  })

  it('取不出数字的排在最前，与后端一致', () => {
    expect(spanSequence('SP-abc')).toBe(0)
    expect(spanSequence('SP-')).toBe(0)
  })
})

describe('parseTime', () => {
  it('正常的 ISO 串', () => {
    expect(parseTime('2026-09-18T14:47:26.000+00:00')).toBe(Date.parse('2026-09-18T14:47:26.000Z'))
  })

  it('空串与坏值都给 null，而不是 NaN', () => {
    // `NaN` 会让后面每一次比较都是 false，于是排序静默失效、
    // 减法得到 NaN 而页面上显示成 "NaN 秒"。
    expect(parseTime('')).toBeNull()
    expect(parseTime('不是时间')).toBeNull()
  })
})

describe('flattenSpans', () => {
  it('三层嵌套按 spanId 编号排，而不是按遍历顺序', () => {
    // 深度优先遍历给出 [根, 子, 孙]；而库里的顺序是**完成**顺序，
    // 父一定晚于子结束。这里把编号与时间**故意错开**，用来分辨
    // 排序键到底是编号还是时间戳——两者在真数据上目前总是一致，
    // 所以只有构造出来的数据能把它们分开（见问题 35）。
    const tree = [
      node(
        { spanId: 'SP-00001', startedAt: '2026-09-18T14:00:00.000+00:00', endedAt: '2026-09-18T14:00:30.000+00:00' },
        [
          node(
            { spanId: 'SP-00002', startedAt: '2026-09-18T14:00:05.000+00:00', endedAt: '2026-09-18T14:00:10.000+00:00' },
            [node({ spanId: 'SP-00003', startedAt: '2026-09-18T14:00:06.000+00:00', endedAt: '2026-09-18T14:00:07.000+00:00' })],
          ),
        ],
      ),
      // 平行的一条，**编号最大但开始得最早**：只有编号排序能把它放对位置
      node({ spanId: 'SP-00004', startedAt: '2026-09-18T14:00:03.000+00:00' }),
    ]

    const flat = flattenSpans(tree)
    expect(flat.map((item) => item.span.spanId)).toEqual([
      'SP-00001',
      'SP-00002',
      'SP-00003',
      'SP-00004',
    ])
    // 祖先链跟着走：孙的祖先是 [根, 子]
    const grandchild = flat.find((item) => item.span.spanId === 'SP-00003')!
    expect(grandchild.ancestors.map((item) => item.spanId)).toEqual(['SP-00001', 'SP-00002'])
    // 根的祖先链是空的
    expect(flat[0]!.ancestors).toEqual([])
  })

  it('编号与开始时间矛盾时，**以编号为准**（与后端 spans() 同一条规矩）', () => {
    // 这正是并发下真会发生的形状：编号 41 的 startedAt 晚于编号 42。
    const flat = flattenSpans([
      node({ spanId: 'SP-00042', startedAt: '2026-09-18T14:00:00.000+00:00' }),
      node({ spanId: 'SP-00041', startedAt: '2026-09-18T14:00:01.000+00:00' }),
    ])
    expect(flat.map((item) => item.span.spanId)).toEqual(['SP-00041', 'SP-00042'])
  })

  it('编号取不出数字时（游离 span）才退回开始时间', () => {
    const flat = flattenSpans([
      node({ spanId: 'SP-zzz', startedAt: '2026-09-18T14:00:02.000+00:00' }),
      node({ spanId: 'SP-aaa', startedAt: '2026-09-18T14:00:01.000+00:00' }),
      // 编号能取出来的一律排在编号取不出来的后面
      node({ spanId: 'SP-00001', startedAt: '2026-09-18T14:00:09.000+00:00' }),
    ])
    expect(flat.map((item) => item.span.spanId)).toEqual(['SP-aaa', 'SP-zzz', 'SP-00001'])
  })
})

describe('buildTimeline', () => {
  it('步号是 0 起连续的一段', () => {
    const timeline = buildTimeline([
      fetchSpan('SP-00001', 'a.com', '2026-09-18T14:00:00.000+00:00'),
      fetchSpan('SP-00002', 'b.com', '2026-09-18T14:00:01.000+00:00'),
      fetchSpan('SP-00003', 'c.com', '2026-09-18T14:00:02.000+00:00'),
    ])
    expect(timeline.steps.map((step) => step.index)).toEqual([0, 1, 2])
  })

  it('时间戳坏掉的行**不丢**，且停在它编号该在的位置', () => {
    // 丢一条会让步数和 summary.spanCount 对不上，而页面上看不出来。
    // 早期版本还会把它挪到最前面（那时按 at 排序）——那等于让一条
    // 数据不全的 span 篡改别人的步骤号。
    const timeline = buildTimeline([
      fetchSpan('SP-00001', 'a.com', '2026-09-18T14:00:00.000+00:00'),
      node({ spanId: 'SP-00002', startedAt: '', endedAt: '' }),
      fetchSpan('SP-00003', 'c.com', '2026-09-18T14:00:02.000+00:00'),
    ])
    expect(timeline.steps.map((step) => step.span.spanId)).toEqual([
      'SP-00001',
      'SP-00002',
      'SP-00003',
    ])
  })

  it('步骤顺序就是编号顺序 —— 不因为时间戳捣乱而重排', () => {
    // 一版曾经按 `at` 重排，理由是"滑杆往右拖 at 不能倒退"。
    // 那样会制造出第三种顺序，并且让 cumulativeMs 不再是前缀和。
    // 真正的保证在 `replayFrame`：hosts 按步骤下标累加，与 at 无关。
    const timeline = buildTimeline([
      fetchSpan('SP-00001', 'a.com', '2026-09-18T14:00:00.000+00:00', 1000, '2026-09-18T14:00:30.000+00:00'),
      fetchSpan('SP-00002', 'b.com', '2026-09-18T14:00:01.000+00:00', 1000, '2026-09-18T14:00:02.000+00:00'),
    ])
    expect(timeline.steps.map((step) => step.span.spanId)).toEqual(['SP-00001', 'SP-00002'])
    // 第一条**结束得更晚**，所以 at 是递减的——这是允许的，不是缺陷
    expect(timeline.steps[0]!.at).toBeGreaterThan(timeline.steps[1]!.at)
  })

  it('空输入不炸', () => {
    const timeline = buildTimeline([])
    expect(timeline.steps).toEqual([])
    expect(timeline.totalMs).toBe(0)
    expect(timeline.startMs).toBe(0)
  })

  it('累计耗时是前缀和（按步骤顺序，不按时间）', () => {
    const timeline = buildTimeline([
      fetchSpan('SP-00001', 'a.com', '2026-09-18T14:00:00.000+00:00', 1000, '2026-09-18T14:00:30.000+00:00'),
      fetchSpan('SP-00002', 'b.com', '2026-09-18T14:00:01.000+00:00', 2500, '2026-09-18T14:00:02.000+00:00'),
    ])
    // 结束时刻是倒着的，前缀和仍然按步骤走：1000, 3500
    expect(timeline.steps.map((step) => step.cumulativeMs)).toEqual([1000, 3500])
  })

  it('totalMs 是跨度，取最晚的结束时刻减起点', () => {
    const timeline = buildTimeline([
      fetchSpan('SP-00001', 'a.com', '2026-09-18T14:00:00.000+00:00', 1000, '2026-09-18T14:00:30.000+00:00'),
      fetchSpan('SP-00002', 'b.com', '2026-09-18T14:00:01.000+00:00', 1000, '2026-09-18T14:00:02.000+00:00'),
    ])
    expect(timeline.totalMs).toBe(30_000)
  })
})

describe('evidenceHost', () => {
  it('小写、去掉 www.', () => {
    expect(evidenceHost('https://WWW.Zhihu.com/question/1')).toBe('zhihu.com')
  })

  it('解析不了的脏数据也能取出一段，取不到给空串', () => {
    // 空串在配对里会自然落空，而不是错配到别人身上。
    expect(evidenceHost('notion.so/pricing')).toBe('notion.so')
    expect(evidenceHost('')).toBe('')
  })

  it('端口与认证段不进主机名', () => {
    expect(evidenceHost('http://user:pass@Example.com:8080/x')).toBe('example.com')
  })
})

describe('spanHosts', () => {
  it('fetch span 的 detail.domain 就是主机', () => {
    expect(spanHosts(span({ kind: 'fetch', detail: { domain: 'zhihu.com' } }))).toEqual(['zhihu.com'])
  })

  it('search span 的 sites 不算数', () => {
    // `sites` 是**请求时指定的站点过滤**，不是"抓到了哪些站"。
    // 算进去会让面板在采集刚开始时就显示一堆还没抓的证据。
    expect(spanHosts(span({ kind: 'search', detail: { sites: ['zhihu.com'], hits: 6 } }))).toEqual([])
  })

  it('没有 domain 就没有主机', () => {
    expect(spanHosts(span({ kind: 'llm', detail: { tier: 'fast' } }))).toEqual([])
    expect(spanHosts(span({ kind: 'fetch', detail: {} }))).toEqual([])
  })
})

describe('replayFrame', () => {
  const tree = [
    node({ spanId: 'SP-00001', kind: 'search', startedAt: '2026-09-18T14:00:00.000+00:00' }),
    fetchSpan('SP-00002', 'zhihu.com', '2026-09-18T14:00:01.000+00:00'),
    // **同一台主机的第二条** —— 累计与当步在这里分开
    fetchSpan('SP-00003', 'zhihu.com', '2026-09-18T14:00:02.000+00:00'),
    fetchSpan('SP-00004', 'bilibili.com', '2026-09-18T14:00:03.000+00:00'),
  ]
  const timeline = buildTimeline(tree)
  const byHost = groupEvidencesByHost([
    { url: 'https://www.zhihu.com/a', credibility: 60 },
    { url: 'https://zhihu.com/b', credibility: 90 },
    { url: 'https://bilibili.com/c', credibility: 70 },
  ])

  it('第一条 fetch 之前没有主机', () => {
    // 实测真任务 TK-1d1ff1f7090b 里前 117 步都属于这一段
    // （需求解析、调度、最早的搜索），滑杆的前 47%。
    expect(replayFrame(timeline, 0, byHost).hosts).toEqual([])
    expect(replayFrame(timeline, 0, byHost).reached).toBe(0)
  })

  it('主机是累计的，不是当步的', () => {
    const second = replayFrame(timeline, 2, byHost)
    expect(second.hosts).toEqual(['zhihu.com'])
    expect(second.reached).toBe(2)
    // 第 3 步仍是 zhihu.com，所以**没有新主机**——
    // 高亮靠这个数，用了当步集合的话 zhihu.com 会一直闪
    expect(second.freshHosts).toEqual([])
  })

  it('新主机只报一次', () => {
    expect(replayFrame(timeline, 1, byHost).freshHosts).toEqual(['zhihu.com'])
    expect(replayFrame(timeline, 3, byHost).freshHosts).toEqual(['bilibili.com'])
  })

  it('到末尾时主机齐了', () => {
    const last = replayFrame(timeline, timeline.steps.length - 1, byHost)
    expect(last.hosts).toEqual(['zhihu.com', 'bilibili.com'])
    expect(last.reached).toBe(3)
  })

  it('游标越界钳到两端，而不是给空画面', () => {
    // `<input type="range">` 在时间线变化（换任务）时会短暂把值留在旧范围外。
    expect(replayFrame(timeline, -5, byHost).step?.index).toBe(0)
    expect(replayFrame(timeline, 9999, byHost).step?.index).toBe(timeline.steps.length - 1)
  })

  it('小数游标取整', () => {
    expect(replayFrame(timeline, 2.7, byHost).step?.index).toBe(2)
  })

  it('空时间线给 null，不抛', () => {
    const frame = replayFrame(buildTimeline([]), 0, byHost)
    expect(frame.step).toBeNull()
    expect(frame.hosts).toEqual([])
    expect(frame.reached).toBe(0)
  })
})

describe('groupEvidencesByHost', () => {
  it('按主机分组，组内按可信度降序', () => {
    const grouped = groupEvidencesByHost([
      { url: 'https://zhihu.com/low', credibility: 20 },
      { url: 'https://zhihu.com/high', credibility: 90 },
      { url: 'https://zhihu.com/mid', credibility: 50 },
    ])
    expect(grouped.get('zhihu.com')?.map((item) => item.credibility)).toEqual([90, 50, 20])
  })

  it('可信度并列时保持原顺序 —— 拖滑杆时列表不能自己抖', () => {
    const grouped = groupEvidencesByHost([
      { url: 'https://a.com/1', credibility: 50 },
      { url: 'https://a.com/2', credibility: 50 },
      { url: 'https://a.com/3', credibility: 50 },
    ])
    expect(grouped.get('a.com')?.map((item) => item.url)).toEqual([
      'https://a.com/1',
      'https://a.com/2',
      'https://a.com/3',
    ])
  })

  it('取不出主机名的证据不落进任何组', () => {
    const grouped = groupEvidencesByHost([{ url: '', credibility: 50 }])
    expect(grouped.size).toBe(0)
  })
})

describe('firstFetchIndex', () => {
  it('指出采集从哪一步开始', () => {
    const timeline = buildTimeline([
      node({ spanId: 'SP-00001', kind: 'search', startedAt: '2026-09-18T14:00:00.000+00:00' }),
      node({ spanId: 'SP-00002', kind: 'llm', startedAt: '2026-09-18T14:00:00.500+00:00' }),
      fetchSpan('SP-00003', 'a.com', '2026-09-18T14:00:01.000+00:00'),
    ])
    expect(firstFetchIndex(timeline)).toBe(2)
  })

  it('一次采集都没有时给 null —— 与"还没轮到采集"是两件事', () => {
    const timeline = buildTimeline([
      node({ spanId: 'SP-00001', kind: 'llm', startedAt: '2026-09-18T14:00:00.000+00:00' }),
    ])
    expect(firstFetchIndex(timeline)).toBeNull()
  })

  it('空时间线给 null', () => {
    expect(firstFetchIndex(buildTimeline([]))).toBeNull()
  })
})
