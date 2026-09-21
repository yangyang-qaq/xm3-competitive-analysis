/**
 * 报告正文的完整形状。报告页是**唯一**读它的地方。
 *
 * 为什么单独一个文件而不是继续写在 `lib/api.ts` 里
 * ----------------------------------------------
 * `api.ts` 里原本有一份 `ReportBody`，只声明了当时用得到的几个键，
 * 并留了一句话："服务端还有十几个键，报告页补全时会一并加进来"。
 * 现在就是那个时候。留在 `api.ts` 里的话，那个文件会同时装着
 * "怎么发请求"和"报告长什么样"——而报告的形状有 30 多个键、
 * 六七层嵌套，它是这一侧最重的一份声明，值得自己一个文件。
 *
 * 全是可选字段，而且**是有意的**
 * ----------------------------
 * 这份类型描述的是**库里的既有数据**，不是"一份理想的报告"。
 * 库里那 13 份报告是不同版本的代码在不同档位下写的：quick 档没有
 * `fiveForces`，没有采到评价类证据的那份 `sentiment.labels` 是空的，
 * 更早的那几份没有 `citations`（见下面的注释）。
 *
 * 把它们写成必填，就要在读取处到处 `as` 断言——而 `as` 会把
 * "这个字段真的没有"和"我懒得判空"变成同一件事。写成可选，
 * 编译器会逼着每一处都回答"没有的时候显示什么"，而那个回答
 * 正是报告页上大部分文案的来源。
 *
 * 哪些字段是**契约**（后端保证一定有）
 * ----------------------------------
 * `REQUIRED_KEYS`（后端 `schemas/report.py`）里那十个：
 * version / subject / brands / mode / generatedAt / sections /
 * claims / evidences / metrics / quality。装配时会跑一次
 * `validate_report`，缺一个就记进出库校验问题。它们在下面标了
 * "契约字段"，其余的都是"有就显示，没有就不显示"。
 */

/** 章节 key → 中文标题。与后端 `schemas/report.py` 的 `SECTION_LABELS` 一致。 */
export const SECTION_LABELS: Record<string, string> = {
  executive_summary: '执行摘要',
  market_overview: '市场概览',
  feature_comparison: '功能对比',
  pricing: '定价分析',
  user_feedback: '用户反馈',
  conclusion: '结论与建议',
  trends: '趋势观察',
  swot: 'SWOT 分析',
}

/** 一节。`content` 里带 `[证据: EV-x]` 标记，由 `lib/reportCitation.ts` 拆。 */
export interface ReportSection {
  key: string
  title: string
  content: string
  claimIds: string[]
  evidenceIds: string[]
  /** 材料不足以支撑完整论述 */
  degraded: boolean
  /** 返工后重写过 */
  reworked: boolean
}

export interface ReportClaim {
  claimId: string
  text: string
  confidence: string
  evidenceIds: string[]
  brand?: string
  dimension?: string
  section?: string
  verified?: boolean
  crossValidated?: boolean
  independentDomains?: number
  /** 引用了不存在的证据的 id。非空说明模型编过引用（已被剔除） */
  phantomEvidenceIds?: string[]
}

/** 一条证据。字段与 `types/domain.ts` 的 `Evidence` 重合，但那份是**事件流**里的，
 *  这份是**落库**的；两者曾经漂开过一次（`fullText` 只在落库的那份里），
 *  所以不合并——合并会让人以为它们是同一个东西。 */
export interface ReportEvidence {
  evidenceId: string
  url: string
  title: string
  snippet: string
  fullText: string
  brand: string
  sourceType: string
  siteName: string
  publishedAt: string
  capturedAt: string
  matchedDimensions: string[]
  query: string
  provider: string
  rank: number
  credibility: number
  credibilityBreakdown?: {
    sourceTypeScore: number
    freshnessScore: number
    contentScore: number
    crossRefScore: number
    penalties: number
    notes: string[]
    total: number
  }
  degraded: boolean
  images: Array<{ url: string; alt?: string }>
}

