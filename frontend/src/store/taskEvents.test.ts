/**
 * 事件归约器：去重、排序、环形上限。
 *
 * 这三件事都是"错了也不会当场报错"的那种：
 *  - 去重坏了 → 断线重连后思维流出现重复，而用户分不出是重复还是真发生了两次
 *  - 排序坏了 → 进度条退回去，看起来像后端算错了
 *  - 上限坏了 → 跑到一半页面开始卡，而没人会怀疑是保留策略
 * 所以它们必须有测试，而且测试要能红。
 */
import { describe, expect, it } from 'vitest'

import type { Evidence, Thought, TraceSpan } from '../types/domain'
import type { PipelineEvent } from '../types/events'
import {
  applySnapshot,
  EMPTY_TASK_STATE,
  reduceEvents,
  SPAN_CAP,
  THOUGHT_CAP,
  type TaskViewState,
} from './taskEvents'

// ============================================================
// 构造器。**只填用得到的字段**，其余按契约给一个合法的默认值——
// 于是这个文件里的每个用例都在说"我要测的是这一条"。
// ============================================================

let seq = 0

function envelope<T extends string>(type: T) {
  seq += 1
  return { seq, taskId: 'TK-1', createdAt: '2026-01-01T00:00:00.000Z', type }
}

function thought(text: string): Thought {
  return {
    id: `TH-${text}`,
    expertId: 'L1-001',
    expertName: '某专家',
    roleTitle: '',
    level: 'L1',
    stage: 'collect',
    text,
    at: '2026-01-01T00:00:00.000Z',
  }
}

function thoughtEvent(text: string, at?: number): PipelineEvent {
  const event = { ...envelope('thought'), thought: thought(text) } as PipelineEvent
  if (at !== undefined) event.seq = at
  return event
}

function spanEvent(spanId: string, at?: number): PipelineEvent {
  const span: TraceSpan = {
    spanId,
    parentId: '',
    kind: 'llm',
    name: 'x',
    purpose: '',
    provider: 'mock',
    model: 'm',
    startedAt: '',
    endedAt: '',
    durationMs: 1,
    promptTokens: 0,
    completionTokens: 0,
    totalTokens: 0,
    costUsd: 0,
    cachedPromptTokens: 0,
    status: 'ok',
    error: '',
    detail: {},
  }
  const event = { ...envelope('trace'), span } as PipelineEvent
  if (at !== undefined) event.seq = at
  return event
}

function evidenceEvent(evidenceId: string): PipelineEvent {
  const evidence: Evidence = {
    evidenceId,
    url: `https://example.com/${evidenceId}`,
    title: evidenceId,
    snippet: '',
    fullText: '',
    brand: 'A',
    sourceType: 'web',
    siteName: '',
    publishedAt: '',
    capturedAt: '',
    matchedDimensions: [],
    query: '',
    provider: 'mock',
    rank: 1,
    credibility: 50,
    degraded: false,
    images: [],
  }
  return { ...envelope('evidence'), evidence } as PipelineEvent
}

function progressEvent(value: number, at?: number): PipelineEvent {
  const event = {
    ...envelope('progress'),
    stage: 'collect',
    progress: value,
    message: '',
    tokens: 0,
    costUsd: 0,
  } as PipelineEvent
  if (at !== undefined) event.seq = at
  return event
}

function nodeEvent(stage: string, status: string, value: number): PipelineEvent {
  return {
    ...envelope('node_update'),
    stage,
    label: '联网采集',
    status,
    progress: value,
    round: 0,
  } as unknown as PipelineEvent
}

function fresh(): TaskViewState {
  return { ...EMPTY_TASK_STATE }
}

/** 跑一批事件，返回新状态与新水位。 */
function reduce(state: TaskViewState, events: PipelineEvent[], lastSeq = 0) {
  return reduceEvents(state, events, lastSeq)
}

// ============================================================

