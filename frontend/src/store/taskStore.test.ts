/**
 * store 的五条硬承诺：
 *
 *  1. **一批事件一次 `set()`。** 参考实现是每条事件一次
 *     `set({thoughts: [...s.thoughts, d]})`——O(n²) 拷贝 + 每事件一次渲染。
 *     这条测试数的是**渲染次数**，不是最终状态：状态对了但渲染了 50 次，
 *     正是要修的那个缺陷。
 *  2. **重连水位只增不减。** 退回去的代价不是"显示错了"，是白传几百帧。
 *  3. **网线断了不丢已收到的东西。** 一次网络故障不该让工作台变白。
 *  4. **任务跑完就关流，不再重连。** 服务端发完历史会正常关流，而
 *     `EventSource` 把"正常关闭"当成"掉线"重连——不拦就是一个
 *     永不停止的循环（实测 149 次请求，全打在一个已经 done 的任务上）。
 *  5. **切到后台标签页，事件照样落地。** `requestAnimationFrame` 在隐藏的
 *     标签页里根本不回调，只挂 rAF 的话界面会冻在切走前那一帧。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakeEventSource, installFakeEventSource } from '../test/fakeEventSource'
import type { CreatedTask, RunMetrics, TaskSnapshot } from '../types/domain'
import type { PipelineEvent } from '../types/events'
import { fetchSnapshot, FLUSH_FALLBACK_MS, useTaskStore } from './taskStore'

// ---- rAF 手动挡 ----
//
// `taskStore` 用 `requestAnimationFrame` 合帧。真跑的话每个用例都要
// `await` 一帧，而"加了多少次 set"这件事就断不出来了。
let frames: (() => void)[] = []
let restoreRaf: () => void

function flushFrame(): void {
  const queued = frames
  frames = []
  for (const run of queued) run()
}

beforeEach(() => {
  const original = globalThis.requestAnimationFrame
  frames = []
  globalThis.requestAnimationFrame = ((run: () => void) => {
    frames.push(run)
    return frames.length
  }) as typeof requestAnimationFrame
  restoreRaf = () => {
    globalThis.requestAnimationFrame = original
  }

  useTaskStore.getState().reset()
})

afterEach(() => {
  useTaskStore.getState().reset()
  restoreRaf()
})

/**
 * 数 `set()` 被调了几次。zustand 每次状态变化都会叫一次订阅者。
 *
 * **要在 `connect()` 之后才开始数。** `connect()` 自己会改两次状态
 * （先 `disconnect()` 一次、再置 `connecting` 一次），把那些算进来
 * 会让"事件引起的渲染次数"这个数没有意义——而这里要测的正是后者。
 */
function countSets(): { count: () => number; stop: () => void } {
  let count = 0
  const unsubscribe = useTaskStore.subscribe(() => {
    count += 1
  })
  return {
    // 取值**不解除订阅**。第一版写成"取值时顺手 unsubscribe"，
    // 于是同一个用例里第二次取值拿到的是第一次的旧数——
    // 而它看起来像"后面的 set 没发生"。
    count: () => count,
    stop: unsubscribe,
  }
}

function thoughtEvent(seq: number): PipelineEvent {
  return {
    seq,
    taskId: 'TK-1',
    createdAt: '2026-01-01T00:00:00.000Z',
    type: 'thought',
    thought: {
      id: `TH-${seq}`,
      expertId: 'L1-001',
      expertName: '某专家',
      roleTitle: '',
      level: 'L1',
      stage: 'collect',
      text: `第 ${seq} 条`,
      at: '2026-01-01T00:00:00.000Z',
    },
  }
}

function snapshot(overrides: Partial<TaskSnapshot> = {}): TaskSnapshot {
  return {
    taskId: 'TK-1',
    status: 'running',
    stage: 'collect',
    stageLabel: '联网采集',
    progress: 0.4,
    reportId: '',
    error: '',
    nodes: {},
    lastSeq: 0,
    needClarify: false,
    awaitingClarify: false,
    clarifyQuestions: [],
    subject: '笔记软件',
    brands: ['Notion'],
    ...overrides,
  }
}

/**
 * 一份**字段齐全**的指标。
 *
 * 不写成 `{} as RunMetrics`：`as` 会让这个夹具在 `RunMetrics` 长出新的
 * 必填字段时静默失效，而它守护的恰恰是"终态事件长什么样"。
 */
