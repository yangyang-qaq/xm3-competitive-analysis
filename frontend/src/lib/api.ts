/**
 * REST 客户端。
 *
 * 走相对路径 `/api`，开发时由 vite 代理到后端 8020（见 vite.config.ts），
 * 生产由反向代理接管——所以这里不需要知道后端地址，也就不存在环境变量漏配的问题。
 */

import type { ReportBody, ReportSection } from '../types/report'
import type {
  CreatedTask,
  ResearchMode,
  SourceType,
  SpanKind,
  SpanNode,
  StageId,
  TaskListResponse,
  TaskSnapshot,
} from '../types/domain'

const BASE = '/api'

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function readErrorMessage(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()) as { detail?: unknown; message?: unknown }
    const detail = body.detail ?? body.message
    if (typeof detail === 'string') return detail
    if (detail) return JSON.stringify(detail)
  } catch {
    // 响应不是 JSON，退回状态文本
  }
  return resp.statusText || `HTTP ${resp.status}`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    throw new ApiError(await readErrorMessage(resp), resp.status)
  }
  return (await resp.json()) as T
}

// ---------- 健康检查 ----------
// 注意：/health 不在 /api 前缀下，所以直接 fetch，不走上面的 request()。

export interface HealthPayload {
  status: string
  version: string
  llmProvider: string
  llmConfigured: boolean
  llmBaseUrl: string
  searchProvider: string
  searchConfigured: boolean
  llmFallback: string | null
  cassetteMode: string
  dbPath: string
}

export function health(): Promise<HealthPayload> {
  return fetch('/health').then(async (resp) => {
    if (!resp.ok) throw new ApiError(await readErrorMessage(resp), resp.status)
    return (await resp.json()) as HealthPayload
  })
}

// ---------- 任务 ----------

export interface PipelineStage {
  key: StageId
  label: string
  description: string
  order: number
  /** 可选阶段：本次运行可能完全不经过它（比如不需要返工） */
  optional: boolean
  /** 该阶段开始时的**累计**进度。DAG 与进度轨用它，前端不再自己推 */
  startProgress: number
}

export interface ModeInfo {
  key: ResearchMode
  label: string
  description: string
  maxBrands: number
  maxDimensions: number
  maxSearchCalls: number
  /**
   * 返工的检索额度，**所有返工轮加起来**的一份独立预算，不占 `maxSearchCalls`。
   * 展示时别把它并进 `maxSearchCalls`：两个数是两本账，
   * 并起来会让人以为"选了 64 就最多花 64"，而实际上限是两者之和。
   */
  reworkSearchCalls: number
  maxReworkRounds: number
  sectionCount: number
  enableSentiment: boolean
  enableStructured: boolean
}

/**
 * trace 汇总。**这里没有、也不该有一个叫 `durationMs` 的字段**（问题 33.1）。
 *
 * 那个名字会被读成"这次任务花了多久"，而任务耗时只有一个出处：
 * `done` 事件里的 **`metrics.durationMs`**（**不叫 `elapsedMs`**——
 * 那个名字属于 `node_update` 与 `runner.snapshot()`）。后端曾经把
 * "所有 span 耗时相加"叫 `durationMs`，实测一个 183 秒的任务它是 789 秒
 * （并发扇出相加，四倍多），接上"耗时"就会显示一个不报错的大错数。
 *
 * 现在给的是三个名字各自说清楚的东西：
 * - `spanDurationMs` —— 所有 span 耗时**相加**（总占用）；
 * - `spanWindowMs` —— span 覆盖的墙钟窗口，**也不是任务耗时**；
 * - `concurrency` —— 前两者相除。≈1 是串行链，4.3 是同时开了四路。
 *
 * 第三个不是装饰：**"变慢了"与"变串行了"是两种病**，只看端到端耗时
 * 它们长得一样，这两个数分得开。
 */
export interface TraceSummary {
  spanCount: number
  spanDurationMs: number
  spanWindowMs: number
  concurrency: number
  slowestMs: number
  costUsd: number
  totalTokens: number
  cachedPromptTokens: number
  /**
   * **失败与降级分开数**（问题 33.2）。曾经合成一个 `failedCount`，
   * 而真实库里 249 条 span 有 24 条 `degraded`、`error` 是 0——
   * 印成"24 次失败"，用户读到的是"这个系统不稳"，
   * 实际是"24 个页面抓到了但正文太薄"，那是采集的正常形态。
   */
  errorCount: number
  degradedCount: number
  byKind: {
    kind: SpanKind
    count: number
    spanDurationMs: number
    costUsd: number
    totalTokens: number
  }[]
}

export interface TaskTrace {
  taskId: string
  /** **树**，不是扁平数组——但当前数据里它是一片全无子节点的森林。见 `SpanNode` 的注释 */
  spans: SpanNode[]
  summary: TraceSummary
}

// ---------- 我的调研 ----------

/**
 * 报告卡片。**不含正文**——后端的列表接口根本不读 `data` 那一列，
 * 所以这里也拿不到（见 `routes_reports._summary` 的 docstring：
 * 一份报告正文有几 MB，50 份就是几百 MB，而这个数字会随使用时间自然增长）。
 */