describe('去重（断线续传的地基）', () => {
  it('水位之内的事件被丢弃，水位不动', () => {
    const events = [thoughtEvent('a', 1), thoughtEvent('b', 2), thoughtEvent('c', 3)]
    const result = reduce(fresh(), events, 2)

    expect(result.state.thoughts.map((t) => t.text)).toEqual(['c'])
    expect(result.lastSeq).toBe(3)
  })

  it('边界是「大于水位」而不是「大于等于」', () => {
    // 写成 `>=` 的话，水位那一条会被再处理一次。多一条重复思维是小事，
    // 但它说明水位语义整体偏了一格——而 `progress` 也走同一条路。
    const result = reduce(fresh(), [thoughtEvent('a', 5)], 5)
    expect(result.state.thoughts).toEqual([])
  })

  it('重复的 seq 在**同一批里**也只生效一次', () => {
    // 补发与实时推送接在一起时，同一帧可能进来两次。
    const result = reduce(fresh(), [thoughtEvent('a', 7), thoughtEvent('a', 7)], 0)
    expect(result.state.thoughts).toHaveLength(1)
    expect(result.lastSeq).toBe(7)
  })

  it('没有新事件时返回**原来那个对象**', () => {
    // 这一条守的是渲染次数：返回一个新对象会让订阅者白重渲染一次。
    // 批处理已经把它降到一帧一次了，这里再退化成"每次都新对象"
    // 就等于把省下来的又还回去。
    const state = fresh()
    const result = reduce(state, [thoughtEvent('a', 1)], 9)
    expect(result.state).toBe(state)
    expect(result.lastSeq).toBe(9)
  })
})

describe('批内排序', () => {
  it('乱序到达的事件被重排，**不会被当成过期帧丢掉**', () => {
    // 服务端正常是顺序推的，但续传的补发段与实时段接在一起时顺序不保证。
    //
    // 这条用例第一版写的是"进度不倒退"，并且它**抓不住"去掉排序"这个突变**
    // ——反证的时候才发现。原因是去掉排序之后，去重守卫（水位只比 seq 大小）
    // 把先到的 seq=3 之后那两条当过期帧直接丢了，于是 `progress` 恰好
    // 停在 0.8，断言照样通过。所以真正会坏的是**丢事件**，不是进度倒退：
    // 现象是思维流中间少几条，而水位已经推过去了，重连也补不回来。
    // 断言里因此必须有"三条都在"，光看进度是测不出这件事的。
    const result = reduce(fresh(), [thoughtEvent('c', 3), thoughtEvent('a', 1), thoughtEvent('b', 2)])

    expect(result.state.thoughts.map((t) => t.text)).toEqual(['a', 'b', 'c'])
    expect(result.lastSeq).toBe(3)
  })

  it('进度取 seq 最大的那一条，与到达顺序无关', () => {
    const result = reduce(fresh(), [progressEvent(0.8, 3), progressEvent(0.2, 1), progressEvent(0.5, 2)])

    expect(result.state.progress).toBe(0.8)
    expect(result.lastSeq).toBe(3)
  })
})

describe('环形上限', () => {
  it('思维流截到上限，且丢的是**最旧的**', () => {
    const events = Array.from({ length: THOUGHT_CAP + 5 }, (_, i) => thoughtEvent(`t${i}`))
    const result = reduce(fresh(), events)

    expect(result.state.thoughts).toHaveLength(THOUGHT_CAP)
    // 写成 `slice(0, cap)` 的话保留的是**最旧**的——工作台上会看到
    // 一场调研的开头，而最新发生的事永远不出现，且越跑越不出现。
    expect(result.state.thoughts[0]?.text).toBe('t5')
    expect(result.state.thoughts.at(-1)?.text).toBe(`t${THOUGHT_CAP + 4}`)
  })

  it('span 的上限比思维流高，且同样丢最旧的', () => {
    expect(SPAN_CAP).toBeGreaterThan(THOUGHT_CAP)
    const events = Array.from({ length: SPAN_CAP + 3 }, (_, i) => spanEvent(`SP-${i}`))
    const result = reduce(fresh(), events)

    expect(result.state.spans).toHaveLength(SPAN_CAP)
    expect(result.state.spans[0]?.spanId).toBe('SP-3')
  })

  it('没到上限时不动数组内容', () => {
    const result = reduce(fresh(), [thoughtEvent('a')])
    expect(result.state.thoughts.map((t) => t.text)).toEqual(['a'])
  })
})