/**
 * 一张图的规格。`kind` 决定 `spec` 里有什么——所以这是**判别联合**，
 * 不是 `spec: Record<string, unknown>`。
 *
 * 用 `unknown` 的话，图表组件里每个字段都要断言一次，而断言写错
 * （比如雷达图按柱状图读 `categories`）不会报错，只会画出一张空图——
 * 一张空图看起来像"这个维度没数据"，而不像"代码读错了字段"。
 *
 * 为什么**没有**一个 `kind: string` 的兜底成员
 * -----------------------------------------
 * 加一个的话，按 `kind` 收窄就永远收窄不干净（`string` 包含 `'bar'`），
 * 于是每个分支里 `spec` 都还是 `unknown`——判别联合白写了。
 *
 * 那"后端新增一种图"怎么办：运行时会遇到 `kind` 不在下面三个里的图，
 * 由 `chartOption()` 返回 `null`、卡片显示一句"这一版还不认识这种图"。
 * 类型这一侧只声明**本版本认识的**，而不是"所有可能存在的"——
 * 声明后者等于替后端做了一个它没承诺过的保证。
 */
export type ReportChart =
  | { chartId: string; kind: 'bar'; title: string; evidenceIds: string[]; spec: BarSpec }
  | { chartId: string; kind: 'radar'; title: string; evidenceIds: string[]; spec: RadarSpec }
  | { chartId: string; kind: 'pie'; title: string; evidenceIds: string[]; spec: PieSpec }
  | { chartId: string; kind: 'line'; title: string; evidenceIds: string[]; spec: LineSpec }

/**
 * 每种 spec 都可能带一个 `note`。
 *
 * 这是后端**主动写的一句披露**（`charts.py`：市场份额图写"份额为推算值，
 * 口径见各数据点的 basis 字段"，舆情图写混合标注比例）。
 * 它必须显示在图上——**前端把它丢掉就等于后端那句披露白写了**，
 * 而图照画，读者看到的是一个没有限定词的百分比。
 *
 * 第一版就是这样：后端从第一天起就发 `note`，`PieSpec` 里没有这个字段，
 * 于是那句话一路被静默丢弃。所以这里把它提到四种 spec 共有的位置。
 */
interface SpecNote {
  note?: string
}

export interface BarSpec extends SpecNote {
  categories: string[]
  series: Array<{ brand: string; color?: string; values: number[] }>
  /**
   * 只有能力矩阵那根柱状图带这个键（后端写死 `{min: 0, max: 5, name: "评分"}`），
   * 因为它的取值是 1–5 分的**量表**。
   *
   * 它必须被声明出来并且**被用上**：不写的话，ECharts 会按数据自适应
   * Y 轴，一根 4.2 分的柱子会和一根 4.4 分的看起来差一倍。
   * 量表图的轴不是刻度，是**口径**。
   */
  yAxis?: { min?: number; max?: number; name?: string }
}

export interface RadarSpec extends SpecNote {
  indicators: Array<{ name: string; max: number }>
  series: Array<{ brand: string; color?: string; values: number[] }>
}

export interface PieSpec extends SpecNote {
  data: Array<{ brand: string; share: number; basis?: string }>
}

/**
 * 折线图。**每条线各自带自己的横轴点**，不是"一个 categories 数组 + 若干 values"。
 *
 * 这是后端 `analysis/charts._trend_chart` 的实际形状，也反映了事实：
 * 同一张图里的趋势序列未必起止在同一批时间点上（一条按季度、一条按年度；
 * 或者某条某个季度没采到数）。写成 `categories + values[]` 就等于承诺
 * "所有线共用一套横轴"，而那个承诺在数据里不成立——一旦成立不了，
 * 短的序列会被**悄悄补零或者截断**，两种都不会报错。
 */
export interface LineSpec extends SpecNote {
  series: Array<{
    name: string
    unit: string
    points: Array<{ period: string; value: number }>
  }>
}

/**
 * 对比矩阵。**品牌优先**：`scores[品牌下标][维度下标]`。
 *
 * 这个约定原先只写在两个读取处（`charts.py` / `export.py`）的代码里，
 * 没有任何东西检查过它，而这个类型的第一版把注释写反了
 * （写成"`scores[品牌下标][维度下标]`"，与实现一致，但旁边的措辞
 * 说"行优先"是维度——**一份注释说反了比没有注释更坏**，
 * 因为它会被当成依据去写读取代码）。
 *
 * 模型偶尔反着给（库里 14 份报告里有一份是 7 维度 × 5 品牌存成 7 行 × 5 列）。
 * 采集侧现在会按形状翻回来并记一条降级（后端 `analysis/matrix.py`），
 * 但**库里已有的那份仍然是转置的**。所以前端不能假定：
 * 渲染前用 `lib/reportMatrix.ts` 的 `matrixOrientation()` 判一次，
 * 判不出来就拒绝画表 —— 一张转置的表和一张正确的表长得一模一样。
 */
