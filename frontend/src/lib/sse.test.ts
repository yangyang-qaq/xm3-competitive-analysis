/**
 * `EventSource` 封装的守卫。
 *
 * 这里的每一条都是"错了不会报错"的那种：收不到事件是 200、控制台干净、
 * 页面只是空的；不断线重连也是安静的，只是永远停在半路。
 * 所以它们只能靠测试发现，而测试必须真能红（见 `scripts/falsify_frontend.py`）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakeEventSource, installFakeEventSource } from '../test/fakeEventSource'
import type { PipelineEvent } from '../types/events'
import { openTaskStream, type TaskStreamHandlers } from './sse'

let uninstall: () => void

beforeEach(() => {
  uninstall = installFakeEventSource()
})

afterEach(() => {
  uninstall()
  vi.restoreAllMocks()
})

/** 一条思维。单独拿出来，好让别的帧也能引用它。 */
function thought(seq: number) {
  return {
    id: `TH-${seq}`,
    expertId: 'L1-001',
    expertName: '某专家',
    roleTitle: '',
    level: 'L1' as const,
    stage: 'collect' as const,
    text: `第 ${seq} 条`,
    at: '2026-01-01T00:00:00.000Z',
  }
}

/** 一条形状合法的 `thought` 帧。 */
function thoughtFrame(seq: number): PipelineEvent {
  return {
    seq,
    taskId: 'TK-1',
    createdAt: '2026-01-01T00:00:00.000Z',
    type: 'thought',
    thought: thought(seq),
  }
}

function collector() {
  const events: PipelineEvent[] = []
  const calls = { open: 0, dropped: [] as number[], fatal: [] as string[] }
  const handlers: TaskStreamHandlers = {
    onEvent: (event) => events.push(event),
    onOpen: () => (calls.open += 1),
    onDropped: (watermark) => calls.dropped.push(watermark),
    onFatal: (reason) => calls.fatal.push(reason),
  }
  return { events, calls, handlers }
}

describe('连接与续传起点', () => {
  it('fromSeq 进查询参数，因为新建连接发不了请求头', () => {
    openTaskStream('TK-1', collector().handlers, { fromSeq: 12 })
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-1/stream?from_seq=12')
  })

  it('fromSeq 为 0 或缺失时不带查询参数（全量回放）', () => {
    openTaskStream('TK-1', collector().handlers)
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-1/stream')
  })

  it('负数水位被夹到 0，初始水位不会是负数', () => {
    // 这条用例第一版**什么也没测到**：它断言的是 URL，而 URL 那一边是靠
    // `fromSeq > 0` 挡住的，`Math.max(0, ...)` 在不在都不影响结果——
    // 反证的时候它没红，才发现的。
    //
    // `Math.max(0, ...)` 真正保护的是**初始水位**：它会是 `onDropped()` 报出去的
    // 那个数，而 `-5` 作为"最后收到第几条事件"没有意义。
    // 后端的 `Query(0, ge=0)` 会拿 422 拒掉 `from_seq=-1`，而这个连接是页面
    // 加载时建的，422 的表现是"工作台永远空白"，排查起来离原因很远。
    const stream = openTaskStream('TK-1', collector().handlers, { fromSeq: -5 })
    expect(stream.watermark()).toBe(0)
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-1/stream')
  })

  it('正数水位原样成为初始水位', () => {
    const stream = openTaskStream('TK-1', collector().handlers, { fromSeq: 12 })
    expect(stream.watermark()).toBe(12)
  })

  it('任务 id 会做 URL 转义', () => {
    openTaskStream('TK/1 号', collector().handlers)
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK%2F1%20%E5%8F%B7/stream')
  })
})

describe('按类型分发', () => {
  it('具名帧能收到——这才是 `onmessage` 收不到的那一半', () => {
    // `sse.ts` 若改用 `onmessage`，这一条必红：替身忠实于规范，
    // 具名帧不发给 `onmessage`。这正是"连接 200、控制台干净、页面全空"的成因。
    const { events, handlers } = collector()
    openTaskStream('TK-1', handlers)
    FakeEventSource.last.emit('thought', thoughtFrame(1))

    expect(events).toHaveLength(1)
    expect(events[0]?.type).toBe('thought')
  })

  it('11 种事件类型每一种都能收到', () => {
    // 漏挂一种的表现是"某一类内容永远不出现"，而其它都正常——
    // 最容易归因成后端没推。这里逐个类型推一帧，断言**每一帧都到了**：
    // 只断言"没抛异常"是测不出漏挂的，漏挂恰恰是安静的那种失败。
    const { events, handlers } = collector()
    openTaskStream('TK-1', handlers)
    const source = FakeEventSource.last

    const base = { taskId: 'TK-1', createdAt: '2026-01-01T00:00:00.000Z' }
    const frames: Record<string, unknown> = {
      node_update: { ...base, type: 'node_update', stage: 'collect', label: '联网采集', status: 'running', progress: 0.4, round: 0 },
      progress: { ...base, type: 'progress', stage: 'collect', progress: 0.5, message: '', tokens: 0, costUsd: 0 },
      thought: { ...base, type: 'thought', thought: thought(1) },
      message: { ...base, type: 'message', message: { from: 'A', to: 'B', kind: 'handoff', stage: 'collect', round: 0, summary: '', issueCount: 0 } },
      evidence: { ...base, type: 'evidence', evidence: { evidenceId: 'E1' } },
      trace: { ...base, type: 'trace', span: { spanId: 'SP-1' } },
      chart: { ...base, type: 'chart', chart: { chartId: 'C1' } },
      image: { ...base, type: 'image', image: { url: 'u1' } },
      report_ready: { ...base, type: 'report_ready', reportId: 'RP-1', query: 'q', subject: 's', sectionCount: 1, evidenceCount: 1, problems: [], degraded: [] },
      done: { ...base, type: 'done', reportId: 'RP-1', metrics: {}, degraded: [], problems: [] },
      error: { ...base, type: 'error', stage: 'collect', message: '炸了', kind: 'X', retryable: false },
    }

    let seq = 0
    const pushed: string[] = []
    for (const [type, frame] of Object.entries(frames)) {
      seq += 1
      pushed.push(type)
      source.emit(type, { seq, ...(frame as object) })
    }

    expect(events.map((event) => event.type)).toEqual(pushed)
  })

  it('onopen 会把状态报回去（重连成功也要报）', () => {
    const { calls, handlers } = collector()
    openTaskStream('TK-1', handlers)
    FakeEventSource.last.open()
    expect(calls.open).toBe(1)
  })
})

