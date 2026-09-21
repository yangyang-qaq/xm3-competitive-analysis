/**
 * 工作台的任务状态。**事件进缓冲、一帧一次 `set()`。**
 *
 * 为什么不是每条事件一次 `set()`
 * ----------------------------
 * 见 `taskEvents.ts` 的文件头：那是参考实现最容易被感知到的性能问题
 * （每条事件复制整个数组 + 触发一次渲染）。这里把"合并"和"归约"
 * 两件事都从 zustand 里拿了出来，store 只剩连接生命周期。
 *
 * 连接生命周期与状态是两件事
 * ------------------------
 * `connection` 不进 `TaskViewState`：它描述的是**这条网线**，
 * 不是任务。混进去的话，`reduceEvents` 的纯函数签名就得带上它，
 * 而它和事件内容毫无关系。
 */
import { create } from 'zustand'

import { api } from '../lib/api'
import { openTaskStream, type TaskStream } from '../lib/sse'
import { isTerminal, type CreatedTask, type TaskSnapshot } from '../types/domain'
import type { PipelineEvent } from '../types/events'
import {
  applySnapshot,
  EMPTY_TASK_STATE,
  reduceEvents,
  type TaskViewState,
} from './taskEvents'

/** 这条网线现在是什么状态。与任务状态无关。 */
export type ConnectionState =
  | 'idle'
  /** 正在连（含浏览器自动重连的退避期） */
  | 'connecting'
  | 'live'
  /** 断了，但浏览器会自己回来 */
  | 'reconnecting'
  /**
   * **任务跑完了，流是我们主动关的。**
   *
   * 与 `fatal` 是两件相反的事：`fatal` 是"该收到的没收到"，
   * 这里是"该收的都收齐了"。与 `idle` 也不能合并——
   * 合成一个值，界面就没法区分"跑完了"和"从没连上"。
   */
  | 'ended'
  /** 不会回来了（任务不存在、或浏览器放弃） */
  | 'fatal'

/**
 * 这条连接已经有结论了，`disconnect()` 不该再把它抹成 `idle`。
 *
 * 抹掉的代价不是显示错了，是**丢信息**：`fatal` 会让用户以为一切正常，
 * 而 `ended` 会让用户以为流根本没接上。
 */
function isSettled(connection: ConnectionState): boolean {
  return connection === 'fatal' || connection === 'ended'
}

interface TaskStore {
  task: TaskViewState
  /** 收到过的最大 seq。**重连时拿它当续传水位** */
  lastSeq: number
  connection: ConnectionState
  /** 断线/致命错误的说明。连接正常时是空串 */
  connectionNote: string
  subject: string
  brands: string[]
  query: string

  hydrate: (snapshot: TaskSnapshot | CreatedTask) => void
  connect: (taskId: string, fromSeq?: number) => void
  disconnect: () => void
  reset: () => void
}

// ---- 批处理缓冲：模块级，不进 React 状态 ----
//
// 放模块级而不是 store 里，因为**它不该触发渲染**。放进 store 的话，
// 每来一条事件都会让订阅者重渲染一次——那正是要避免的事。
let pending: PipelineEvent[] = []
let scheduled = false
let stream: TaskStream | null = null
/** 已排上队的合帧回调。两个调度器都要能取消对方，否则会漏掉一次 flush。 */
let frameHandle: number | null = null
let flushTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 兜底合帧的间隔。
 *
 * **这个定时器不是 rAF 的降级替代，是它的补充。** 可见的标签页里永远是
 * rAF 先跑（一帧 16ms vs 这里 250ms），它只保证一件事：
 * **标签页切到后台时，事件照样落地。**
 *
 * 后台标签页里 `requestAnimationFrame` **根本不回调**——Chrome 是暂停它，
 * 不是让它变慢。只挂 rAF 的话，"用户切走"到"用户切回来"这段时间里
 * 事件一直在到、`pending` 一直在涨，而界面上一条都不出现——回来看到的
 * 是一个冻住的进度条配一个还在滚的网络面板。
 *
 * 实测过一次：任务已经在后端跑完（journal 里 520 条），页面上停在 **14%**，
 * 正好是切走那一刻快照里的进度。同一个页面上还有一个"重连中"——
 * 那是另一个缺陷（服务端发完历史就关流，见 `onDropped`）。
 */
export const FLUSH_FALLBACK_MS = 250

/**
 * 取消已排队的合帧。
 *
 * ⚠️ **`clearTimeout` / `cancelAnimationFrame` 这两行挡得住事情，但当前的
 * 测试套件证伪不了它们**（实测：把它们换成只清标志、甚至和下面 `run` 里的
 * `if (!scheduled) return` 一起删掉，都仍然全绿）。还能绿是因为 `run()`
 * 里那句守卫已经让迟到的回调变成空操作，而 `flush()` 开头那句
 * `pending.length === 0` 又让空批不产生 `set()`——两层兜底叠在一起，
 * 观察不到差别。
 *
 * 那为什么还留着：它们防的是**迟到的回调把新一批事件提前 flush 掉**
 * （一个窗口被劈成两次 `set()`），而这种交错要靠运气才撞上，
 * 用例里造不出来。删掉它们只是把"靠两层兜底"变成"靠一层兜底"。
 * 记在这里，是为了下一次有人读到这两行时知道：
 * **它们不是被测试保护的**，改它们不会有任何东西报警。
 */