export interface ReportSummary {
  reportId: string
  taskId: string
  query: string
  subject: string
  brands: string[]
  mode: ResearchMode
  generatedAt: string
  metrics: Record<string, number | string | unknown>
  quality: { passed?: boolean; publishable?: boolean; blockers?: number }
  evidenceCount: number
  degradedCount: number
}

export interface ReportListResponse {
  items: ReportSummary[]
  total: number
  limit: number
  offset: number
}

export interface CorrectionRate {
  reports: number
  annotatedReports: number
  correctionRate: number
  totalFeedbacks: number
}

export interface ReportFeedback {
  feedbackId: string
  reportId: string
  sectionKey: string
  kind: string
  content: string
  author: string
  createdAt: string
}

export interface ReportDetail {
  reportId: string
  taskId: string
  /**
   * 整份报告正文。章节、图表、指标、质量门、审计全在里面。
   *
   * 类型来自 `types/report.ts`——**这份声明不在本文件里**。报告正文有
   * 三十多个键、六七层嵌套，它描述的是数据本身，与"怎么发请求"无关。
   * 早先这里有一份只声明了七八个键的 `ReportBody`，
   * 并留了一句"报告页补全时会一并加进来"；现在报告页做出来了，
   * 那句话兑现了。
   */
  data: ReportBody
  feedback: ReportFeedback[]
  feedbackCount: number
}

/** 「深化本节」的结果。后端 `report/refine.py` 的返回值。 */
export interface RefineResult {
  reportId: string
  /** 重写后的那一节，直接替换掉页面上原来那份 */
  section: ReportSection
  sectionKey: string
  addedEvidences: number
  phantomCitations: string[]
  degraded: string[]
  refinement: { sectionKey: string; annotation: string; addedEvidences: number; at: string }
  completeness: ReportBody['completeness']
  quality: ReportBody['quality']
  feedbackCount: number
  correctionRate: CorrectionRate
}

/** 导出走的是浏览器直接下载，不是 fetch——所以这里给的是 URL。 */
export function reportExportUrl(reportId: string, format: 'md' | 'json' = 'md'): string {
  return `${BASE}/reports/${encodeURIComponent(reportId)}/export?format=${format}`
}

// ---------- 知识库 ----------

export interface EvidenceCard {
  evidenceId: string
  url: string
  title: string
  snippet: string
  brand: string
  sourceType: SourceType
  siteName: string
  publishedAt: string
  capturedAt: string
  credibility: number
  degraded: boolean
  /** 被多少次调研采到过。**知识库区别于证据流的那一列**——见后端 `library()`。 */
  taskCount: number
}

export interface EvidenceQuery {
  brand?: string
  sourceType?: string
  minCred?: number
  domain?: string
  q?: string
  limit?: number
  offset?: number
}

export interface EvidenceListResponse {
  items: EvidenceCard[]
  /** 去重后的**来源**数 */
  total: number
  limit: number
  offset: number
}

export interface FacetBucket {
  value: string
  count: number
}

export interface EvidenceFacets {
  /** 去重后的来源数 */
  total: number
  /** 包含重复的总行数。"266 条来源 / 被引用 2426 次"里的后一个数 */
  mentions: number
  bySourceType: FacetBucket[]
  byBrand: FacetBucket[]
}

// ---------- 专家公会 ----------

export interface ExpertStats {
  tasks?: number
  thoughts?: number
  evidences?: number
  costUsd?: number
  /**
   * `seed` 表示**还没被量过**——里面的 0 是"此刻正确"，不是"量出来是 0"。
   * 界面据此决定显示不显示，直接印出来就是编数据。
   */
  source?: string
}

export interface Expert {
  expertId: string
  level: string
  levelLabel: string
  group: string
  name: string
  roleTitle: string
  oneLiner: string
  skills: string[]
  knowledgeBase: string
  knowledgeTags: string[]
  badgeColor: string
  avatarColor: string
  domainIcon: string
  stats: ExpertStats
  /** 被派进过多少份报告。真实数出来的（见后端 `reports.team_usage`） */
  participation: number
}

export interface ExpertLevelBucket {
  level: string
  label: string
  description: string
  count: number
}

export interface ExpertListResponse {
  items: Expert[]
  /** 过滤之后的条数 */
  total: number
  /** 名册总人数。页头写"48 位专家"用它，用 `total` 的话筛选后会变成"12 位" */
  rosterSize: number
  byLevel: ExpertLevelBucket[]
  byGroup: FacetBucket[]
}

export interface ExpertDetail extends Expert {
  reports: Array<{
    reportId: string
    taskId: string
    query: string
    subject: string
    generatedAt: string
  }>
}

// ---------- 竞争情报中心 ----------