describe('按 id 去重的两类事件', () => {
  it('同一个 chartId 只保留最后一份', () => {
    // 返工之后同一个图表会被重新生成。直接追加会让图集里出现两张一样的图，
    // 而且旧的那张带着过期的数据。
    const chart = (chartId: string, title: string): PipelineEvent =>
      ({ ...envelope('chart'), chart: { chartId, kind: 'bar', title, evidenceIds: [], spec: {} } }) as PipelineEvent

    const result = reduce(fresh(), [chart('C1', '旧'), chart('C2', '别的'), chart('C1', '新')])

    expect(result.state.charts.map((c) => c.chartId)).toEqual(['C2', 'C1'])
    expect(result.state.charts[1]?.title).toBe('新')
  })

  it('同一个图片 url 只留一份', () => {
    const image = (url: string): PipelineEvent =>
      ({ ...envelope('image'), image: { url, alt: '', evidenceId: 'E1', brand: 'A', sourceUrl: '', siteName: '' } }) as PipelineEvent

    const result = reduce(fresh(), [image('u1'), image('u1'), image('u2')])
    expect(result.state.images.map((i) => i.url)).toEqual(['u1', 'u2'])
  })

  it('证据不去重：同一个 url 采到两次是事实，藏起来会更坏', () => {
    // 与上面两条刻意不一致。图表和图片是**同一样东西的两次产物**
    // （重算覆盖旧的），而证据是**两次采集各自的结果**——
    // 把它们合并会让"独立信源数"这个指标算少。
    const result = reduce(fresh(), [evidenceEvent('E1'), evidenceEvent('E1')])
    expect(result.state.evidences).toHaveLength(2)
  })
})

describe('状态与阶段', () => {
  it('节点第一次更新时把任务从 pending 推进到 running', () => {
    const result = reduce(fresh(), [nodeEvent('collect', 'running', 0.4)])
    expect(result.state.status).toBe('running')
    expect(result.state.nodes.collect).toBe('running')
  })

  it('任务已经在跑时，节点更新不改变任务状态', () => {
    const state = { ...fresh(), status: 'running' as const }
    const result = reduce(state, [nodeEvent('analyze', 'running', 0.6)])
    expect(result.state.status).toBe('running')
  })

  it('done 节点让任务状态跟着变成 done', () => {
    const result = reduce({ ...fresh(), status: 'running' }, [nodeEvent('done', 'done', 1)])
    expect(result.state.status).toBe('done')
    expect(result.state.nodes.done).toBe('done')
  })

  it('degraded 是节点状态，不会被当成 done 或 error', () => {
    const result = reduce(fresh(), [nodeEvent('audit', 'degraded', 0.8)])
    expect(result.state.nodes.audit).toBe('degraded')
    expect(result.state.error).toBe('')
  })
})