function clearScheduled(): void {
  if (frameHandle !== null) {
    if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frameHandle)
    frameHandle = null
  }
  if (flushTimer !== null) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  scheduled = false
}

function scheduleFlush(): void {
  if (scheduled) return
  scheduled = true
  const run = () => {
    // 两个调度器谁先到都算数，后到的那个就此作废。
    // ⚠️ 这一行同样**证伪不了**，理由见 `clearScheduled` 上面那段。
    if (!scheduled) return
    clearScheduled()
    flush()
  }
  if (typeof requestAnimationFrame === 'function') frameHandle = requestAnimationFrame(run)
  // **这一行是必须的，不是保险丝。** 理由见上面 `FLUSH_FALLBACK_MS`。
  flushTimer = setTimeout(run, FLUSH_FALLBACK_MS)
}

/**
 * 主动、且**正常**地结束这条流。
 *
 * 与 `disconnect()` 只差一件事：`connection` 记成 `ended` 而不是 `idle`。
 */
function endStream(note: string): void {
  if (!stream) return
  stream.close()
  stream = null
  pending = []
  clearScheduled()
  useTaskStore.setState({ connection: 'ended', connectionNote: note })
}

/**
 * "流断了"这一刻：**先落地，再判断这次断开是不是预期内的。**
 *
 * 返回 `true` 表示任务已经到终态、这条流本来就不该再有了（顺手关掉）。
 *
 * 为什么要先 `flush`：服务端把历史推完就立刻关流，而"关流"这一幕发生的
 * 那一刻 rAF 很可能还没跑过——最后那批事件（含 `done`）还在 `pending` 里。
 * 不 flush 就判断的话看到的状态是 `running`，于是一个**已经跑完**的任务
 * 被判成"掉线"，交给浏览器去重连，服务端每次再关一遍——永远循环。
 * 实测就是那 149 次请求。
 *
 * 为什么两个分支都要问它：`EventSource` 把"服务端正常关流"归到
 * `CONNECTING`（会重连）还是 `CLOSED`（不会重连）**不在我们的控制里**。
 * 只在一处判断的话，修复成不成全看浏览器当天怎么归类。
 */
function settleIfTerminal(note: string): boolean {
  flush()
  if (!isTerminal(useTaskStore.getState().task.status)) return false
  endStream(note)
  return true
}

function flush(): void {
  if (pending.length === 0) return
  const batch = pending
  pending = []

  const store = useTaskStore
  const { task, lastSeq } = store.getState()
  const result = reduceEvents(task, batch, lastSeq)
  if (result.state !== task || result.lastSeq !== lastSeq) {
    store.setState({ task: result.state, lastSeq: result.lastSeq })
  }

  // **这批里有终态事件，说明服务端马上就要把这条流关掉。**
  // 不等它关，主动断——因为"服务端正常关流"在 `EventSource` 眼里
  // 和"网络掉线"长得一模一样，它会一直重连下去（实测 149 次，
  // 每 4.8 秒一次，全部打在同一个 URL 上）。
  //
  // 为什么敢断定 `done` / `error` 之后不会再有事件：全后端
  // `journal.publish` 只有 `core/pipeline/context.py` 一处入口，
  // 而 `core/report/refine.py`（"深化本节"）一个事件都不发——
  // 它改的是报告，不往这条流里推东西。
  if (batch.some((event) => event.type === 'done' || event.type === 'error')) {
    endStream('任务已结束')
  }
}

