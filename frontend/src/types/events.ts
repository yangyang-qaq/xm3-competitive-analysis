/**
 * SSE 事件契约。
 *
 * **字段级的唯一真相源是 `contracts/sse_events.json`**，后端与这里各有一条
 * 测试读它（`events.contract.test.ts` 就是这一侧的那条）。改字段名要两边
 * 一起改，否则至少一侧会红——而不是等到浏览器里少显示一块内容。
 *
 * 用**判别联合**而不是 `unknown` + 类型断言：store 里的 `switch` 会被穷尽
 * 检查，将来后端新增一种事件、或给现有事件加字段时，编译器会在消费点报错
 * 而不是静默丢字段。参考实现的对应写法走 `as` 断言，新字段被无声忽略。
 *
 * 形状：信封摊平，领域对象嵌套
 * ----------------------------
 * 信封的四个键（`seq`/`type`/`taskId`/`createdAt`）直接在最外层，
 * **不再套一层 `payload`**——套了的话 `payload` 的类型只能是 `unknown`，
 * 判别联合当场失效。
 *
 * 但携带领域对象的那六种事件把对象嵌在自己的键下面
 * （`thought`/`message`/`evidence`/`span`/`chart`/`image`）。两个理由，
 * 第二个是硬性的：
 *  1. 这些对象在报告里、在接口里都有自己的类型（`Thought`/`Evidence`/…）。
 *     同一样东西在事件里摊平、在别处嵌套，就会出现两份描述它的类型。
 *  2. **摊平有时根本做不到**。`TraceSpan` 的落库读取路径带 `taskId`，
 *     而 `taskId` 是信封保留键——后端 `publish()` 会直接抛错。
 *     摊平没有位置放它。
 *
 * 剩下五种是「事件自身的事实」，它们摊平：
 * `node_update` / `progress` / `report_ready` / `done` / `error`。
 *
 * 每个帧都带 `seq`（按任务递增）：
 *  - 前端靠它去重（断线重连后服务端会从 Last-Event-ID 补发）
 *  - 决策回放靠它排序
 */
import type {
  ChartSpec,
  Evidence,
  GalleryImage,
  MessageSummary,
  RunMetrics,
  StageId,
  StageState,
  Thought,
  TraceSpan,
} from './domain'

/**
 * 11 种事件类型。集合与后端 `EVENT_TYPES` 严格相等，有测试守着。
 *
 * 这里不声称顺序有任何含义：它曾经被注释成「分类顺序」，而那个顺序
 * 从来没有被谁读过。一个不描述任何行为的顺序，只会让人以为调整它
 * 会有什么后果。
 */
export const EVENT_TYPES = [
  'node_update',
  'progress',
  'thought',
  'message',
  'evidence',
  'trace',
  'chart',
  'image',
  'report_ready',
  'done',
  'error',
] as const

export type PipelineEventType = (typeof EVENT_TYPES)[number]

/** 信封。四个键由服务端 journal 统一填，载荷里出现它们会被直接拒收。 */
interface EventBase {
  /** 任务内单调递增，从 1 开始 */
  seq: number
  taskId: string
  /** ISO 8601 UTC，毫秒精度 */
  createdAt: string
}

// ============================================================
// 事件自身的事实：载荷摊平
// ============================================================

export interface NodeUpdateEvent extends EventBase {
  type: 'node_update'
  stage: StageId
  /** 阶段的中文名。DAG 节点的标题就是它 */
  label: string
  status: StageState
  /** **任务整体**进度 0–1，不是阶段内进度。只增不减 */
  progress: number
  /** 返工轮次，首次执行是 0 */
  round: number
  /** 阶段结束时才有 */
  elapsedMs?: number
  /** 阶段自己的产出摘要（章数、论点数、问题数…）。形状按阶段而异 */
  detail?: Record<string, unknown>
}

export interface ProgressEvent extends EventBase {
  type: 'progress'
  stage: StageId
  /** 0–1。**不叫 percent**：名为 percent 而值是 0–1 的字段，每个消费点都得先查一遍刻度 */
  progress: number
  message: string
  /** 累计值，不是增量 */
  tokens: number
  costUsd: number
}

export interface ReportReadyEvent extends EventBase {
  type: 'report_ready'
  reportId: string
  query: string
  subject: string
  sectionCount: number
  evidenceCount: number
  /** 出库校验发现的问题。非空时报告仍然落库了，但必须显示出来 */
  problems: string[]
  /** `"块：原因"`，降级必须可见 */
  degraded: string[]
}