export interface ReportMatrix {
  dimensions: string[]
  brands: string[]
  scores: number[][]
  evidenceIds: string[]
}

export interface MarketShareItem {
  brand: string
  share: number
  basis: string
  evidenceIds: string[]
}

export interface FiveForceItem {
  force: string
  /** 1–5 的强度。0 或缺失表示没判出来 */
  intensity: number
  analysis: string
  evidenceIds: string[]
}

export interface TrendSeries {
  name: string
  unit: string
  points: Array<{ period: string; value: number }>
  evidenceIds: string[]
}

/** 支持度。后端 `schemas/feature_tree.py` 只发这三个值。 */
export type FeatureSupport = 'full' | 'partial' | 'none' | string

export interface FeatureTree {
  brand: string
  categories: Array<{
    category: string
    features: Array<{
      name: string
      support: FeatureSupport
      note: string
      evidenceIds: string[]
    }>
  }>
  /** 0–1。有结论的功能点占比 */
  coverage: number
  unknownCount: number
  degraded: boolean
}

export interface PricingTier {
  name: string
  priceText: string
  priceValue: number | null
  monthlyValue: number | null
  currency: string
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
  entryPrice: number | null
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

export interface PersonaSet {
  brand: string
  personas: Persona[]
  /** 0–1。带证据的画像占比 */
  evidenceBackedRate: number
  degraded: boolean
}

export interface SentimentLabel {
  evidenceId: string
  brand: string
  sourceType: string
  sentiment: 'positive' | 'neutral' | 'negative'
  reason: string
  /** `llm` 或 `rule`。**必须显示**——见报告页舆情面板的注释 */
  labeledBy: string
  excerpt: string
}

export interface SentimentBrandRow {
  brand: string
  positive: number
  negative: number
  neutral: number
  total: number
  /** 0–1。正面占比 */
  positiveRate: number
}

/**
 * 舆情面板。
 *
 * **`labeledByLlm` / `labeledByRule` 必须在页面上分开印。**
 * 标注用的是"模型优先、规则兜底"：模型漏掉某条样本时，规则函数顶上。
 * 只印"正面 62%"的话，那个数看起来全是模型判断的结果——而其中可能有
 * 一小半是关键词匹配出来的（"卡顿"扣分、"好用"加分），两者的可信度
 * 完全不同。所以混合比例是**这个面板的主要信息之一**，不是脚注。
 *
 * `note` 里后端已经把这句话写好了（"共标注 N 条，其中 X 条由模型判断、
 * Y 条由关键词规则兜底"），直接用——前端再算一遍就是第二份真相源。
 */
export interface Sentiment {
  labels: SentimentLabel[]
  counts: { positive: number; negative: number; neutral: number }
  /** 由模型判断的条数 */
  labeledByLlm: number
  /** 由关键词规则兜底的条数 */
  labeledByRule: number
  total: number
  /** 0–1。模型判断的占比。`total` 为 0 时后端按 1 做分母，所以空面板要用 `total` 判空，别用这个数 */
  llmRatio?: number
  /** 按品牌拆的同一批标签。按总数降序 */
  byBrand?: SentimentBrandRow[]
  /** 空的时候后端给一句人话解释为什么空（比如"没有采到用户评价类证据，本节为空"） */
  note: string
}

export interface ReportMetrics {
  claims?: number
  verifiedClaims?: number
  unsupportedClaimRate?: number
  hallucinationRate?: number
  phantomCitations?: number
  crossValidatedClaims?: number
  crossValidationRate?: number
  independentDomains?: number
  minIndependentDomains?: number
  evidences?: number
  degradedEvidences?: number
  degradedRate?: number
  platformCount?: number
  platforms?: string[]
  dimensionsPlanned?: number
  dimensionsCovered?: number
  dimensionCoverage?: number
  dimensionCoverageDetail?: Record<string, number>
  searchCalls?: number
  searchErrors?: number
  rawHits?: number
  filteredHits?: number
  /**
   * 有多少条检索**把平台筛选折进了查询词**。
   *
   * 这是适配层"能力协商"的账：所用搜索源不支持按站点过滤时，
   * `calls.py` 不报错、也不静默丢掉平台约束，而是把平台域名拼进查询
   * 文本——精度低一些，但在任何搜索源上都能工作。次数记在这里，
   * 于是"这次调研有多少条查询的精度打了折"是**可查的**。
   *
   * 前端要显示它：一个不显示的降级和一个没发生的降级，在报告里
   * 长得一模一样。
   */
  siteFilterFolded?: number
  fetchedOk?: number
  fetchedDegraded?: number
  fetchSkippedByBudget?: number
  llmCalls?: number
  llmOptionalFailures?: number
  totalCostUsd?: number
  totalTokens?: number
  slowestCallMs?: number
  durationMs?: number
  firstEvidenceMs?: number
  reworkRounds?: number
  issues?: number
  /**
   * 主 LLM 故障切换到备用的记录。`FallbackLLM.summary()`。
   *
   * **正常情况下它是空对象 `{}`**——没配备用 provider 时后端就发一个
   * 空 dict。所以每个键都是可选的，而"空"和"有内容"必须分开判断
   * （看 `degraded`，不是看对象在不在）：配了备用但一次都没切过时
   * `degraded` 是 false，那时挂个警告是**造了一次没发生的故障**。
   *
   * 页面上它只在 `degraded` 为真时才该出现。这不是可选的装饰：
   * 主模型挂掉、由备用答完了整份报告，而报告里对此只字不提，
   * 那这份报告的"成本"和"耗时"读起来都会莫名其妙（换了模型，
   * 单价比、速度、风格全不一样）。
   */
  llmFallback?: {
    primary?: string
    secondary?: string
    /** 断路器跳了：后续调用全部直接走备用，不再试主 */
    latched?: boolean
    fallbackCalls?: number
    degraded?: boolean
    /** 每次切换一条：`{purpose, tier, from, to, error, message, latched}` */
    events?: Array<Record<string, unknown>>
  }
  byPurpose?: Record<string, number>
  /**
   * 返工前后的对比。只有真的返工过才有这个键。后端 `metrics.rework_delta()`。
   *
   * 逐项的对比在 **`changes`** 里（每项是 `{before, after, delta}`），
   * 不是三个并排的 map——早先这里声明成了 `delta` 这个不存在的键，
   * 于是报告页在**第一份真的返工过的报告上**就崩了：`rework.delta[key]`
   * 读的是 `undefined`。
   *
   * 两层结构各有用处：`before`/`after` 是平表，方便直接读数；
   * `changes` 带 `delta`，是"这三项里哪一项在动"的答案。
   */
  rework?: {
    before: Record<string, number>
    after: Record<string, number>
    changes: Record<string, { before: number; after: number; delta: number }>
    /** 返工多的那点钱。**不参与 `improved`** —— 返工必然更贵 */
    costDelta?: number
    /** 后端判的"这轮返工有没有用"：看覆盖与独立信源，不看成本 */
    improved?: boolean
  }
  /**
   * 每一轮返工做了什么。
   *
   * 是**对象数组**，不是字符串数组：`reason` 是审计给的返工理由，
   * `qualityPassed` 是这一轮之后的门禁结果，而提前停下的那一轮
   * 没有 `reason`、只有 `stopped`（值是 `no-new-evidence`）。
   * 早先这里声明成 `string[]`，页面又直接把它渲染成文本——
   * 那是一条必然的 React 崩溃（对象不能当子元素渲染），
   * 只是被上面那个 `delta` 的崩溃挡在了后面。
   */
  reworkLog?: Array<{
    round: number
    targets: number
    added: number
    reason?: string
    qualityPassed?: boolean
    /** 有值表示这一轮**提前结束**了，值是结束的原因 */
    stopped?: string
  }>
  qualityGatePassed?: boolean
}

export interface DimensionReview {
  dimension: string
  score: number
  comment: string
}

export interface QualityReview {
  dimensions: DimensionReview[]
  summary: string
  weakDimensions: string[]
  averageScore: number
}

export interface ReportQuality {
  passed?: boolean
  coverage?: number
  dimensionsPlanned?: number
  dimensionsCovered?: number
  uncoveredDimensions?: string[]
  blockers?: number
  major?: number
  minor?: number
  thresholds?: Record<string, number>
  review?: QualityReview
  failedBecause?: string[]
  /** 完整度 0–1。与 `passed` 是**两个判断**——见报告页质量门 */
  completeness?: number
  publishable?: boolean
}

/**
 * 一条审计问题。
 *
 * **这个类型刚被改对。** 原先 `types/domain.ts` 里有一个叫 `Issue` 的
 * 类型，字段是 `target` / `message`，`kind` 有五个取值；而服务端发的是
 * `dimension` / `brand` / `detail` / `kindLabel` / `targets`，
 * `kind` 有九个取值（`models.ISSUE_KINDS`）。两边只有"有问题"这件事
 * 是一致的。
 *
 * 它错了很久而**没有任何东西报错**，因为没有任何代码读它——唯一提到它的
 * `TeamPreview` 自己也是死代码（后端从不发那个事件），所以类型检查、
 * 测试、界面全都碰不到那一条链。两者已经一起删掉，换成了这一份。
 * 这不是"顺手清理"：一个声明着错误形状、又没有任何调用方的类型，
 * 下一个人会照着它写读取代码。
 *
 * `kind` 是 `string` 而不是联合类型：真值是后端那张表，在前端抄一遍
 * 就是第二份真相源。标签直接用服务端发来的 `kindLabel`，
 * 前端**不建 kind → 标签的映射表**——建了就会在加一种问题时静默显示
 * 一个英文 key。
 */
export interface ReportAuditIssue {
  issueId: string
  kind: string
  kindLabel: string
  severity: string
  dimension: string
  brand: string
  detail: string
  suggestion: string
  targets: string[]
  evidenceIds: string[]
  /** 返工是否已处理掉这条问题 */
  resolved: boolean
}

export interface CoercionReport {
  degraded: boolean
  repairs: string[]
  phantomIds: string[]
  producedIds: number
  resolvedIds: number
  missing: string[]
}

export interface GlossaryEntry {
  term: string
  definition: string
}

export interface GalleryItem {
  url: string
  alt: string
  evidenceId: string
  brand: string
  sourceUrl: string
  siteName: string
}

export interface EvidenceStats {
  total?: number
  /** 平均可信度。0–100 */
  avgCredibility?: number
  /** 正文没抓到的条数。与 `metrics.degradedEvidences` 是同一个数的两种口径 */
  degraded?: number
  bySourceType?: Array<{ value: string; count: number }>
  byBrand?: Array<{ value: string; count: number }>
  independentDomains?: number
}

/**
 * 完整度。`score` 是**加权**的：`filled` 记 1 分、`degraded` 记 0.5、
 * `missing` 记 0。
 *
 * 记半分是刻意的：一份"有定价章节但没抽出数字"的报告，比完全没有定价
 * 章节强、比抽出了数字弱，而布尔量表达不了这个区别——报告顶部只显示
 * 一个数的时候，那个数必须能表达它。
 *
 * `isPublishable` 只看 `missing`，**不看 `degraded`**：有降级但完整的
 * 报告是有用的（读者能看到哪些部分弱），缺章节的报告是误导的。
 * 所以它和 `score` 会不一致，而且不一致是有意义的。
 */
export interface Completeness {
  score?: number
  /** 每个模块的状态：`filled` / `degraded` / `missing` */
  blocks?: Record<string, string>
  /** 模块 key → 中文名。**用它显示，别在前端建映射表** */
  labels?: Record<string, string>
  /** 完全没有的模块 key */
  missing?: string[]
  /** 有但不全的模块 key */
  degraded?: string[]
  isPublishable?: boolean
}

export interface TeamRoster {
  lead?: string[]
  strategists?: string[]
  executors?: string[]
}

/**
 * 一条正文引用编号：`[number]` 指的是 `evidenceId`。
 *
 * **这是 2026-09-19 新加的键。** 在那之前编号由每个渲染器现算——
 * 导出算一遍、报告页再算一遍。两个渲染器各算一遍，就得保证遍历顺序、
 * 容错、参与编号的块永远一致，而没有东西守着它。现在 `assemble`
 * 算一次存进来，两边都读它（见后端 `report/citation_index.py`）。
 *
 * 库里那 12 份早于这次改动的报告**没有这个键**，所以它是可选的，
 * 页面会退回现算（规则与后端 `build_citations` 相同）。
 */
export interface ReportCitation {
  number: number
  evidenceId: string
}

/**
 * 「深化本节」留下的一条记录。
 *
 * `reworked: true` 只说这一节被改过，不说**为什么**改。批注是要求，
 * 深化是对它的执行，两者都该在页面上看得见。
 */
export interface ReportRefinement {
  sectionKey: string
  annotation: string
  addedEvidences: number
  degraded: string[]
  at: string
}

/**
 * 报告正文。**30 多个键**，其中标了 `契约字段` 的十个是后端保证有的。
 */
export interface ReportBody {
  /** 契约字段。改了正文结构就升它 */
  version?: string
  taskId?: string
  query?: string
  /** 契约字段 */
  subject?: string
  domain?: string
  category?: string
  /** 契约字段 */
  brands?: string[]
  /** 契约字段 */
  mode?: { key?: string; label?: string; description?: string }
  dimensions?: string[]
  /** 契约字段 */
  generatedAt?: string
  durationMs?: number

