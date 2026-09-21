/**
 * `useTaskStream` 的契约：**先快照、后连流、走时断开。**
 *
 * 为什么这三件事的顺序值得单写一个文件
 * ----------------------------------
 * 阶段 6 的验收里有一条是"跑到一半刷新页面，工作台从 journal 恢复"。
 * 那一句话的实现就是这里的顺序：快照带着服务端的水位（`lastSeq`），
 * `connect` 拿它算出 `?from_seq=`。任何一步换位置，症状都不是报错——
 * 是刷新之后重传几百帧（顺序换了）或者干脆一条都不补（水位丢了）。
 *
 * 这里刻意**不 mock `fetchSnapshot`**，而是把 `fetch` 桩在底下：
 * 于是请求真的走了 `api.ts` 那条路径，URL 和错误分支都是真的。
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { FakeEventSource } from '../test/fakeEventSource'
import { useTaskStore } from '../store/taskStore'
import type { TaskSnapshot } from '../types/domain'
import type { PipelineEvent } from '../types/events'
import { useTaskStream, type TaskLoad } from './useTaskStream'

// ---- fetch 桩：请求不自己完成，由用例决定什么时候回、回什么 ----

interface PendingRequest {
  url: string
  /** 回一个任意响应。测 404 这类分支要靠它——`ok()` 只发 200。 */
  respond: (response: Response) => void
  /** 便捷：200 + JSON */
  ok: (body: unknown) => void
  /** 网络层就失败了（连不上、超时），没有响应可言 */
  fail: (err: unknown) => void
}

let pending: PendingRequest[] = []
let restoreFetch: () => void

/**
 * 只做到 `request()` 真正会用到的那几个成员：`ok` / `status` / `statusText` / `json()`。
 *
 * 不用全局的 `Response`，是因为 jsdom 不实现 fetch，那个构造器在不在
 * 取决于 node 版本——而它在不在跟这里要测的东西毫无关系。
 */
function okResponse(body: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: async () => body } as unknown as Response
}

function errorResponse(status: number, detail: string) {
  return {
    ok: false,
    status,
    statusText: 'Error',
    json: async () => ({ detail }),
  } as unknown as Response
}

function installFetch(): void {
  const original = globalThis.fetch
  pending = []
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const url = String(input)
    return new Promise<Response>((resolve, reject) => {
      pending.push({
        url,
        respond: resolve,
        ok: (body) => resolve(okResponse(body)),
        fail: reject,
      })
    })
  }) as typeof fetch
  restoreFetch = () => {
    globalThis.fetch = original
  }
}

/** 第 `index` 个请求（0 起）。effect 里发请求是同步的，但等一下更稳。 */
async function requestAt(index: number): Promise<PendingRequest> {
  await waitFor(() => expect(pending.length).toBeGreaterThan(index))
  const request = pending[index]
  if (!request) throw new Error(`第 ${index} 个请求还没有发出来`)
  return request
}

// ---- 记录"流是什么时候开的、开的时候 store 里有什么" ----

const openedAt: { url: string; subject: string; lastSeq: number }[] = []

class RecordingSource extends FakeEventSource {
  constructor(url: string) {
    super(url)
    // 构造 `EventSource` 那一瞬间的 store 状态。**这是本文件最重要的一次采样**：
    // "先 hydrate 再 connect"这条顺序没有别的办法从外面看见。
    const state = useTaskStore.getState()
    openedAt.push({ url, subject: state.subject, lastSeq: state.lastSeq })
  }
}

let restoreEventSource: () => void

function installEventSource(): void {
  const original = (globalThis as { EventSource?: unknown }).EventSource
  FakeEventSource.reset()
  openedAt.length = 0
  ;(globalThis as { EventSource?: unknown }).EventSource = RecordingSource
  restoreEventSource = () => {
    ;(globalThis as { EventSource?: unknown }).EventSource = original
    FakeEventSource.reset()
  }
}

// ---- rAF 手动挡：事件合帧由 store 负责，这里要的是确定 ----

let frames: (() => void)[] = []
let restoreRaf: () => void

function flushFrame(): void {
  const queued = frames
  frames = []
  for (const run of queued) run()
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
    lastSeq: 42,
    needClarify: false,
    awaitingClarify: false,
    clarifyQuestions: [],
    subject: '笔记软件',
    brands: ['Notion'],
    ...overrides,
  }
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
  installFetch()
  installEventSource()
})

afterEach(() => {
  useTaskStore.getState().reset()
  restoreRaf()
  restoreEventSource()
  restoreFetch()
})

