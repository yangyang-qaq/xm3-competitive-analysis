/**
 * 领域模型的唯一真相源。
 *
 * 约定：这里用 camelCase。后端 pydantic 模型通过别名生成器序列化成 camelCase
 * （Python 内部仍是 snake_case），这样两侧各用各的惯用写法，不用互相迁就。
 * 字段名两边必须严格对齐——`tests/contract/` 下有测试盯着这件事。
 */

// ============================================================
// 枚举（用联合类型而非 TS enum：erasableSyntaxOnly 下更干净，
// 且序列化后就是普通字符串，没有运行时代码）
// ============================================================

export type ResearchMode = 'quick' | 'deep' | 'expert'

/** 论点置信度。unverified = 没有任何证据支撑，仍然保留但必须显式标注（铁律 1）。 */
export type Confidence = 'high' | 'medium' | 'low' | 'unverified'

export type ExpertLevel = 'L3' | 'L2' | 'L1'
export type ExpertGroup = 'decision' | 'strategy' | 'industry' | 'function'

export type SourceType =
  | 'official'
  | 'financial_report'
  | 'news'
  | 'zhihu'
  | 'bilibili'
  | 'weibo'
  | 'xiaohongshu'
  | 'douyin'
  | 'review'
  | 'web'
  | 'unknown'

export type StageId =
  | 'intake'
  | 'orchestrator'
  | 'collect'
  | 'analyze'
  | 'audit'
  | 'rework'
  | 'write'
  | 'done'

/**
 * 一个阶段在 DAG 上的状态。**与后端 `RunnerState.nodes` 的取值同一套。**
 *
 * 曾经写的是 `idle | working | rework | done | failed`，三处都不对：
 *  - `rework` 是一个**阶段**（`StageId` 里有它），不是一种状态。
 *    把它混进状态里，DAG 上"返工轮次"就没地方表达了。
 *  - `failed` 表达不了 `degraded`——而"跑完了但不完整"是这套系统的
 *    常态之一（降级必须可见），把它并进 `done` 会让降级的阶段看起来一切正常。
 *  - `idle` 与后端的 `pending` 是同一个意思，两个词会导致
 *    「DAG 初始状态」这件事有两处定义。
 */
export type StageState = 'pending' | 'running' | 'done' | 'degraded' | 'error'

export type Sentiment = 'positive' | 'neutral' | 'negative'

/** 标注来源。必须披露，否则「正面 62%」可能其实是关键词计数器算出来的。 */
export type LabeledBy = 'llm' | 'rule'

export type ModelTier = 'core' | 'aux' | 'fast'

// ============================================================
// 专家名册
// ============================================================

export interface Expert {
  id: string
  level: ExpertLevel
  group: ExpertGroup
  name: string
  roleTitle: string
  oneLiner: string
  skills: string[]
  knowledgeBase: string
  knowledgeTags: string[]
  avatar: string
  badgeColor: string
  domainIcon: string
}

export interface StageDef {
  id: StageId
  label: string
  /** 主导该阶段的专家层级；null 表示系统环节（如 done） */
  ownerLevel: ExpertLevel | null
  description: string
}

// ============================================================
// 证据与论点
// ============================================================

/**
 * 可信度的分项明细。有了它，UI 才能回答「为什么这条是 82 分」。
 *
 * `total` 与四个分项加扣分的和**必须相等**（后端有一条测试守着这个恒等式）。
 * 只要两者可能对不上，明细就不再是「解释」而只是「另一组数字」——
 * 读者没法用明细去复核总分，可解释性就落空了。前端直接用 `total`，
 * 不要自己求和：那会把同一套策略实现两遍，迟早有一处漏改扣分项。
 */
export interface CredibilityBreakdown {
  sourceTypeScore: number
  freshnessScore: number
  contentScore: number
  crossRefScore: number
  /** 扣分项之和，≤ 0 */
  penalties: number
  /** 每一笔加减分的理由，逐条给出来 */
  notes: string[]
  /** 0–100，等于上面五项之和（钳制后） */
  total: number
}