const METRICS: RunMetrics = {
  elapsedSeconds: 1,
  firstEvidenceSeconds: 1,
  llmCalls: 1,
  totalTokens: 1,
  costUsd: 0,
  evidenceCount: 1,
  independentDomains: 1,
  platformCount: 1,
  highConfidenceRatio: 0,
  reworkRounds: 0,
  efficiencyMultiple: 1,
  coverageRatio: 1,
  consistency: 1,
  accuracy: 1,
  manualCorrectionRate: 0,
  baselines: {},
}

function doneEvent(seq: number): PipelineEvent {
  return {
    seq,
    taskId: 'TK-1',
    createdAt: '2026-01-01T00:00:00.000Z',
    type: 'done',
    reportId: 'RP-1',
    metrics: METRICS,
    degraded: [],
    problems: [],
  }
}

describe('批处理：一批事件一次 set()', () => {
  it('50 条事件只触发一次状态变更', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const source = FakeEventSource.last
    const sets = countSets()

    for (let seq = 1; seq <= 50; seq += 1) source.emit('thought', thoughtEvent(seq))
    // 还没到帧：一次 `set()` 都不该发生。事件进了模块级缓冲，不进 React 状态。
    // 这一条就是 O(n²) 那个缺陷的守卫：参考实现在这里已经 set 了 50 次，
    // 每次都把整个数组复制一遍、每次都重渲染一遍工作台。
    expect(sets.count()).toBe(0)

    flushFrame()
    expect(sets.count()).toBe(1)
    expect(useTaskStore.getState().task.thoughts).toHaveLength(50)

    sets.stop()
    uninstall()
  })

  it('分两帧到达就分两次 set()，第二条不会捎带第一条重放', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const source = FakeEventSource.last
    const sets = countSets()

    source.emit('thought', thoughtEvent(1))
    flushFrame()
    source.emit('thought', thoughtEvent(2))
    flushFrame()

    expect(sets.count()).toBe(2)
    // 去重的作用在这里：第二帧只处理 seq=2。没有去重的话，
    // 第二帧会把 seq=1 再放一遍，思维流里出现两条"第 1 条"。
    expect(useTaskStore.getState().task.thoughts.map((t) => t.text)).toEqual([
      '第 1 条',
      '第 2 条',
    ])
    sets.stop()
    uninstall()
  })

  it('没有事件的帧不产生 set()', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const sets = countSets()

    flushFrame()
    expect(sets.count()).toBe(0)
    sets.stop()
    uninstall()
  })

  it('reset 丢掉缓冲里还没落地的帧', () => {
    // 与 `disconnect` 同一条道理：那些帧属于上一条连接。
    // 这一条曾经写成"reset 之后重建连接，事件照样会渲染"，并且它
    // **抓不住任何缺陷**——反证时才发现：那个已经排好的 rAF 回调
    // 会在下一次 `flushFrame()` 里照常跑，把新事件冲掉，于是断言总是绿的。
    // 它测的是"浏览器会执行你排的回调"，不是 `reset`。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    FakeEventSource.last.emit('thought', thoughtEvent(1))
    // 故意不 flush：此刻 `pending` 里有一条，`scheduled` 是 true
    useTaskStore.getState().reset()

    flushFrame()
    expect(useTaskStore.getState().task.thoughts).toEqual([])
    uninstall()
  })

  it('reset 之后标成 idle，可以重新连下一个任务', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    useTaskStore.getState().reset()
    expect(useTaskStore.getState().connection).toBe('idle')

    useTaskStore.getState().connect('TK-2')
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-2/stream')
    uninstall()
  })
})