describe('终态事件', () => {
  it('done 事件把进度钉到 1，并带上指标', () => {
    const event = {
      ...envelope('done'),
      reportId: 'RP-1',
      metrics: { elapsedSeconds: 12 } as never,
      degraded: ['图表：模型没吐出合法 JSON'],
      problems: [],
    } as PipelineEvent

    const result = reduce({ ...fresh(), status: 'running' }, [event])
    expect(result.state.status).toBe('done')
    expect(result.state.progress).toBe(1)
    expect(result.state.reportId).toBe('RP-1')
    expect(result.state.metrics).toEqual({ elapsedSeconds: 12 })
    expect(result.state.degraded).toEqual(['图表：模型没吐出合法 JSON'])
  })

  it('done 事件里 reportId 为空时不抹掉已有的 reportId', () => {
    // `report_ready` 先到、`done` 后到，而 `done` 在某些路径上不带 report_id。
    // 无条件赋值会让报告页的链接突然消失。
    const state = { ...fresh(), reportId: 'RP-9' }
    const event = {
      ...envelope('done'),
      reportId: '',
      metrics: {} as never,
      degraded: [],
      problems: [],
    } as PipelineEvent

    expect(reduce(state, [event]).state.reportId).toBe('RP-9')
  })

  it('error 事件把任务标为失败，并且**记进降级列表**', () => {
    // 只写 error 字段的话，失败的任务在降级面板上是空的——
    // 而"哪里没做成"恰恰是那个面板要回答的问题。
    const event = { ...envelope('error'), stage: 'analyze', message: '模型超时', kind: 'Timeout', retryable: true } as PipelineEvent
    const result = reduce({ ...fresh(), status: 'running' }, [event])

    expect(result.state.status).toBe('failed')
    expect(result.state.error).toBe('模型超时')
    expect(result.state.degraded).toHaveLength(1)
    expect(result.state.degraded[0]).toContain('模型超时')
  })

  it('report_ready 不改变任务状态', () => {
    // 报告写完了任务还没结束（后面还有落库与指标），这一条不该让它看起来完成了。
    const event = {
      ...envelope('report_ready'),
      reportId: 'RP-1',
      query: 'q',
      subject: 's',
      sectionCount: 3,
      evidenceCount: 5,
      problems: [],
      degraded: [],
    } as PipelineEvent

    const result = reduce({ ...fresh(), status: 'running' }, [event])
    expect(result.state.status).toBe('running')
    expect(result.state.reportId).toBe('RP-1')
  })
})

describe('快照与事件的合流', () => {
  it('兜底快照的空 nodes 不抹掉从事件流重建出来的节点', () => {
    // 服务重启后打开页面就是这条路：快照来自 DB 兜底分支（`nodes` 是 `{}`），
    // 节点状态只能靠回放的 `node_update` 事件重建。
    // 这里直接覆盖的话，DAG 会在恢复完成的下一秒变回一片灰。
    const state = reduce(fresh(), [nodeEvent('collect', 'done', 0.5)]).state
    expect(Object.keys(state.nodes)).toHaveLength(1)

    const merged = applySnapshot(state, { nodes: {} })
    expect(merged.nodes.collect).toBe('done')
  })

  it('内存快照有 nodes 时以它为准', () => {
    // 反过来：有 runner 的时候，快照是权威的。以事件为准会让"刚连上、
    // 历史还没回放完"的那一瞬显示成旧状态。
    const state = reduce(fresh(), [nodeEvent('collect', 'running', 0.3)]).state
    const merged = applySnapshot(state, { nodes: { collect: 'done', analyze: 'running' } })

    expect(merged.nodes.collect).toBe('done')
    expect(merged.nodes.analyze).toBe('running')
  })

  it('快照里空字符串的 reportId / error 不覆盖已有的值', () => {
    const state: TaskViewState = { ...fresh(), reportId: 'RP-1', error: '上次的错' }
    const merged = applySnapshot(state, { reportId: '', error: '' })
    expect(merged.reportId).toBe('RP-1')
    expect(merged.error).toBe('上次的错')
  })

  it('awaitingClarify 与 needClarify 各自独立', () => {
    // 两者的取值组合里最要紧的一种：问过（need=true）但现在不在等（awaiting=false）。
    // 合并成一个字段的话，一个上周跑完的任务每次打开都会被拉回澄清页。
    const merged = applySnapshot(fresh(), { needClarify: true, awaitingClarify: false })
    expect(merged.needClarify).toBe(true)
    expect(merged.awaitingClarify).toBe(false)
  })
})