/**
 * 一条采集到的证据。**键名与 `Evidence.to_dict()` 逐字一致**——
 * 后端显式列出每个键而不是跑通用的 snake→camel 转换，就是为了让这里
 * 能被一条契约测试对上（`backend/tests/contract/test_sse_contract.py`）。
 *
 * 刻意没有 `domain` 字段，尽管「独立域名数」是头条指标：主机名的判据
 * （子域算独立、去掉 www）写在 `core/evidence/sourcetypes.py` 的
 * `independent_domain` 里，有明确的已知代价。在前端用 `new URL().hostname`
 * 重写一遍那条规则，会让报告页上的域名数与指标里的对不上。
 * 需要的那个**数**从 `metrics.independentDomains` 读，逐条的主机名
 * 由 `/api/evidences` 那个接口补。
 */
export interface Evidence {
  evidenceId: string
  url: string
  title: string
  /** 搜索结果的摘要。`fullText` 为空时它就是唯一的正文 */
  snippet: string
  /** 抓取到的网页正文。空字符串表示没抓到（此时 `degraded` 为 true） */
  fullText: string
  brand: string
  sourceType: SourceType
  /** 站点名。provider 没给时是主机名 */
  siteName: string
  publishedAt: string
  capturedAt: string
  /** 命中的调研维度，是「维度覆盖率」这个指标的真相源 */
  matchedDimensions: string[]
  /** 采到它时用的检索词 */
  query: string
  /** 哪家搜索源给的 */
  provider: string
  /** 该检索词下的名次 */
  rank: number
  /** 0–100 */
  credibility: number
  credibilityBreakdown?: CredibilityBreakdown
  /** 正文抓取失败、仅有摘要时为 true */
  degraded: boolean
  /** 从这条证据里带出来的配图 */
  images: { url: string; alt?: string }[]
}

export interface Claim {
  claimId: string
  text: string
  confidence: Confidence
  evidenceIds: string[]
  /** 引用了多少个互不相同的域名——交叉验证的度量（铁律 2） */
  independentDomains: number
  brand?: string
  dimension?: string
  sectionId?: string
}

/** 引用校验的统计。LLM 编造的 evidence_id 会被丢弃，这里记录丢了多少。 */
export interface CitationStats {
  emitted: number
  kept: number
  dropped: number
  dropRate: number
}

// ============================================================
// 消息协议
// ============================================================

export type Severity = 'blocker' | 'major' | 'minor'

// 这里曾经有一个 `Issue`（`{issueId, kind, severity, target, message, suggestion}`），
// `kind` 有五个取值。**它和服务端发的不是同一样东西**：
// 服务端发的是 `{kind, kindLabel, severity, dimension, brand, detail,
// suggestion, targets, evidenceIds, resolved}`，`kind` 有九个取值
// （后端 `models.ISSUE_KINDS`）。
//
// 两边只有"有个问题"这件事是一致的。它错了很久，因为**没有任何东西
// 在用它**——唯一提到它的 `TeamPreview` 自己也是死代码（已一并删掉），
// 所以类型检查、测试、界面全都不会碰到它。
//
// 审计问题的类型现在在 `types/report.ts` 的 `ReportAuditIssue`，
// 那份是照着服务端的 `Issue.to_dict()` 写的，并有报告页在用。
// 放这儿的话会重新长出第二个定义——这正是当初出错的方式。

/**
 * 一条专家间消息的类型。与 `core/models.py` 的 `Envelope.kind` 一致。
 *
 * 曾经写的是 `PRODUCE | REWORK | PASS`——那是参考实现的词汇，本系统的
 * 后端从来不发这三个值。按"派活 / 交活 / 提问 / 回结果"分，
 * 是因为三层专家分工的**可解释性**就在这里：读者要看的是
 * "谁把什么交给了谁"，而 L3→L2 的 `handoff` 与 L1→L2 的 `handoff`
 * 在流程图上含义不同，靠 `from`/`to` 的层级才分得开。
 */
export type EnvelopeKind = 'request' | 'result' | 'issue' | 'handoff'