export interface DoneEvent extends EventBase {
  type: 'done'
  reportId: string
  /** 整份指标。工作台在这一刻要渲染成本面板与四条铁律的数字，不必再拉一次 */
  metrics: RunMetrics
  degraded: string[]
  problems: string[]
}

export interface ErrorEvent extends EventBase {
  type: 'error'
  /** 出错时正在跑哪个阶段 */
  stage: StageId
  message: string
  /** 异常类名。用来区分「可重试的基础设施问题」与「代码 bug」 */
  kind: string
  /**
   * true = 等会儿可能自己会好（限流、连接抖动），false = 得有人去看
   * （密钥错了、代码 bug）。非 provider 异常一律 false。
   */
  retryable: boolean
}

// ============================================================
// 携带领域对象：嵌在自己的键下
// ============================================================

export interface ThoughtEvent extends EventBase {
  type: 'thought'
  thought: Thought
}

export interface MessageEvent extends EventBase {
  type: 'message'
  message: MessageSummary
}

export interface EvidenceEvent extends EventBase {
  type: 'evidence'
  evidence: Evidence
}

export interface TraceEvent extends EventBase {
  type: 'trace'
  /** 一次调用结束时推。开始时推的话，最有用的耗时与成本都还不存在 */
  span: TraceSpan
}

export interface ChartEvent extends EventBase {
  type: 'chart'
  chart: ChartSpec
}

export interface ImageEvent extends EventBase {
  type: 'image'
  image: GalleryImage
}

// ============================================================

export type PipelineEvent =
  | NodeUpdateEvent
  | ProgressEvent
  | ThoughtEvent
  | MessageEvent
  | EvidenceEvent
  | TraceEvent
  | ChartEvent
  | ImageEvent
  | ReportReadyEvent
  | DoneEvent
  | ErrorEvent

/**
 * 运行时校验：SSE 帧来自网络，不能假设它长对了。
 *
 * 只做**结构**校验（有没有 type/seq/taskId），不做字段级校验：
 * 帧里的载荷形状由 `contracts/sse_events.json` 的两侧测试保证，
 * 在每一次消息里再跑一遍深校验，等于把契约测试搬到运行时去，
 * 而它换不来任何契约测试没保证的东西。
 */
export function isPipelineEvent(value: unknown): value is PipelineEvent {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as { type?: unknown; seq?: unknown; taskId?: unknown }
  return (
    typeof candidate.type === 'string' &&
    (EVENT_TYPES as readonly string[]).includes(candidate.type) &&
    typeof candidate.seq === 'number' &&
    typeof candidate.taskId === 'string'
  )
}

// ============================================================
// 非事件流的管理接口形状
// ============================================================

/** 决策回放：把 span 归到「一次决策」里用。 */
export interface ReplayStep {
  seq: number
  span: TraceSpan
  stage: StageId
  headline: string
  citedEvidenceIds: string[]
}

/**
 * 会话创建响应。
 *
 * **不再在这里另写一份。** 这里曾经有一个 `CreateTaskResponse`，声明为
 * `{taskId, mode, needClarify, clarifyQuestions, scope}` —— 而后端实际返回的是
 * 整个任务快照再加上 `query`/`mode`：少了 `status`、`awaitingClarify`、
 * `lastSeq`、`nodes`、`subject`、`brands`，多了一个后端从来不返回的 `scope`。
 *
 * 最要命的是少了 `awaitingClarify`：澄清页要靠它决定跳不跳，而它不在类型里，
 * 于是那段代码只能去读 `needClarify` —— 一个**事实**被当成**状态**用，
 * 而一个早就跑完的任务 `needClarify` 也是真，于是每次打开都被拉回澄清页，
 * 那一页上没有任何东西可做。
 *
 * 现在直接指到 `domain.ts` 的同一个类型：这份响应本来就是一份快照，
 * 放在事件文件里只会长出第二个定义。
 */
export type { CreatedTask as CreateTaskResponse } from './domain'

// 这里曾经有一个 `TeamPreview`，声明为
// `{team: TeamPlan, experts: Expert[], issues: Issue[]}`。
//
// 它**从来没有被任何代码引用过**，后端也从来不发这个事件
// （`contracts/sse_events.json` 里没有它）。它唯一的作用是让
// `domain.ts` 里那个错误百出的 `Issue` 看起来"还在被用着"——
// 于是那个类型错了很久也没有人发现。两者一起删掉了。
//
// 专家队伍是通过 `node_update` 的载荷送过来的，不走这里。
