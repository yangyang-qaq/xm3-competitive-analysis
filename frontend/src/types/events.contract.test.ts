/**
 * SSE 事件契约：这一侧的那条测试。
 *
 * 后端有一条对应的（`backend/tests/contract/test_sse_contract.py`），
 * 它跑一遍真实的 Mock 流水线，断言推出去的字节与 `contracts/sse_events.json`
 * 逐键一致。这一条守的是另外半边：**TypeScript 的接口与那份 JSON**。
 *
 * 两条合起来才闭环。只有后端那一条的话，后端改了字段名、前端跟着改了
 * 接口、但两边都改错成第三个名字——JSON 会被同步改掉，两条测试全绿，
 * 而 `contracts/sse_events.json` 本身没有第三方在看着它。
 *
 * 怎么在没有运行时类型的情况下做到这件事
 * ------------------------------------
 * TypeScript 的 interface 在运行时不存在，没法反射。所以下面写了一份
 * `SAMPLES`——每种事件一个字面量对象，用 `satisfies` 标注：
 *
 *   - 少一个**必需**字段 → 编译不过
 *   - 字段名写错 / 多一个字段 → 编译不过（字面量的多余属性检查）
 *   - 类型不对（比如 `progress` 写成百分数） → 编译不过
 *
 * 然后运行时把 `SAMPLES` 的键集合与 JSON 的键集合比一遍。
 * 于是"接口"↔"SAMPLES"靠编译器、"SAMPLES"↔"JSON"靠这个测试、
 * "JSON"↔"后端真实事件"靠后端那条测试。三方任意两个不同步都会红。
 *
 * 手写而不是从 JSON 生成：生成的样例永远与 JSON 一致，也就永远发现不了
 * 接口与 JSON 不一致——那正是要防的事。
 */
import { describe, expect, it } from 'vitest'

import contract from '../../../contracts/sse_events.json'
import type {
  ChartEvent,
  DoneEvent,
  ErrorEvent,
  EvidenceEvent,
  ImageEvent,
  MessageEvent,
  NodeUpdateEvent,
  PipelineEvent,
  PipelineEventType,
  ProgressEvent,
  ReportReadyEvent,
  ThoughtEvent,
  TraceEvent,
} from './events'
import { EVENT_TYPES, isPipelineEvent } from './events'

/** 信封的四个键。样例里每个事件都要带全。 */
const ENVELOPE = {
  seq: 1,
  taskId: 'TK-000000000000',
  createdAt: '2026-01-01T00:00:00.000+00:00',
} as const