describe('水位', () => {
  it('重连时取 fromSeq 与手上水位的较大者', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().hydrate(snapshot({ lastSeq: 100 }))
    useTaskStore.getState().connect('TK-1', 10)

    // 用 `fromSeq` 会退回快照那一刻，于是重连要重放 90 帧已经看过的事件。
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-1/stream?from_seq=100')
    uninstall()
  })

  it('手上水位更低时用 fromSeq', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1', 42)
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-1/stream?from_seq=42')
    uninstall()
  })

  it('hydrate 只增水位：后到的旧快照不会把它拉回去', () => {
    // 快照是一个更早发出的请求，它的 `lastSeq` 可能落后于已经通过 SSE
    // 收到的那些。水位退回去等于下次重连重传一大段。
    const store = useTaskStore.getState()
    store.hydrate(snapshot({ lastSeq: 50 }))
    expect(useTaskStore.getState().lastSeq).toBe(50)

    store.hydrate(snapshot({ lastSeq: 5 }))
    expect(useTaskStore.getState().lastSeq).toBe(50)
  })

  it('重连成功后水位继续往前走，不退回连接建立时那一刻', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const source = FakeEventSource.last

    source.emit('thought', thoughtEvent(9))
    flushFrame()
    source.error(FakeEventSource.CONNECTING)
    expect(useTaskStore.getState().lastSeq).toBe(9)

    // 浏览器自动重连成功，又来了两条
    source.open()
    source.emit('thought', thoughtEvent(10))
    flushFrame()
    expect(useTaskStore.getState().lastSeq).toBe(10)
    uninstall()
  })
})

describe('连接状态', () => {
  it('connect 之后是 connecting，onopen 之后是 live', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    expect(useTaskStore.getState().connection).toBe('connecting')

    FakeEventSource.last.open()
    expect(useTaskStore.getState().connection).toBe('live')
    uninstall()
  })

  it('CONNECTING 时进 reconnecting，并说明已经收到多少', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const source = FakeEventSource.last
    source.emit('thought', thoughtEvent(3))
    flushFrame()
    source.error(FakeEventSource.CONNECTING)

    expect(useTaskStore.getState().connection).toBe('reconnecting')
    expect(useTaskStore.getState().connectionNote).toContain('3')
    uninstall()
  })

  it('致命错误之后 disconnect 不会把 fatal 抹成 idle', () => {
    // 抹掉的话界面会说"空闲"，而事实是这条流永远不会再来了。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    FakeEventSource.last.error(FakeEventSource.CLOSED)
    expect(useTaskStore.getState().connection).toBe('fatal')

    useTaskStore.getState().disconnect()
    expect(useTaskStore.getState().connection).toBe('fatal')
    uninstall()
  })

  it('致命错误**不清空**已经收到的内容', () => {
    // 一次网络故障不该让整个工作台变白。清掉的话用户看到的是
    // "跑了半天的东西全没了"，而其实都在，只是流断了。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    FakeEventSource.last.emit('thought', thoughtEvent(1))
    flushFrame()
    FakeEventSource.last.error(FakeEventSource.CLOSED)

    expect(useTaskStore.getState().task.thoughts).toHaveLength(1)
    uninstall()
  })

  it('disconnect 丢掉缓冲里还没落地的帧', () => {
    // 断开之后到达的帧属于上一条连接。下一次连接会按水位重新补发，
    // 所以丢掉它们不丢信息；而 flush 它们会让"断开"变成"再渲染一次"。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    FakeEventSource.last.emit('thought', thoughtEvent(1))
    useTaskStore.getState().disconnect()

    flushFrame()
    expect(useTaskStore.getState().task.thoughts).toEqual([])
    uninstall()
  })

  it('重复 connect 会先关掉上一条流', () => {
    // 不关的话，一个 React 严格模式下的二次挂载会留下两条对着同一个
    // 任务的事件流，两边都在 `set()`。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const first = FakeEventSource.last
    useTaskStore.getState().connect('TK-1')

    expect(first.closed).toBe(true)
    expect(FakeEventSource.instances).toHaveLength(2)
    uninstall()
  })
})