/** 完整的信封。随报告落库（`messages[]`），不进 SSE——见 `MessageSummary`。 */
export interface Envelope {
  sender: string
  recipient: string
  kind: EnvelopeKind
  payload: Record<string, unknown>
  traceId: string
  createdAt: string
}

/**
 * 事件流里的信封投影。
 *
 * 与 `Envelope` 是两个类型，不是同一个：事件的载荷里**不带** `payload`
 * （可能是一整包证据，给每个订阅者推全量是浪费），改带一个两百字的摘要。
 * 名字因此不叫 `envelope`——它不是信封，是信封的摘要。
 */
export interface MessageSummary {
  from: string
  to: string
  kind: EnvelopeKind
  stage: StageId
  /** 返工轮次，非返工时为 0 */
  round: number
  summary: string
  /** 信封里带的 issue 条数。完整列表在报告里 */
  issueCount: number
}

/**
 * 一条专家思维。事件里的那一份与报告 `thoughts[]` 里的**是同一份**，
 * 所以它有自己的 `id` 和自己的时间戳——报告里那份不经过 SSE 信封。
 */
export interface Thought {
  id: string
  expertId: string
  expertName: string
  /** 名册里查不到这位专家时是空串。前端要显示得出来，而不是渲染一个空标签 */
  roleTitle: string
  level: ExpertLevel | ''
  stage: StageId
  text: string
  at: string
}

/** 图集里的一张图。六个字段全部来自它所属的那条证据。 */
export interface GalleryImage {
  url: string
  /** 原页面的 alt 文本。图注由前端用 brand / siteName 拼 */
  alt: string
  evidenceId: string
  brand: string
  /** 图片来源页，不是图片本身的地址 */
  sourceUrl: string
  siteName: string
}

// ============================================================
// 调研计划与组队
// ============================================================

export interface Scope {
  subject: string
  domain: string
  category: string
  candidateBrands: string[]
  rationale: string
}

export interface ClarifyQuestion {
  id: string
  question: string
  kind: 'single' | 'multi' | 'text'
  options: string[]
  recommended: string
}

export interface ResearchPlan {
  subject: string
  brands: string[]
  dimensions: string[]
  searchAngles: string[]
  rationale: string
}

export interface TeamAssignment {
  expertId: string
  expert: Expert
  role: string
  reason: string
}

export interface TeamPlan {
  lead?: TeamAssignment
  strategists: TeamAssignment[]
  executors: TeamAssignment[]
  rationale: string
  /** 抽取队伍时丢弃了 LLM 编造的专家 id —— 会影响人数，必须可见 */
  droppedAssignments: string[]
}

// ============================================================
// 结构化知识
// ============================================================

export type SupportLevel = 'full' | 'partial' | 'none' | 'unknown'

export interface FeatureNode {
  name: string
  support: SupportLevel
  note: string
  evidenceIds: string[]
}

export interface FeatureCategory {
  category: string
  features: FeatureNode[]
}

export interface FeatureTree {
  brand: string
  categories: FeatureCategory[]
  degraded: boolean
}

export interface PricingTier {
  name: string
  price: string
  period: string
  unit: string
  targetUser: string
  includes: string[]
  evidenceIds: string[]
}

export interface PricingModel {
  brand: string
  currency: string
  modelType: string
  freeTier: string
  tiers: PricingTier[]
  degraded: boolean
}

export interface Persona {
  name: string
  segment: string
  needs: string[]
  scenarios: string[]
  painPoints: string[]
  decisionFactors: string[]
  migrationCost: string
  evidenceIds: string[]
}

export interface UserPersona {
  brand: string
  personas: Persona[]
  degraded: boolean
}

// ============================================================
// 图表与数据
// ============================================================

export type ChartKind = 'bar' | 'line' | 'radar' | 'pie' | 'scatter'

export interface ChartSeries {
  brand: string
  color: string
  values: number[]
}

/**
 * 一张图的**规格**，不是渲染结果。后端不依赖任何图表库，
 * 前端用 ECharts 按 `spec` 画出来——于是图表的样式改动不需要动 Python。
 *
 * `spec` 刻意是一个不展开的袋子（里面是 `categories` / `series` /
 * `yAxis` / `indicators` …）：图的种类要增删时不该改动线上事件契约。
 * 需要按 `kind` 取值的地方，用下面这组类型去收窄。
 */