export interface Dashboard {
  coverage: {
    brands: number
    brandNames: string[]
    sources: number
    mentions: number
    avgEvidencesPerReport: number
    claims: number
    crossValidatedClaims: number
    crossValidationRate: number
    independentDomains: number
    platformCount: number
  }
  runs: {
    reports: number
    tasks: number
    runningTasks: number
    passed: number
    publishable: number
    passRate: number
    totalCostUsd: number
    totalTokens: number
    avgDurationMs: number
  }
  byMode: Array<{
    mode: ResearchMode
    reports: number
    evidences: number
    passed: number
    avgEvidences: number
  }>
  correction: CorrectionRate
  recent: Array<{
    reportId: string
    taskId: string
    query: string
    subject: string
    mode: ResearchMode
    generatedAt: string
    evidences: number
    claims: number
    passed: boolean
    publishable: boolean
  }>
}

/**
 * REST 入口。
 *
 * **这里没有 `stream()`。** SSE 不走 `fetch`：它要的是 `EventSource`
 * 的自动重连与 `Last-Event-ID`，而 `fetch` 两者都没有。
 * 把它塞进这个对象里，会让人以为它和别的几个一样是一次性的请求。
 */
export const api = {
  /** DAG 节点。**前端不自己写一份阶段列表**——写了的话，后端加一个阶段、
   *  前端不显示，而没有任何东西会报错。 */
  pipelineStages: () =>
    request<{ stages: PipelineStage[]; terminal: StageId }>('/pipeline/stages'),

  /** 三档模式。档位里的数字（品牌数、检索预算、返工轮数）是会变的，
   *  硬编码在前端就会在改了档位之后继续显示旧数字。 */
  modes: () => request<{ default: ResearchMode; modes: ModeInfo[] }>('/modes'),

  createTask: (body: { query: string; mode?: ResearchMode; autoClarify?: boolean }) =>
    request<CreatedTask>('/tasks', { method: 'POST', body: JSON.stringify(body) }),

  listTasks: (params: { limit?: number; offset?: number; status?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.offset !== undefined) query.set('offset', String(params.offset))
    if (params.status) query.set('status', params.status)
    const suffix = query.toString()
    return request<TaskListResponse>(`/tasks${suffix ? `?${suffix}` : ''}`)
  },

  getTask: (taskId: string) => request<TaskSnapshot>(`/tasks/${encodeURIComponent(taskId)}`),

  /** 交澄清答案。任务不在等回答时后端返回 409，调用方要处理这个分支。 */
  clarifyTask: (taskId: string, answers: Record<string, string>) =>
    request<TaskSnapshot>(`/tasks/${encodeURIComponent(taskId)}/clarify`, {
      method: 'POST',
      body: JSON.stringify({ answers }),
    }),

  taskTrace: (taskId: string) =>
    request<TaskTrace>(`/tasks/${encodeURIComponent(taskId)}/trace`),

  // ---------- 我的调研（报告） ----------

  listReports: (params: { limit?: number; offset?: number; mode?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.offset !== undefined) query.set('offset', String(params.offset))
    if (params.mode) query.set('mode', params.mode)
    const suffix = query.toString()
    return request<ReportListResponse>(`/reports${suffix ? `?${suffix}` : ''}`)
  },

  getReport: (reportId: string) =>
    request<ReportDetail>(`/reports/${encodeURIComponent(reportId)}`),

  addFeedback: (reportId: string, body: { content: string; sectionKey?: string; kind?: string }) =>
    request<{ feedbackId: string; count: number; correctionRate: CorrectionRate }>(
      `/reports/${encodeURIComponent(reportId)}/feedback`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  /**
   * 按批注**深化一节**：再搜一轮、再写一遍。
   *
   * 这是本文件里唯一一个"会花钱、会改数据、且要等几十秒"的调用——
   * 所以页面必须给它一个明确的进行中状态，而不能像别的按钮那样
   * 点完就等着。`search=false` 是"我只要换个说法，别再去搜"。
   */
  refineReport: (
    reportId: string,
    body: { annotation: string; sectionKey?: string; search?: boolean },
  ) =>
    request<RefineResult>(`/reports/${encodeURIComponent(reportId)}/refine`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ---------- 知识库（证据来源） ----------

  listEvidences: (params: EvidenceQuery = {}) => {
    const query = new URLSearchParams()
    if (params.brand) query.set('brand', params.brand)
    if (params.sourceType) query.set('sourceType', params.sourceType)
    if (params.minCred !== undefined) query.set('minCred', String(params.minCred))
    if (params.domain) query.set('domain', params.domain)
    if (params.q) query.set('q', params.q)
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.offset !== undefined) query.set('offset', String(params.offset))
    const suffix = query.toString()
    return request<EvidenceListResponse>(`/evidences${suffix ? `?${suffix}` : ''}`)
  },

  evidenceFacets: () => request<EvidenceFacets>('/evidences/facets'),

  // ---------- 专家公会 ----------

  listExperts: (params: { level?: string; group?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.level) query.set('level', params.level)
    if (params.group) query.set('group', params.group)
    const suffix = query.toString()
    return request<ExpertListResponse>(`/experts${suffix ? `?${suffix}` : ''}`)
  },

  getExpert: (expertId: string) =>
    request<ExpertDetail>(`/experts/${encodeURIComponent(expertId)}`),

  // ---------- 竞争情报中心 ----------

  dashboard: () => request<Dashboard>('/dashboard'),
}

export { request }