describe('任务跑完之后，流要停', () => {
  // 这一组守的是一个**永不停止的循环**，不是一次显示错误。
  //
  // 服务端把历史发完就主动关流（见 `routes_tasks.py` 那段 docstring：
  // "这条连接会自己关掉——而不是永远挂着一条不再有内容的流"）。
  // 这是对的。但 `EventSource` 分不出"服务端正常关流"和"网络掉线"，
  // 它一律按掉线处理并自动重连，而重连打的还是构造时拼死的那个 URL。
  //
  // 实测：一个已经 done 的任务，浏览器打了 **149 次**
  // `GET /api/tasks/TK-1d1ff1f7090b/stream?from_seq=16`，每 4.8 秒一次，
  // 界面上一直写着"重连中"。旁边那个 14% 是另一个缺陷（见文件末尾那一组）。

  it('收到 done 就主动关流，连接记成 ended', () => {
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const source = FakeEventSource.last

    source.emit('done', doneEvent(5))
    flushFrame()

    expect(source.closed).toBe(true)
    expect(useTaskStore.getState().connection).toBe('ended')
    uninstall()
  })

  it('done 还躺在缓冲里时流就断了——也判结束，不当掉线', () => {
    // **这一条是最接近线上实况的那条。**
    //
    // 服务端推完历史立刻关流，而"关流"这一刻 rAF 很可能还没跑过——
    // 最后那批事件（含 `done`）还在 `pending` 里。所以 `onDropped`
    // **必须先 flush 再判断**：不 flush 的话看到的状态是 `running`，
    // 于是一个已经跑完的任务被判成掉线，交给浏览器去重连，
    // 服务端每次再关一遍——这就是那 149 次请求。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const source = FakeEventSource.last

    source.emit('done', doneEvent(5))
    source.error(FakeEventSource.CONNECTING) // 刻意不 flushFrame()

    expect(useTaskStore.getState().task.status).toBe('done')
    expect(useTaskStore.getState().connection).toBe('ended')
    expect(useTaskStore.getState().connectionNote).not.toContain('重连')
    uninstall()
  })

  it('终态来自快照时（历史里没有 done 事件）也判结束', () => {
    // 打开一个早就跑完的任务：快照说 done，`from_seq` 已经在历史末尾，
    // 服务端一条都不发就关流。这时不该有任何重连。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().hydrate(snapshot({ status: 'done', lastSeq: 520, progress: 1 }))
    useTaskStore.getState().connect('TK-1')
    FakeEventSource.last.error(FakeEventSource.CONNECTING)

    expect(useTaskStore.getState().connection).toBe('ended')
    uninstall()
  })

  it('终态任务拿到"致命断开"也判结束，不说"连接已断开"', () => {
    // `CLOSED` 分支不会重连，所以它不是那 149 次请求的来源。这一条守的是
    // **别说错话**：一个已经跑完的任务配一句"连接已断开"，会让人去查网络——
    // 而网络没事，是任务本来就结束了。
    //
    // 换到 `CLOSED` 分支上问同一句话，是因为浏览器把"服务端正常关流"
    // 归到哪个分支**不在我们的控制里**。只在一处判断的话，这个修复
    // 成不成全看浏览器当天怎么归类。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().hydrate(snapshot({ status: 'done', lastSeq: 520, progress: 1 }))
    useTaskStore.getState().connect('TK-1')
    FakeEventSource.last.error(FakeEventSource.CLOSED)

    expect(useTaskStore.getState().connection).toBe('ended')
    expect(useTaskStore.getState().connectionNote).not.toContain('断开')
    uninstall()
  })

  it('**跑着的**任务掉线仍然进重连', () => {
    // 上一条的反面，缺了它这个修复就太宽了：把"关流"写成"断线就关"，
    // 一次网络抖动会让工作台永久停更——而界面上写着"已结束"，
    // 看起来像任务跑完了。那比 149 次请求难查得多。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    const source = FakeEventSource.last

    source.emit('thought', thoughtEvent(3))
    flushFrame()
    source.error(FakeEventSource.CONNECTING)

    expect(useTaskStore.getState().connection).toBe('reconnecting')
    expect(source.closed).toBe(false)
    uninstall()
  })

  it('结束之后 disconnect 不会把 ended 抹成 idle', () => {
    // 与 `fatal` 同一个道理（见上面那条）：抹掉之后"跑完了"和"从没连上"
    // 在界面上长得一模一样。
    const uninstall = installFakeEventSource()
    useTaskStore.getState().connect('TK-1')
    FakeEventSource.last.emit('done', doneEvent(5))
    flushFrame()

    useTaskStore.getState().disconnect()
    expect(useTaskStore.getState().connection).toBe('ended')
    uninstall()
  })
})