export interface ChartSpec {
  chartId: string
  kind: ChartKind
  title: string
  /** 图表也是论点，同样受铁律 1 约束。空数组即「这张图没有证据链」 */
  evidenceIds: string[]
  spec: ChartSpecBody
}

export interface ChartSpecBody {
  categories?: string[]
  series?: ChartSeries[]
  indicators?: { name: string; max: number }[]
  yAxis?: { min?: number; max?: number; name?: string }
  /** 其余按 `kind` 各自定义的字段。渲染前用 `kind` 判一次，别硬转 */
  [key: string]: unknown
}

export interface DataGrid {
  columns: string[]
  rows: string[][]
  evidenceIds?: string[]
}

// ============================================================
// 分析产物
// ============================================================

export interface CapabilityMatrix {
  dimensions: string[]
  brands: string[]
  /** scores[brandIndex][dimensionIndex]，0–5 */
  scores: number[][]
  evidenceIds: string[]
}

export interface MarketShare {
  brand: string
  /** 百分比 */
  share: number
  basis: string
  evidenceIds: string[]
}

export interface FiveForce {
  force: string
  /** 1–5 */
  intensity: number
  analysis: string
  evidenceIds: string[]
}

export interface TrendPoint {
  period: string
  value: number
}

export interface TrendSeries {
  name: string
  points: TrendPoint[]
  unit: string
  evidenceIds: string[]
}

// ============================================================
// 舆情
// ============================================================

export interface SentimentItem {
  itemId: string
  platform: SourceType
  brand: string
  text: string
  sentiment: Sentiment
  labeledBy: LabeledBy
  evidenceId?: string
  url?: string
}

export interface PlatformSentiment {
  platform: SourceType
  positivePct: number
  neutralPct: number
  negativePct: number
  sampleSize: number
  llmLabeled: number
  ruleLabeled: number
}

export interface SentimentBundle {
  brands: string[]
  platforms: PlatformSentiment[]
  items: SentimentItem[]
  /** 披露 LLM 与规则标注的占比，避免把关键词计数当成模型输出 */
  disclosure: string
}

// ============================================================
// 质检
// ============================================================

export interface DimensionCoverage {
  dimension: string
  covered: boolean
  evidenceCount: number
  claimCount: number
  evidenceIds: string[]
}

export interface QualityMetrics {
  dimensionCoverageRate: number
  brandCoverageRate: number
  confidenceRatio: number
  schemaCompleteness: number
  evidenceCount: number
  independentDomains: number
  unverifiedClaimRate: number
  crossValidatedRate: number
  hallucinatedCitationRate: number
}

export interface DimensionReview {
  dimension: string
  /** 1–5 */
  score: number
  comment: string
}

export interface QualityReport {
  metrics: QualityMetrics
  byDimension: DimensionCoverage[]
  review?: {
    dimensions: DimensionReview[]
    summary: string
    model: string
  }
  reworkRounds: number
  metricsBefore?: QualityMetrics
  metricsAfter?: QualityMetrics
  issuesResolved: string[]
}

// ============================================================
// 报告
// ============================================================

export type SectionKind = 'text' | 'structured' | 'data'

export interface ReportSection {
  sectionId: string
  title: string
  order: number
  kind: SectionKind
  paragraphs: string[]
  claimIds: string[]
  evidenceIds: string[]
  chartIds: string[]
  dataGrid?: DataGrid
  featureTrees?: FeatureTree[]
  pricingModels?: PricingModel[]
  userPersonas?: UserPersona[]
  model: string
  degraded: boolean
}

export interface RunMetrics {
  elapsedSeconds: number
  firstEvidenceSeconds: number
  llmCalls: number
  totalTokens: number
  costUsd: number
  evidenceCount: number
  independentDomains: number
  platformCount: number
  highConfidenceRatio: number
  reworkRounds: number
  /** 人工分钟 / 实际分钟 */
  efficiencyMultiple: number
  coverageRatio: number
  consistency: number
  accuracy: number
  manualCorrectionRate: number
  /** 每个比例的基线都要标注来源，否则数字是编的 */
  baselines: Record<string, string>
}