const SAMPLES: { [K in PipelineEventType]: Extract<PipelineEvent, { type: K }> } = {
  node_update: {
    ...ENVELOPE,
    type: 'node_update',
    stage: 'collect',
    label: '证据采集',
    status: 'running',
    progress: 0.2,
    round: 0,
    elapsedMs: 1234,
    detail: { evidences: 42 },
  } satisfies NodeUpdateEvent,

  progress: {
    ...ENVELOPE,
    type: 'progress',
    stage: 'collect',
    progress: 0.25,
    message: '正在检索第 3 个角度',
    tokens: 51677,
    costUsd: 0.0042,
  } satisfies ProgressEvent,

  thought: {
    ...ENVELOPE,
    type: 'thought',
    thought: {
      id: 'TH-0001',
      expertId: 'L3-001',
      expertName: '调研总监',
      roleTitle: '统筹全局',
      level: 'L3',
      stage: 'intake',
      text: '收到调研需求。',
      at: '2026-01-01T00:00:00.000+00:00',
    },
  } satisfies ThoughtEvent,

  message: {
    ...ENVELOPE,
    type: 'message',
    message: {
      from: 'L3-001',
      to: 'L2-001',
      kind: 'handoff',
      stage: 'orchestrator',
      round: 0,
      summary: '队伍已就位：6 位执行专家、3 位战略专家',
      issueCount: 0,
    },
  } satisfies MessageEvent,

  evidence: {
    ...ENVELOPE,
    type: 'evidence',
    evidence: {
      evidenceId: 'EV-a1b2c3d4e5f6',
      url: 'https://sspai.com/post/1',
      title: '少数派测评',
      snippet: '摘要',
      fullText: '正文',
      brand: 'Notion',
      sourceType: 'news',
      siteName: '少数派',
      publishedAt: '2026-01-01T00:00:00+00:00',
      capturedAt: '2026-01-02T00:00:00.000+00:00',
      matchedDimensions: ['功能覆盖'],
      query: 'Notion 功能',
      provider: 'mock',
      rank: 1,
      credibility: 82.5,
      credibilityBreakdown: {
        sourceTypeScore: 60,
        freshnessScore: 20,
        contentScore: 8,
        crossRefScore: 5,
        penalties: -10.5,
        notes: ['正文抓取失败，仅有摘要'],
        total: 82.5,
      },
      degraded: false,
      images: [{ url: 'https://sspai.com/a.png', alt: '截图' }],
    },
  } satisfies EvidenceEvent,

  trace: {
    ...ENVELOPE,
    type: 'trace',
    span: {
      spanId: 'SP-00001',
      parentId: '',
      kind: 'llm',
      name: 'analyze_claims',
      purpose: '分析论点',
      provider: 'mock',
      model: 'mock-core',
      startedAt: '2026-01-01T00:00:00.000+00:00',
      endedAt: '2026-01-01T00:00:00.250+00:00',
      durationMs: 250,
      promptTokens: 120,
      completionTokens: 30,
      totalTokens: 150,
      costUsd: 0.0042,
      cachedPromptTokens: 96,
      status: 'ok',
      error: '',
      detail: { claims: 3 },
    },
  } satisfies TraceEvent,

  chart: {
    ...ENVELOPE,
    type: 'chart',
    chart: {
      chartId: 'chart-matrix-bar',
      kind: 'bar',
      title: '功能维度评分对比',
      evidenceIds: ['EV-a1b2c3d4e5f6'],
      spec: {
        categories: ['功能覆盖'],
        series: [{ brand: 'Notion', color: '#5B8FF9', values: [4.3] }],
        yAxis: { min: 0, max: 5, name: '评分' },
      },
    },
  } satisfies ChartEvent,

  image: {
    ...ENVELOPE,
    type: 'image',
    image: {
      url: 'https://sspai.com/a.png',
      alt: '截图',
      evidenceId: 'EV-a1b2c3d4e5f6',
      brand: 'Notion',
      sourceUrl: 'https://sspai.com/post/1',
      siteName: '少数派',
    },
  } satisfies ImageEvent,

  report_ready: {
    ...ENVELOPE,
    type: 'report_ready',
    reportId: 'RP-000000000000',
    query: '对比 Notion 与 Obsidian',
    subject: 'Notion 与 Obsidian',
    sectionCount: 8,
    evidenceCount: 216,
    problems: [],
    degraded: [],
  } satisfies ReportReadyEvent,

  done: {
    ...ENVELOPE,
    type: 'done',
    reportId: 'RP-000000000000',
    metrics: {} as DoneEvent['metrics'],
    degraded: [],
    problems: [],
  } satisfies DoneEvent,

  error: {
    ...ENVELOPE,
    type: 'error',
    stage: 'intake',
    message: 'mock：purpose 被配置为失败',
    kind: 'Transient',
    retryable: true,
  } satisfies ErrorEvent,
}

interface ContractEntry {
  required: string[]
  optional?: string[]
  carries: string | null
}

const EVENTS = contract.events as Record<string, ContractEntry>
const DOMAIN = contract.domain as Record<string, string[]>
const ENVELOPE_KEYS = new Set(contract.envelope.required)

/**
 * 按类型取契约条目。
 *
 * 用函数而不是 `EVENTS[type]`：`noUncheckedIndexedAccess` 下后者的类型是
 * `T | undefined`，而"契约里没有这一种事件"恰恰是上面那条集合相等的
 * 测试要报的错——不该在这里退化成一串 `undefined` 传播下去，
 * 那会让真正的错因埋在一堆类型错误里。
 */