describe('后台标签页：rAF 不回调也要落地', () => {
  // `requestAnimationFrame` 在隐藏的标签页里**根本不回调**——Chrome 是
  // 暂停它，不是让它变慢。只挂 rAF 的话，用户切走的这段时间里事件一直在到、
  // 缓冲一直在涨，而界面一条都不出现：回到页面上是一个冻住的进度条，
  // 旁边还有一个还在滚的网络面板。
  //
  // 实测过：任务已经在后端跑完（journal 里 520 条），页面停在 **14%**，
  // 正好是切走那一刻快照里的进度。
  //
  // 这一组里的用例只伪造 `setTimeout`/`clearTimeout`，**不伪造 rAF**：
  // 上面的 `beforeEach` 已经把 rAF 换成了手动挡，而"rAF 没回调"就是
  // "不调 `flushFrame()`"。把 rAF 一起交给假定时器的话，
  // `advanceTimersByTime` 会顺带把它推进，用例会因为错的原因变绿。

  it('累计一帧都没跑过时，兜底定时器把事件送进状态', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const uninstall = installFakeEventSource()
      useTaskStore.getState().connect('TK-1')
      FakeEventSource.last.emit('thought', thoughtEvent(1))

      expect(useTaskStore.getState().task.thoughts).toHaveLength(0)

      vi.advanceTimersByTime(FLUSH_FALLBACK_MS)

      expect(useTaskStore.getState().task.thoughts).toHaveLength(1)
      uninstall()
    } finally {
      vi.useRealTimers()
    }
  })

  it('兜底路径上，同一窗口里的多条事件仍然只 set() 一次', () => {
    // 兜底定时器不能把"一批一次"变成"一条一次"——那个性质是这里
    // 唯一存在的理由（见文件头第 1 条）。`setTimeout(run, ...)` 是
    // **每个窗口排一次**，而不是每条事件排一次。
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const uninstall = installFakeEventSource()
      useTaskStore.getState().connect('TK-1')
      const sets = countSets()
      const source = FakeEventSource.last

      source.emit('thought', thoughtEvent(1))
      source.emit('thought', thoughtEvent(2))
      source.emit('thought', thoughtEvent(3))
      expect(sets.count()).toBe(0)

      vi.advanceTimersByTime(FLUSH_FALLBACK_MS)

      expect(sets.count()).toBe(1)
      expect(useTaskStore.getState().task.thoughts).toHaveLength(3)
      uninstall()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('快照进 store', () => {
  it('CreatedTask 带着 query，就更新 query', () => {
    const created: CreatedTask = { ...snapshot(), query: '对比 Notion 与 Obsidian', mode: 'quick' }
    useTaskStore.getState().hydrate(created)
    expect(useTaskStore.getState().query).toBe('对比 Notion 与 Obsidian')
    expect(useTaskStore.getState().subject).toBe('笔记软件')
  })

  it('普通快照没有 query，保留已有的 subject/brands', () => {
    const store = useTaskStore.getState()
    store.hydrate({ ...snapshot(), query: '先来的', mode: 'quick' } as CreatedTask)
    store.hydrate(snapshot({ subject: '', brands: [] }))

    expect(useTaskStore.getState().query).toBe('先来的')
    expect(useTaskStore.getState().subject).toBe('笔记软件')
    expect(useTaskStore.getState().brands).toEqual(['Notion'])
  })

  it('brands 给空数组时不覆盖（回头路：那一刻还没解析出品牌）', () => {
    const store = useTaskStore.getState()
    store.hydrate(snapshot({ brands: ['Notion', 'Obsidian'] }))
    store.hydrate(snapshot({ brands: [] }))
    expect(useTaskStore.getState().brands).toEqual(['Notion', 'Obsidian'])
  })

  it('reset 清空任务与连接', () => {
    const uninstall = installFakeEventSource()
    const store = useTaskStore.getState()
    store.connect('TK-1')
    store.hydrate(snapshot({ lastSeq: 30 }))

    useTaskStore.getState().reset()
    const after = useTaskStore.getState()
    expect(after.lastSeq).toBe(0)
    expect(after.connection).toBe('idle')
    expect(after.task.thoughts).toEqual([])
    uninstall()
  })
})

describe('REST 出口', () => {
  it('fetchSnapshot 打到任务的快照接口', async () => {
    const calls: string[] = []
    const original = globalThis.fetch
    globalThis.fetch = ((url: string) => {
      calls.push(url)
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(snapshot()),
      } as Response)
    }) as typeof fetch

    try {
      const result = await fetchSnapshot('TK-1')
      expect(calls).toEqual(['/api/tasks/TK-1'])
      expect(result.taskId).toBe('TK-1')
    } finally {
      globalThis.fetch = original
    }
  })
})