describe('水位', () => {
  it('收到一帧就前进', () => {
    const stream = openTaskStream('TK-1', collector().handlers)
    FakeEventSource.last.emit('thought', thoughtFrame(3))
    FakeEventSource.last.emit('thought', thoughtFrame(4))
    expect(stream.watermark()).toBe(4)
  })

  it('乱序到达时水位不回退', () => {
    const stream = openTaskStream('TK-1', collector().handlers)
    FakeEventSource.last.emit('thought', thoughtFrame(9))
    FakeEventSource.last.emit('thought', thoughtFrame(2))
    expect(stream.watermark()).toBe(9)
  })

  it('水位**先推进再分发**：分发抛异常也不会卡在同一帧', () => {
    // 反过来写的话，一次抛异常会让水位停住，于是重连时服务端把同一帧
    // 再补发一次、再抛一次——每断一次网就卡死在同一处。
    const stream = openTaskStream('TK-1', {
      onEvent: () => {
        throw new Error('消费方炸了')
      },
    })
    expect(() => FakeEventSource.last.emit('thought', thoughtFrame(7))).toThrow('消费方炸了')
    expect(stream.watermark()).toBe(7)
  })
})

describe('坏帧', () => {
  it('解析不了的帧被跳过，后面的帧照收', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { events, calls, handlers } = collector()
    openTaskStream('TK-1', handlers)

    FakeEventSource.last.emit('thought', '{这不是 JSON')
    FakeEventSource.last.emit('thought', thoughtFrame(2))

    expect(events).toHaveLength(1)
    // 关键：**不能**因为一帧坏了就报致命。后面还有几百帧。
    expect(calls.fatal).toEqual([])
  })

  it('形状不合契约的帧被跳过（比如没有 seq）', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { events, handlers } = collector()
    openTaskStream('TK-1', handlers)

    FakeEventSource.last.emit('thought', { type: 'thought', taskId: 'TK-1' })
    expect(events).toEqual([])
  })

  it('坏帧不推进水位', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const stream = openTaskStream('TK-1', collector().handlers)
    FakeEventSource.last.emit('thought', 'garbage')
    expect(stream.watermark()).toBe(0)
  })
})

describe('出错时的两条完全不同的路', () => {
  it('CONNECTING：浏览器自己在退避重连，**不要 close**', () => {
    // 这里 `close()` 是最坏的选择：它把浏览器自带的、带退避的自动重连
    // 关掉，然后我们手上什么都没有。表现是"网络抖一下就永久停更"。
    const { calls, handlers } = collector()
    const stream = openTaskStream('TK-1', handlers)
    const source = FakeEventSource.last

    source.error(FakeEventSource.CONNECTING)

    expect(source.closed).toBe(false)
    expect(calls.dropped).toEqual([0])
    expect(calls.fatal).toEqual([])
    expect(stream.watermark()).toBe(0)
  })

  it('CONNECTING 时报出的水位是当时已收到的条数', () => {
    const { calls, handlers } = collector()
    openTaskStream('TK-1', handlers)
    FakeEventSource.last.emit('thought', thoughtFrame(5))
    FakeEventSource.last.error(FakeEventSource.CONNECTING)
    expect(calls.dropped).toEqual([5])
  })

  it('CLOSED：不会回来了，报致命', () => {
    const { calls, handlers } = collector()
    openTaskStream('TK-1', handlers)
    FakeEventSource.last.error(FakeEventSource.CLOSED)

    expect(calls.fatal).toHaveLength(1)
    expect(calls.dropped).toEqual([])
  })

  it('close() 之后 onerror 不再往上冒', () => {
    // unmount 之后浏览器还可能补一个 error 事件。不管它的话，
    // 已经离开的工作台会把状态改回"重连中"。
    const { calls, handlers } = collector()
    const stream = openTaskStream('TK-1', handlers)
    const source = FakeEventSource.last

    stream.close()
    source.error(FakeEventSource.CLOSED)

    expect(source.closed).toBe(true)
    expect(calls.fatal).toEqual([])
  })

  it('close() 之后 onopen 也不上报', () => {
    const { calls, handlers } = collector()
    const stream = openTaskStream('TK-1', handlers)
    stream.close()
    FakeEventSource.last.open()
    expect(calls.open).toBe(0)
  })
})