function entryOf(type: PipelineEventType): ContractEntry {
  const entry = EVENTS[type]
  if (!entry) throw new Error(`契约里没有 ${type} 这一种事件`)
  return entry
}

/** 载荷的键 = 全部键 − 信封。信封那一份由 `EventBase` 提供，不在各变体里重复。 */
function payloadKeys(sample: object): Set<string> {
  return new Set(Object.keys(sample).filter((key) => !ENVELOPE_KEYS.has(key)))
}

describe('事件类型集合', () => {
  it('与后端 EVENT_TYPES 严格相等', () => {
    // 集合不等说明有一边新增或删掉了一种事件，那一定会让另一边的 switch
    // 走到未定义分支上。顺序不比：顺序不描述任何行为。
    expect([...EVENT_TYPES].sort()).toEqual(Object.keys(EVENTS).sort())
  })

  it('没有重复项', () => {
    expect(new Set(EVENT_TYPES).size).toBe(EVENT_TYPES.length)
  })
})

describe('样例对象与契约', () => {
  it.each(EVENT_TYPES)('%s 的载荷键与契约完全一致', (type) => {
    const entry = entryOf(type)
    const declared = new Set([...entry.required, ...(entry.optional ?? [])])
    const actual = payloadKeys(SAMPLES[type])

    const missing = [...entry.required].filter((key) => !actual.has(key))
    const extra = [...actual].filter((key) => !declared.has(key))

    expect({ type, missing, extra }).toEqual({ type, missing: [], extra: [] })
  })

  it.each(EVENT_TYPES)('%s 的信封四个键一个不少', (type) => {
    const envelope = Object.keys(SAMPLES[type]).filter((key) => ENVELOPE_KEYS.has(key))

    expect(envelope.sort()).toEqual([...ENVELOPE_KEYS].sort())
  })

  it.each(EVENT_TYPES)('%s 的 type 字段就是它自己的名字', (type) => {
    expect(SAMPLES[type].type).toBe(type)
  })
})

describe('被携带的领域对象', () => {
  const CARRYING = EVENT_TYPES.filter((type) => entryOf(type).carries)

  it('携带领域对象的恰好是六种', () => {
    // 与后端契约里的划分一致：其余五种是「事件自身的事实」，载荷摊平。
    expect(CARRYING).toHaveLength(6)
  })

  it.each(CARRYING)('%s 里那个对象的键与契约完全一致', (type) => {
    const carried = entryOf(type).carries
    if (!carried) throw new Error(`${type} 不携带领域对象`)
    // `trace` 的载荷键叫 `span`（装的是 TraceSpan），其余与自己同名
    const key = type === 'trace' ? 'span' : type
    const value = (SAMPLES[type] as unknown as Record<string, unknown>)[key]
    const declared = DOMAIN[carried]
    if (!declared) throw new Error(`契约的 domain 里没有 ${carried}`)

    if (typeof value !== 'object' || value === null) {
      throw new Error(`${type}.${key} 不是一个对象`)
    }
    // 相等而不是包含：对象多一个字段，界面那块内容就少一份数据，
    // 而且是静默地少。
    expect(Object.keys(value).sort()).toEqual([...declared].sort())
  })
})

describe('运行时校验', () => {
  it('放过每一个合法样例', () => {
    for (const type of EVENT_TYPES) {
      expect(isPipelineEvent(SAMPLES[type])).toBe(true)
    }
  })

  it('挡下未知的 type', () => {
    expect(isPipelineEvent({ ...ENVELOPE, type: 'not_a_real_event' })).toBe(false)
  })

  it('挡下缺 seq 的帧', () => {
    const { seq: _seq, ...rest } = SAMPLES.done
    expect(isPipelineEvent(rest)).toBe(false)
  })

  it('挡下非对象', () => {
    expect(isPipelineEvent(null)).toBe(false)
    expect(isPipelineEvent('done')).toBe(false)
    expect(isPipelineEvent(undefined)).toBe(false)
  })
})