export const useTaskStore = create<TaskStore>((set, get) => ({
  task: { ...EMPTY_TASK_STATE },
  lastSeq: 0,
  connection: 'idle',
  connectionNote: '',
  subject: '',
  brands: [],
  query: '',

  hydrate: (snapshot) =>
    set((state) => ({
      task: applySnapshot(state.task, {
        status: snapshot.status,
        stage: snapshot.stage,
        stageLabel: snapshot.stageLabel,
        progress: snapshot.progress,
        nodes: snapshot.nodes,
        needClarify: snapshot.needClarify,
        awaitingClarify: snapshot.awaitingClarify,
        // **这个字段漏过一次。** 快照里有它，而 `hydrate` 的映射里没写，
        // 于是澄清页上一个问题都没有——那看起来像"后端没生成问题"，
        // 而不是像一处漏写。凡是快照里澄清页要用的字段，
        // 都要显式列在这里，没有别的机制会提醒。
        clarifyQuestions: snapshot.clarifyQuestions,
        reportId: snapshot.reportId,
        error: snapshot.error,
      }),
      // 水位只增不减：快照里的 lastSeq 是服务端的，而手上可能有
      // 通过 SSE 收到的更高的那些（快照是更早发的请求）。
      lastSeq: Math.max(state.lastSeq, snapshot.lastSeq ?? 0),
      subject: snapshot.subject || state.subject,
      brands: snapshot.brands?.length ? [...snapshot.brands] : state.brands,
      query: 'query' in snapshot ? String(snapshot.query) : state.query,
    })),

  connect: (taskId, fromSeq) => {
    get().disconnect()

    // **水位取 `max(fromSeq, lastSeq)`。** 只传 `fromSeq` 的话，
    // 一个"先 hydrate 过、又从别处重连"的页面会把水位退回快照那一刻，
    // 于是重连时重放一大段已经看过的事件——去重能挡住重复显示，
    // 但那是白传几百帧。
    const resumeFrom = Math.max(fromSeq ?? 0, get().lastSeq)

    set({ connection: 'connecting', connectionNote: '' })
    stream = openTaskStream(
      taskId,
      {
        onEvent: (event) => {
          pending.push(event)
          scheduleFlush()
        },
        onOpen: () => set({ connection: 'live', connectionNote: '' }),
        onDropped: (watermark) => {
          // 终态是从**快照**来的时候走这条：历史已经收完了，
          // 服务端没有新东西可发，所以它关流是正常的，不是掉线。
          if (settleIfTerminal('任务已结束，连接正常关闭')) return

          set({
            connection: 'reconnecting',
            // 断线时把水位记下来。浏览器重连会带 `Last-Event-ID`，
            // 但那是它的水位；这里记一份是为了让"手动重连"也能接上。
            lastSeq: Math.max(get().lastSeq, watermark),
            connectionNote: `连接中断，正在重连（已收到 ${watermark} 条事件）`,
          })
        },
        onFatal: (reason) => {
          // 这条分支不产生重连（`readyState` 已经是 CLOSED），所以它不是
          // 那 149 次请求的来源。这里问一句是为了**别说错话**：
          // 一个跑完的任务收到"连接已断开"，会让人去查网络。
          if (settleIfTerminal('任务已结束，连接正常关闭')) return

          set({
            connection: 'fatal',
            connectionNote: reason,
            // 流不会再来了，但**已经收到的东西要留住**：
            // 把 task 清掉的话，一次网络故障会让整个工作台变白。
          })
        },
      },
      { fromSeq: resumeFrom },
    )
  },

  disconnect: () => {
    stream?.close()
    stream = null
    // 缓冲里可能还有没落地的帧。丢掉它们而不是 flush：
    // 断开之后到达的帧属于上一次连接，而下一次连接会按水位重新补发。
    pending = []
    clearScheduled()
    set((state) => ({
      connection: isSettled(state.connection) ? state.connection : 'idle',
    }))
  },

  reset: () => {
    stream?.close()
    stream = null
    // 回到初始状态要把**模块级的那几样**一起清掉——它们不在 zustand 里：
    // `stream` 是这条网线，`pending` 是还没落地的帧，
    // 两个句柄是"已经排了一帧"。
    //
    // `pending = []` 和 `clearScheduled()` 都**不是在修一个线上 bug**：
    // 不写它们，那个已经排好的回调照样会跑，而 `flush()` 第一件事就是
    // 把它抹掉，所以状态自己会恢复。写它们是因为 `reset()` 的契约就是
    // "回到初始状态"，留着一个排队中的回调意味着函数名和它做的事对不上。
    // 顺带一提：`clearScheduled()` 里那两行取消也**证伪不了**，
    // 见它自己上面那段——这句"证伪不了"现在写两处，是因为两处都容易被
    // 下一次改动顺手删掉，而删掉不会有任何东西报警。
    //
    // 这段注释原来写的是：这一步从"卫生"变成"必需"，只差"给这里加一句
    // `cancelAnimationFrame`"——那一刻起没有任何东西会再把标志清掉，
    // 而它的唯一作用是让 `scheduleFlush()` 提前返回，于是此后一条事件
    // 都不渲染、页面空白、日志干净。
    //
    // **那句"差一句 cancelAnimationFrame"已经发生了**，只是形式变了一下：
    // 合帧现在同时挂在 rAF 和一个 250ms 兜底定时器上（见 `FLUSH_FALLBACK_MS`），
    // 两个都要被 `clearScheduled()` 收掉。预言成立的方式和预想的一样安静，
    // 所以这里留个记号。
    pending = []
    clearScheduled()
    set({
      task: { ...EMPTY_TASK_STATE },
      lastSeq: 0,
      connection: 'idle',
      connectionNote: '',
      subject: '',
      brands: [],
      query: '',
    })
  },
}))

// ---- REST 入口 ----

/**
 * 取一次快照。
 *
 * **刻意不在这里调 `connect`**：快照与事件流是两件事，
 * 而把两件事塞进一个 `load()` 里，会让人以为"加载完了"= "连接建立好了"。
 * 页面负责按顺序调它们。
 */
export async function fetchSnapshot(taskId: string): Promise<TaskSnapshot> {
  return api.getTask(taskId)
}