export interface Report {
  reportId: string
  taskId: string
  title: string
  subtitle: string
  query: string
  mode: ResearchMode
  brands: string[]
  scope?: Scope
  plan?: ResearchPlan
  team?: TeamPlan
  expertIds: string[]
  sections: ReportSection[]
  claims: Claim[]
  evidences: Evidence[]
  charts: ChartSpec[]
  matrix?: CapabilityMatrix
  marketShare: MarketShare[]
  fiveForces: FiveForce[]
  trends: TrendSeries[]
  sentiment?: SentimentBundle
  quality?: QualityReport
  metrics: RunMetrics
  citationStats: CitationStats
  /**
   * 哪里降级了、为什么，每条一个 `"块：原因"` 字符串。
   *
   * **降级必须可见**：静默降级的报告看起来和完整报告一样自信，而读者
   * 无从知道某一块其实没做成——这比明确报错更危险。
   * 空数组表示整份报告没有任何降级。
   */
  degraded: string[]
  createdAt: string
}

export interface ReportSummary {
  reportId: string
  taskId: string
  title: string
  subtitle: string
  query: string
  mode: ResearchMode
  brands: string[]
  evidenceCount: number
  claimCount: number
  costUsd: number
  elapsedSeconds: number
  degraded: boolean
  createdAt: string
}

// ============================================================
// 任务
// ============================================================

/**
 * 任务的状态。**集合与后端任务行里真实存在的状态严格相等**
 * （`contracts/task_states.json` 是唯一真相源，两侧各有测试比对）。
 *
 * 这里以前写的是 `'created' | 'clarifying' | 'running' | 'done' | 'failed'`，
 * 与真正会落库的那六个只对得上三个，而**没有任何东西会因此报错**：
 * 澄清页的 `status === 'clarifying'` 恒为假（那一页永远不跳）、
 * `cancelled` 与 `awaiting_clarify` 落到 `switch` 的 default
 * （列表里渲染成没有标签的空状态）。这类漂移只能靠契约测试挡住。
 *
 * `pending` 是建行时的初始值，在流水线启动前真实存在，不是一瞬间。
 * `awaiting_clarify` 是**等回答**，任务马上还要接着跑——所以它不在终态里。
 */
export type TaskStatus =
  | 'pending'
  | 'running'
  | 'awaiting_clarify'
  | 'done'
  | 'failed'
  | 'cancelled'

/** 全部任务状态。要用（过滤器、标签映射）时读它，别另写一份数组。 */
export const TASK_STATUSES = [
  'pending',
  'running',
  'awaiting_clarify',
  'done',
  'failed',
  'cancelled',
] as const satisfies readonly TaskStatus[]

/**
 * 终态：任务不会再变化了。
 *
 * **`awaiting_clarify` 刻意不在里面。** 后端的 `ensure_runner` 靠这个集合
 * 决定「回放历史」还是「启动流水线」；把等待澄清当成终态，
 * 用户答完之后流水线就再也不会动了，而界面显示一切正常。
 */
export const TERMINAL_STATUSES = ['done', 'failed', 'cancelled'] as const

export function isTerminal(status: TaskStatus): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status)
}

/**
 * 任务列表里的一行。**与 `TaskRecord.to_dict()` 逐字一致。**
 *
 * 这里以前少了 `stage`/`progress`/`subject`/`brands`/`error`，
 * 多了一个库里根本没有的 `reportId`，还把 `clarifyAnswers` 写成了
 * `answers`——最后一个是最坏的一种：字段名差一个词，读到的永远是
 * `undefined`，而 TypeScript 不报错，因为两边各自都自洽。
 */
export interface Task {
  taskId: string
  query: string
  mode: ResearchMode
  status: TaskStatus
  stage: StageId | ''
  /** 0–1，整个任务的进度 */
  progress: number
  needClarify: boolean
  clarifyQuestions: ClarifyQuestion[]
  clarifyAnswers: Record<string, string>
  subject: string
  brands: string[]
  createdAt: string
  updatedAt: string
  error: string
}