describe('顺序：先快照、后连流', () => {
  it('快照回来之前，一个 EventSource 都不建', async () => {
    renderHook(() => useTaskStream('TK-1'))

    // 这条挡的是"把 connect 挪到 fetch 前面"这类改动。那时候流已经接了，
    // 而 store 里空空如也——历史事件会灌进一个还没 hydrate 的状态。
    expect(FakeEventSource.instances).toHaveLength(0)

    await act(async () => {
      ;(await requestAt(0)).ok(snapshot())
    })
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('流打开的那一刻，store 里已经有这个任务的快照了', async () => {
    renderHook(() => useTaskStream('TK-1'))
    await act(async () => {
      ;(await requestAt(0)).ok(snapshot())
    })

    // 采到的是 `new EventSource(...)` 那一瞬间的状态。
    // 顺序反了的话这里是 subject='' / lastSeq=0——而界面照样能渲染出来，
    // 只是刷新的那一刻水位从 0 开始，服务端重发全部历史。
    expect(openedAt).toHaveLength(1)
    expect(openedAt[0]?.subject).toBe('笔记软件')
    expect(openedAt[0]?.lastSeq).toBe(42)
  })

  it('续传水位来自快照——这就是"刷新页面从 journal 恢复"', async () => {
    renderHook(() => useTaskStream('TK-1'))
    await act(async () => {
      ;(await requestAt(0)).ok(snapshot({ lastSeq: 137 }))
    })

    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-1/stream?from_seq=137')
  })

  it('水位是 0 的新任务，URL 上不带查询参数', async () => {
    renderHook(() => useTaskStream('TK-1'))
    await act(async () => {
      ;(await requestAt(0)).ok(snapshot({ lastSeq: 0 }))
    })

    // 带上 `?from_seq=0` 也不算错（后端会夹到 0，效果一样），
    // 但这个 URL 是排查"为什么重传了全部历史"时第一个要看的东西，
    // 让它对新任务保持干净。
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-1/stream')
  })

  it('快照一到手，加载状态就是 ready', async () => {
    const { result } = renderHook(() => useTaskStream('TK-1'))
    expect(result.current.load).toBe('loading')

    await act(async () => {
      ;(await requestAt(0)).ok(snapshot())
    })
    expect(result.current.load).toBe('ready')
  })
})

describe('切任务：不能渲染上一个任务的残留', () => {
  it('换了 taskId 之后，没有任何一次渲染拿新 id 配旧的 ready', async () => {
    const seen: { taskId: string; load: TaskLoad }[] = []
    const { rerender } = renderHook(
      (props: { taskId: string }) => {
        const handle = useTaskStream(props.taskId)
        // **逐次渲染地记**，而不是只看最终值。要抓的东西只存在于
        // "taskId 已经换了、而 effect 还没跑"的那一帧里——effect 在渲染
        // 之后才跑，所以那一帧一定被渲染出来了。
        seen.push({ taskId: props.taskId, load: handle.load })
        return handle
      },
      { initialProps: { taskId: 'TK-1' } },
    )

    await act(async () => {
      ;(await requestAt(0)).ok(snapshot())
    })

    // 切到 TK-2，**它的快照还没回来**。
    rerender({ taskId: 'TK-2' })

    // 朴素写法（把 load 直接放进 state、在 effect 里置 'loading'）在这里
    // 会留下 { taskId: 'TK-2', load: 'ready' } 这一条：页面于是用 TK-1 的
    // DAG、思维流和进度，顶着 TK-2 的标题渲染一帧。
    expect(seen.filter((item) => item.taskId === 'TK-2' && item.load === 'ready')).toEqual([])
    expect(seen).toContainEqual({ taskId: 'TK-2', load: 'loading' })
  })

  it('切走之后旧任务的快照姗姗来迟，不会把新任务盖掉', async () => {
    const { rerender, result } = renderHook(
      (props: { taskId: string }) => useTaskStream(props.taskId),
      { initialProps: { taskId: 'TK-1' } },
    )
    const stale = await requestAt(0)

    rerender({ taskId: 'TK-2' })
    // 新任务**先**回来。
    await act(async () => {
      ;(await requestAt(1)).ok(snapshot({ taskId: 'TK-2', subject: '新的', lastSeq: 5 }))
    })
    expect(result.current.load).toBe('ready')
    expect(useTaskStore.getState().subject).toBe('新的')

    // 然后 TK-1 那条慢响应才到。**网络快慢不该决定工作台显示哪个任务。**
    // 顺序很要紧：先回旧的那条，后面的新响应自然会把状态盖回来，
    // 于是缺了 `cancelled` 守卫也看不出来——这正是这条用例第一版
    // 漏掉的地方。要让旧的**最后**到。
    await act(async () => {
      stale.ok(snapshot({ taskId: 'TK-1', subject: '旧的', lastSeq: 999 }))
    })

    expect(useTaskStore.getState().subject).toBe('新的')
    // 不只是"内容没被盖掉"：少了守卫时这里还会**为 TK-1 开一条新流**，
    // 于是两条流并存，都往同一个 store 里灌。
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-2/stream?from_seq=5')
    expect(result.current.load).toBe('ready')
  })

  it('切到另一个任务时，水位不继承上一个任务的', async () => {
    const { rerender } = renderHook(
      (props: { taskId: string }) => useTaskStream(props.taskId),
      { initialProps: { taskId: 'TK-1' } },
    )
    await act(async () => {
      ;(await requestAt(0)).ok(snapshot({ lastSeq: 500 }))
    })
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-1/stream?from_seq=500')

    rerender({ taskId: 'TK-2' })
    await act(async () => {
      ;(await requestAt(1)).ok(snapshot({ taskId: 'TK-2', lastSeq: 5 }))
    })

    // store 是模块级的，它只装得下一个任务。少了那句 `reset()`，
    // 从 TK-1 切到 TK-2 时会带着 TK-1 的水位去连——服务端于是认为
    // "你已经看过前 500 条"，TK-2 的历史一条都不补。
    // 表现是工作台全空，而连接 200、日志干净、没有任何地方报错。
    expect(FakeEventSource.last.url).toBe('/api/tasks/TK-2/stream?from_seq=5')
  })

  it('切任务时上一条流会被关掉', async () => {
    const { rerender } = renderHook(
      (props: { taskId: string }) => useTaskStream(props.taskId),
      { initialProps: { taskId: 'TK-1' } },
    )
    await act(async () => {
      ;(await requestAt(0)).ok(snapshot())
    })
    const firstStream = FakeEventSource.last

    rerender({ taskId: 'TK-2' })
    // 不关的话，两条流并存：TK-1 的事件继续灌进同一个 store，
    // 而 store 是模块级的、只装得下一个任务。
    expect(firstStream.closed).toBe(true)
  })
})

describe('加载失败：三种说法不能混成一种', () => {
  it('404 是 notFound，不是 error', async () => {
    const { result } = renderHook(() => useTaskStream('TK-404'))
    const request = await requestAt(0)
    await act(async () => {
      // 用真的 `ApiError`，且从 `api.ts` 拿——一个手搓的
      // `{name: 'ApiError', status: 404}` 不是它的实例，`instanceof`
      // 会走 error 分支，于是这条用例测的是"我以为的类型判断"。
      const { ApiError } = await import('../lib/api')
      request.fail(new ApiError('任务不存在', 404))
    })

    expect(result.current.load).toBe('notFound')
    // 一个被清掉的任务 id 显示"网络错误"会让人去查网络，所以这条单独分出来。
    expect(result.current.error).toBe('')
  })

  it('走 api.ts 的真实路径时，404 也落到 notFound', async () => {
    // 上一条直接把 ApiError 塞进 Promise，跳过了 `request()`。
    // 这一条让 `fetch` 真的返回 404，验证 `request()` 确实会把它
    // 变成带 status 的 ApiError——两段接不上时，上一条照样是绿的。
    const { result } = renderHook(() => useTaskStream('TK-1'))
    const request = await requestAt(0)
    await act(async () => {
      request.respond(errorResponse(404, '任务不存在'))
    })
    expect(result.current.load).toBe('notFound')
  })

  it('连不上后端：error，且把原因带出来', async () => {
    const { result } = renderHook(() => useTaskStream('TK-1'))
    await act(async () => {
      ;(await requestAt(0)).fail(new TypeError('Failed to fetch'))
    })

    expect(result.current.load).toBe('error')
    expect(result.current.error).toBe('Failed to fetch')
  })

  it('空的 taskId 直接判 notFound，且一个请求都不发', async () => {
    const { result } = renderHook(() => useTaskStream(''))

    expect(result.current.load).toBe('notFound')
    expect(pending).toHaveLength(0)
    expect(FakeEventSource.instances).toHaveLength(0)
  })
})

describe('卸载与重试', () => {
  it('卸载时断开流', async () => {
    const { unmount } = renderHook(() => useTaskStream('TK-1'))
    await act(async () => {
      ;(await requestAt(0)).ok(snapshot())
    })
    const stream = FakeEventSource.last

    unmount()
    // 不断开的话，离开工作台之后事件还在往 store 里灌——
    // 而 store 是模块级的，下一次进别的任务会先看到一段别人的思维流。
    expect(stream.closed).toBe(true)
  })

  it('重试：立刻回到"加载中"，并重新发一次请求', async () => {
    const { result } = renderHook(() => useTaskStream('TK-1'))
    await act(async () => {
      ;(await requestAt(0)).fail(new TypeError('Failed to fetch'))
    })
    expect(result.current.load).toBe('error')

    act(() => {
      result.current.reload()
    })
    // **状态由那次点击来改。** 等 effect 起来再改的话，用户点完"重试"
    // 到 effect 跑之间还看得见上一次的错误信息。
    expect(result.current.load).toBe('loading')
    expect(result.current.error).toBe('')

    await act(async () => {
      ;(await requestAt(1)).ok(snapshot())
    })
    expect(result.current.load).toBe('ready')
  })
})

describe('事件流真的接上了', () => {
  it('流上的具名帧会落进 store', async () => {
    const { result } = renderHook(() => useTaskStream('TK-1'))
    await act(async () => {
      ;(await requestAt(0)).ok(snapshot())
    })

    const source = FakeEventSource.last
    act(() => {
      source.open()
    })
    expect(result.current.connection).toBe('live')

    act(() => {
      source.emit('thought', thoughtEvent(43))
      flushFrame()
    })

    expect(useTaskStore.getState().task.thoughts.map((item) => item.id)).toEqual(['TH-43'])
  })
})

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