  /** 契约字段 */
  sections?: ReportSection[]
  /** 契约字段 */
  claims?: ReportClaim[]
  /** 契约字段 */
  evidences?: ReportEvidence[]
  charts?: ReportChart[]

  matrix?: ReportMatrix
  marketShare?: MarketShareItem[]
  fiveForces?: FiveForceItem[]
  trends?: TrendSeries[]
  featureTrees?: FeatureTree[]
  pricingModels?: PricingModel[]
  personaSets?: PersonaSet[]
  sentiment?: Sentiment

  team?: TeamRoster
  messages?: Array<{
    sender: string
    recipient: string
    kind: string
    payload: Record<string, unknown>
    /** 契约字段用的是 `snake_case`：这个键与 `thoughts[].at` 不同，
     *  它由 `Envelope.to_dict()` 直接 `asdict()` 出来，没有经过转换。
     *  写成 `createdAt` 的话这个字段在页面上永远是空的，
     *  而"消息没有时间"看起来只是数据缺失。 */
    trace_id?: string
    created_at?: string
  }>
  thoughts?: Array<{
    id: string
    expertId: string
    expertName: string
    roleTitle: string
    level: string
    stage: string
    text: string
    at: string
  }>
  coercion?: CoercionReport
  /** 降级说明。**非空就必须显示**——见报告页的降级横幅 */
  degraded?: string[]
  glossary?: GlossaryEntry[]