/**
 * 任务快照。工作台与澄清页读它。
 *
 * **两个生产者，键集合不一样**（见 `contracts/task_states.json`）：
 * 内存里有 runner 时给 `elapsedMs`，服务重启之后的兜底分支给
 * `evidenceCount`。所以这两个是可选的，读的时候必须兜底——
 * 无条件读 `snapshot.elapsedMs` 会在"重启后打开的页面"上显示 NaN，
 * 那看起来像渲染 bug，其实是契约问题。
 */
export interface TaskSnapshot {
  taskId: string
  status: TaskStatus
  stage: StageId | ''
  stageLabel: string
  /** 0–1。**整个任务**的进度，不是阶段内的 */
  progress: number
  /** 空串表示还没有报告 */
  reportId: string
  /** 空串表示没有错误 */
  error: string
  /**
   * 每个阶段的状态。兜底分支给 `{}`——那里无从得知，
   * 而全填 `pending` 会谎称"还没开始"，事实是"跑过一段但过程不在内存里"。
   */
  nodes: Partial<Record<StageId, StageState>>
  /** journal 的水位。重连时拿它当 `Last-Event-ID` */
  lastSeq: number
  /**
   * **事实**：这个需求信息不够，intake 生成过问题。
   * 一个早就跑完的任务这个值也可能是 true（它当时确实问了）。
   */
  needClarify: boolean
  /**
   * **状态**：现在正停着等回答。
   *
   * 判断"要不要跳澄清页"只能看它。看 `needClarify` 的话，
   * 一个上周跑完的任务会在每次打开时把用户拉回澄清页，
   * 而那一页上没有任何东西可做——问题早就答过了。
   */
  awaitingClarify: boolean
  clarifyQuestions: ClarifyQuestion[]
  subject: string
  brands: string[]
  /** 只有内存快照给：进程重启之后那个启动时刻已经不存在了 */
  elapsedMs?: number
  /** 只有兜底分支给 */
  evidenceCount?: number
}

/** `POST /api/tasks` 的响应：快照 + 这两个。前端直接当快照用。 */
export interface CreatedTask extends TaskSnapshot {
  query: string
  mode: ResearchMode
}

/** `GET /api/tasks` 的分页响应。`total` 是**过滤之后**的总数，不是本页条数。 */
export interface TaskListResponse {
  total: number
  limit: number
  offset: number
  items: Task[]
}

// ============================================================
// 可观测性
// ============================================================

/** span 的种类。`stage` 是阶段本身的总账，另外三种是 provider 调用。 */
export type SpanKind = 'llm' | 'search' | 'fetch' | 'stage'
export type SpanStatus = 'ok' | 'error' | 'degraded'

/**
 * 一次被观测的调用。键名与 `Span.to_event()` 逐字一致。
 *
 * 字段分三组，分别回答三个问题：**谁**（kind/name/provider/model）、
 * **多久**（startedAt/endedAt/durationMs）、**花了多少**（token 与成本）。
 *
 * 刻意没有 `taskId` 与 `seq`：事件携带它时两者都在信封里，读 span 时
 * taskId 来自 URL（`GET /api/tasks/{id}/trace`）。加进来会与信封里
 * 那一份重复，而两个字段描述同一件事时，它们迟早会不一致。
 *
 * **span 的先后顺序编码在 `spanId` 里（`SP-00007`），按它排序即可**——
 * 这句话是回放页的排序依据，别改成按 `startedAt` 排：
 * 后端 `Tracer.spans()` 用的就是编号（编号在 span 开始时发，
 * 所以它"就是"开始顺序），而编号与时间戳取自两个不同的临界区，
 * 并发下会不一致。见 `lib/replay.ts` 里 `flattenSpans` 的说明与问题 35。
 *
 * > 这一段原先举的例子是 `/api/reports/{id}/trace`。**那条路由不存在，
 * > 而且是有意不做的**（见 `docs/DECISIONS.md` D4）——
 * > 报告详情已经返回 `taskId`，再开一条报告级的路由等于同一份数据两条取法。
 */
