/**
 * 事件 → 状态。**纯函数，不碰 zustand、不碰 DOM。**
 *
 * 单独放一个文件是为了让"seq 去重"和"环形上限"这两件事能被直接测：
 * 它们都在 zustand 的 `set()` 里面的话，测一次要建一个 store、
 * 模拟一堆事件、再断言 —— 而真正想测的只有"第 5 条被丢掉了"。
 *
 * 为什么不是每个事件一次 `set()`
 * ----------------------------
 * 参考实现的写法是 `set({ thoughts: [...s.thoughts, d] })`。每次事件都把
 * 整个数组复制一遍，于是 800 条思维流进来是 O(n²) 次元素拷贝，
 * 而且每来一条事件就重渲染一次工作台。实测在几百条的时候肉眼可见地卡。
 *
 * 这里改成：事件先进一个模块级缓冲，用 rAF 合并成一帧一次 `set()`。
 * 一次渲染处理一批事件，而不是一次事件一次渲染。
 */

import type {
  ChartSpec,
  ClarifyQuestion,
  Evidence,
  GalleryImage,
  MessageSummary,
  RunMetrics,
  StageId,
  StageState,
  TaskStatus,
  Thought,
  TraceSpan,
} from '../types/domain'
import type { PipelineEvent } from '../types/events'

/** 思维流的保留条数。**只保最近这些**：一场调研能产出上千条，
 *  而工作台一屏只看得到十几条——全留着是内存和渲染的双重负担。 */
export const THOUGHT_CAP = 800

/** span 的保留条数。比思维流高：trace 面板要按 kind 汇总成本，
 *  砍掉一半会让数字对不上，而它恰恰是要被信任的那个数字。 */
export const SPAN_CAP = 1200

/** 保留最近的 N 条，**丢弃最旧的**。 */
function capped<T>(list: T[], cap: number): T[] {
  return list.length > cap ? list.slice(list.length - cap) : list
}

export interface TaskViewState {
  status: TaskStatus
  stage: StageId | ''
  stageLabel: string
  progress: number
  /** 每个阶段的状态。DAG 节点直接读它 */
  nodes: Partial<Record<StageId, StageState>>
  /** 返工轮次 */
  round: number

  /** **事实**：这个需求信息不够，生成过问题 */
  needClarify: boolean
  /** **状态**：现在正停着等回答。与上一个不是一回事，别合并 */
  awaitingClarify: boolean
  /**
   * 要问用户的那几个问题。澄清页的全部内容就是它。
   *
   * **只能从快照来。** 问题是在需求理解那一步生成的，那发生在工作台
   * 被打开之前，所以事件流里没有它——`node_update` 只说"intake 跑完了"。
   * 这也意味着它必须**显式**列在 `applySnapshot` 里：漏一个字段的表现是
   * 澄清页上一个问题都没有，而那看起来像"后端没生成问题"。
   */
  clarifyQuestions: ClarifyQuestion[]

  thoughts: Thought[]
  messages: MessageSummary[]
  evidences: Evidence[]
  charts: ChartSpec[]
  images: GalleryImage[]
  spans: TraceSpan[]

  reportId: string
  metrics: RunMetrics | null
  problems: string[]
  degraded: string[]
  error: string
}

export const EMPTY_TASK_STATE: TaskViewState = {
  status: 'pending',
  stage: '',
  stageLabel: '',
  progress: 0,
  nodes: {},
  round: 0,
  needClarify: false,
  awaitingClarify: false,
  clarifyQuestions: [],
  thoughts: [],
  messages: [],
  evidences: [],
  charts: [],
  images: [],
  spans: [],
  reportId: '',
  metrics: null,
  problems: [],
  degraded: [],
  error: '',
}

/**
 * 状态集合从快照来（`GET /api/tasks/{id}`）。
 *
 * 恢复快照与事件流是**两个方向**的信息：快照告诉你此刻是什么样，
 * 事件流告诉你它是怎么变成这样的。要在中途打开工作台，两者都要：
 * 没有快照，DAG 上的节点状态要等下一次 `node_update` 才出现；
 * 没有事件流，之前发生过的思维一条都看不到。
 */
export function applySnapshot(
  state: TaskViewState,
  snapshot: Partial<TaskViewState> & { lastSeq?: number },
): TaskViewState {
  return {
    ...state,
    // `?? state.x` 而不是直接赋值：兜底分支（服务重启后）的 `nodes` 是 `{}`，
    // 直接覆盖会把从事件流重建出来的节点状态抹成空——而工作台恰好
    // 是在刷新之后才需要它。
    status: snapshot.status ?? state.status,
    stage: snapshot.stage ?? state.stage,
    stageLabel: snapshot.stageLabel || state.stageLabel,
    progress: typeof snapshot.progress === 'number' ? snapshot.progress : state.progress,
    nodes:
      snapshot.nodes && Object.keys(snapshot.nodes).length > 0
        ? snapshot.nodes
        : state.nodes,
    needClarify: snapshot.needClarify ?? state.needClarify,
    awaitingClarify: snapshot.awaitingClarify ?? state.awaitingClarify,
    // 空数组不覆盖：兜底分支（服务重启后）理论上也能给出问题，
    // 但"给不出"与"问过但一个都没留下"在读到的形状上是一样的。
    // 后者覆盖前者会让澄清页在重连之后变空，而任务仍然停在那儿等回答。
    clarifyQuestions:
      snapshot.clarifyQuestions && snapshot.clarifyQuestions.length > 0
        ? [...snapshot.clarifyQuestions]
        : state.clarifyQuestions,
    reportId: snapshot.reportId || state.reportId,
    error: snapshot.error || state.error,
  }
}