  evidenceStats?: EvidenceStats
  /**
   * 审计的全部产出。后端 `pipeline/audit.py` 的 `AuditResult.to_dict()`。
   *
   * 这里声明了五个键，页面目前只用 `issues`：
   * - `review` / `quality` 与顶层的 `quality`（和 `quality.review`）
   *   是同一份数据的两条路径，页面读的是顶层那份，免得同一个数两处渲染；
   * - `reworkTargets` 是"这轮要补哪几个维度 × 品牌"，明细已经在
   *   `metrics.reworkLog[].reason` 里有一句人话，这里就不重复列了。
   *
   * **还是要把它们声明出来**：声明的意义是"后端会发这些"这个事实，
   * 而不是"前端用到了这些"。少声明一个键，下一个人读类型时会以为
   * 后端不提供它，于是去别处再算一遍——那就有了第二份真相源。
   */
  audit?: {
    issues?: ReportAuditIssue[]
    review?: unknown
    quality?: unknown
    reworkTargets?: Array<{ brand?: string; dimension?: string; keyword?: string }>
    /** 这轮为什么返工，一句人话。`reworkLog[].reason` 是它的逐轮版本 */
    reworkReason?: string
  }
  /** 契约字段 */
  metrics?: ReportMetrics
  completeness?: Completeness
  /** 契约字段 */
  quality?: ReportQuality
  gallery?: GalleryItem[]

  citations?: ReportCitation[]
  /** 深化过几次、各按什么批注。旧报告没有这个键 */
  refinements?: ReportRefinement[]
}