export interface TraceSpan {
  spanId: string
  /** 父 span。空串表示它是这一层的根 */
  parentId: string
  kind: SpanKind
  /** 被观测对象的名称（函数名 / 检索词 / URL） */
  name: string
  /** 这次调用**为什么**发起。「在干什么」与「干的是哪一件」是两个问题 */
  purpose: string
  provider: string
  model: string
  startedAt: string
  endedAt: string
  /** 没有 `latencyMs`：耗时是从两个时间戳算出来的，存两份会分叉 */
  durationMs: number
  promptTokens: number
  completionTokens: number
  /** 前两者之和，由后端算好——各消费点自己加就会有一处漏改 */
  totalTokens: number
  costUsd: number
  /**
   * `promptTokens` 里命中前缀缓存的那部分，**是子集不是增量**。
   * 命中价与未命中价差 50 倍，所以成本面板要靠它说明「为什么这么便宜」；
   * 不区分缓存的 provider 恒为 0。
   */
  cachedPromptTokens: number
  status: SpanStatus
  error: string
  /** 这次调用产出了什么。自由 dict：搜索记命中数，写作记字符数 */
  detail: Record<string, unknown>
}

/**
 * 树上的一个 span。`GET /api/tasks/{id}/trace` 给的是**树**而不是扁平数组
 * （`traces_repo.span_tree`），因为决策回放要的是"当前 span 的祖先链"。
 *
 * 契约上它不是一个新类型，是 `TraceSpan` 加一个 `children`——
 * 所以这里用继承而不是重写一遍字段：重写的话，`TraceSpan` 加一个字段、
 * 这里漏加，编译器不会报错（对象字面量只检查多余属性），
 * 而 trace 面板上那个字段永远是 undefined。
 *
 * **但在当前数据上 `children` 永远是空数组 —— 它其实是一片森林**（问题 33.3）。
 *
 * 实测：mock 流水线一次 49 条 span、真库 1036 行，`parentId` 全是空串。
 * 原因是全仓库没有一处 `with span(...)` 嵌在另一处里面，
 * 所以 `Tracer` 取父 span 的那个 contextvar 从来没取到过东西。
 *
 * 接口仍然按树返回（组装在服务端，见 `span_tree`），因为
 * **父不在就丢弃**是个很容易写出来的 bug：一旦将来真有嵌套，
 * 那样写会让整棵子树从界面上消失，而看起来只像"这次跑得少"。
 * 所以渲染端仍然按递归写，只是别指望现在能靠真实数据测到嵌套那一段——
 * 要测就自己造（`tests/integration/test_task_trace_api.py` 里是这么做的）。
 */
export interface SpanNode extends TraceSpan {
  children: SpanNode[]
}

// ============================================================
// Provider
// ============================================================

export interface LlmCapabilities {
  jsonMode: boolean
  thinkingToggle: boolean
  streaming: boolean
  maxContextTokens: number
  maxOutputTokens: number
}

export interface SearchCapabilities {
  siteFilter: boolean
  freshnessFilter: boolean
  longSnippet: boolean
  maxResultsPerCall: number
}

export interface ProviderInfo {
  kind: 'llm' | 'search'
  name: string
  active: boolean
  configured: boolean
  baseUrl: string
  models?: Record<string, string>
  capabilities: LlmCapabilities | SearchCapabilities
}

export interface ProviderHealth {
  name: string
  kind: 'llm' | 'search'
  ok: boolean
  latencyMs: number
  detail: string
  checkedAt: string
}

// ============================================================
// 仪表盘
// ============================================================

export interface DashboardData {
  reportCount: number
  taskCount: number
  evidenceCount: number
  totalCostUsd: number
  totalTokens: number
  avgElapsedSeconds: number
  avgEvidencePerReport: number
  reworkTriggerRate: number
  degradedRate: number
  manualCorrectionRate: number
  modeBreakdown: Array<{ mode: ResearchMode; count: number }>
  sourceTypeBreakdown: Array<{ sourceType: SourceType; count: number }>
  recentReports: ReportSummary[]
}

export interface CostPoint {
  date: string
  costUsd: number
  tokens: number
  calls: number
}