/** 一条事件推进状态。返回同一个对象（就地改）——它只在批处理的草稿上跑。 */
function applyOne(state: TaskViewState, event: PipelineEvent): void {
  switch (event.type) {
    case 'node_update':
      state.stage = event.stage
      state.stageLabel = event.label
      state.progress = event.progress
      state.round = event.round
      state.nodes = { ...state.nodes, [event.stage]: event.status }
      // 节点状态与任务状态是两套东西，但**终态要能对上**：
      // 一个 `done` 的节点不该让任务看起来还在跑。
      if (event.stage === 'done') state.status = 'done'
      else if (state.status === 'pending') state.status = 'running'
      return

    case 'progress':
      state.stage = event.stage
      state.progress = event.progress
      return

    case 'thought':
      state.thoughts = capped([...state.thoughts, event.thought], THOUGHT_CAP)
      return

    case 'message':
      state.messages = [...state.messages, event.message]
      return

    case 'evidence':
      state.evidences = [...state.evidences, event.evidence]
      return

    case 'trace':
      state.spans = capped([...state.spans, event.span], SPAN_CAP)
      return

    case 'chart':
      // 图表按 id 去重：返工之后同一个图表会被重新生成一次，
      // 直接追加会让图集里出现两张一样的图。
      state.charts = [...state.charts.filter((c) => c.chartId !== event.chart.chartId),
        event.chart]
      return

    case 'image': {
      const url = event.image.url
      state.images = state.images.some((i) => i.url === url)
        ? state.images
        : [...state.images, event.image]
      return
    }

    case 'report_ready':
      state.reportId = event.reportId
      state.problems = [...event.problems]
      state.degraded = [...event.degraded]
      return

    case 'done':
      state.status = 'done'
      state.progress = 1
      state.reportId = event.reportId || state.reportId
      state.metrics = event.metrics
      state.problems = [...event.problems]
      state.degraded = [...event.degraded]
      return

    case 'error':
      state.status = 'failed'
      state.error = event.message
      // 降级与失败不是一回事，但"哪里没做成"要能被看见：
      // 一条 error 事件也进 degraded，免得失败的任务在降级面板上是空的。
      state.degraded = [...state.degraded, `error：${event.message}`]
      return

    default:
      // 判别联合会把这里变成 `never`：将来后端加一种事件类型、
      // 而 EVENTS 里补了、这里忘了补，**编译不过**。
      // 参考实现走 `as` 断言，新事件被静默忽略。
      return assertNever(event)
  }
}

function assertNever(value: never): never {
  throw new Error(`未处理的事件类型：${JSON.stringify(value)}`)
}

/**
 * 把一批事件合并进状态。**`lastSeq` 之前的事件直接丢弃。**
 *
 * 去重是断线续传的必要条件，不是优化：服务端按水位补发，
 * 但补发点与页面手上那一条之间可能重叠（水位是"我收到的最后一条"，
 * 而重连请求可能在它之后又收到了几条）。没有去重，
 * 那一段思维会在界面上出现两遍——而用户分不出是重复还是真的发生了两次。
 *
 * 去重因此作用于**每一个事件**，而不只是"批的开头那一下"：同一批里出现
 * 两个相同的 seq 是续传的正常形状，不是异常输入。见循环里那行 `continue`。
 */
export function reduceEvents(
  state: TaskViewState,
  events: readonly PipelineEvent[],
  lastSeq: number,
): { state: TaskViewState; lastSeq: number } {
  let watermark = lastSeq
  // 先按 seq 排序。**不排的话，乱序到达的那几条会被下面那行去重守卫
  // 当成"已经看过"直接丢掉**——水位只比较 seq 的大小，它分不出
  // 「这一条是乱序来的」和「这一条我已经收过了」。
  //
  // 丢的是**已经发生过的事实**：水位同时被推到了最大值，所以之后每一次
  // 重连都会从那个最大值之后补发，那几条思维再也回不来了。
  // 表现是思维流中间少几条、进度停在半路，而日志干净。
  //
  // 正常路径上服务端是顺序推的，但续传的补发段与实时段接在一起时
  // 那一段的顺序不保证——所以排序不是"更整齐"，是**不丢数据**。
  const fresh = events
    .filter((event) => event.seq > watermark)
    .sort((a, b) => a.seq - b.seq)

  if (fresh.length === 0) return { state, lastSeq: watermark }

  const next: TaskViewState = { ...state }
  for (const event of fresh) {
    // **这一行必须在循环里，不能只靠上面那次 filter。**
    // filter 用的是**批处理开始时**的水位，所以同一个 seq 在一批里出现两次时
    // 两条都会通过。而这恰恰是续传会产生的形状：补发段与实时段接在一起，
    // 同一帧既在 journal 的补发里、又在订阅的推送里。
    //
    // 少了这一行，那条思维在界面上出现两遍；更坏的是 `lastSeq` 已经推到位了，
    // 于是之后每一次重连都不会把它纠正过来——它看起来就像真的发生了两次。
    if (event.seq <= watermark) continue
    // 排过序，所以这里单调递增，不需要 `Math.max`。
    watermark = event.seq
    applyOne(next, event)
  }
  return { state: next, lastSeq: watermark }
}
