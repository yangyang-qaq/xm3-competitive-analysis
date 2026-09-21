/**
 * 知识图谱的数据层：把一份报告正文摊成**调研对象 — 品牌 — 维度 — 专家**
 * 四层的一张图，并给出一个确定性的布局。
 *
 * 为什么证据不成为节点
 * ================
 * 实测一份真报告的 `evidences` 有 **216 条**。216 个点加上连线铺在一屏里，
 * 得到的东西叫毛球，不叫图——**看不清的东西等于没画**。
 *
 * 所以证据不进节点，它进**边的权重**：`品牌 × 维度` 那条边上写着
 * "这个品牌在这个维度上有 27 条证据"。**聚合的是画法，不是数据**——
 * 每一条证据都还在，点那条边，右栏把它列出来。
 *
 * 四种边里只有一种带权重
 * ===================
 * 只有 `brand-dimension` 是有出处的：它数的是
 * `evidences[].matchedDimensions`，即**采到了什么**。
 * 另外三种（对象—品牌、对象—维度、对象—专家）的权重恒为 0，
 * 它们表达的是**这次调研安排了什么**：品牌名单、计划维度、派了谁。
 * 所以页面上把前者画实线、后者画细虚线——把"计划"和"采到"画成一样粗细，
 * 读者会把计划当成事实。相应地，那个 `weight === 0` 不是"没有数据"，
 * 而是"这条边不是计量出来的"，这一点必须由渲染端说明。
 *
 * 三条不变量（有测试守着）
 * ====================
 * 1. **维度节点的 `weight` 等于它相连的带权边之和。** 严格相等。
 * 2. **品牌节点的 `weight` 等于它进图的证据条数**，而它的边权之和
 *    **可能更大**，差额是**"多记的笔数"**：
 *    `Σ(每条证据命中的计划维度数 − 1)`。
 *
 *    注意这个差额**不是**"有几条证据命中了多个维度"。一条证据命中 3 个
 *    维度时，它多记 **2** 笔，而"多维度证据"只算 1 条。真报告上两种数法
 *    差过 1（Notion 那份：多维度证据 3 条，多记 4 笔）——所以两种说法
 *    不能混用，用错了就是又一个"同一个问题两个答案"。
 *
 *    这里踩过一次坑，值得写下来：初版把 1 与 2 合成了一条"点的大小等于
 *    边权之和"，测试也过了——**但那是碰巧**。当时的夹具里每条证据都只命中
 *    一个维度，于是两个定义给出同一个数。换成真报告（一份 6 维度的
 *    新能源充电桩调研）当场就分开了：品牌 45 条证据，六条边加起来 46。
 *
 *    分开之后为什么不干脆"把点的数也改成边权之和"？因为那个数会被页面
 *    读成属性名——"这个品牌有 46 条证据"——而它名下其实只有 45 条，
 *    点开列出来也是 45 行。**为了视觉上凑整而让文字说谎，是更坏的交换。**
 *    差额只有个位数百分比，圆的面积上看不出来；而 45 与 46 印在页面上
 *    是能一条条数出来的。
 *
 *    维度之所以不受影响：一条证据命中同一维度两次不会发生（除非
 *    `matchedDimensions` 自己重复），所以维度那一侧两个定义重合。
 * 3. **同一份正文两次构图、两次布局，结果逐字节相同。** 布局用 d3-force，
 *    节点初始位置由下标算出来（不用随机数），所以它是确定性的——
 *    这一点值得测，因为"每次打开图长得不一样"会让人以为数据在变。
 */
import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation } from 'd3-force'

import type { ReportBody, ReportEvidence } from '../types/report'

/** 画布尺寸。**布局与 `<svg viewBox>` 都读它**，两处各写一份迟早对不上。 */
export const GRAPH_WIDTH = 960
export const GRAPH_HEIGHT = 640

const SUBJECT_ID = 'subject'

export type GraphNodeKind = 'subject' | 'brand' | 'dimension' | 'expert'

/**
 * 边的种类。前三种是"安排"，最后一种是"采到"——
 * 判据见文件头：`weight > 0` 只可能是 `brand-dimension`。
 */
export type GraphLinkKind =
  | 'subject-brand'
  | 'subject-dimension'
  | 'subject-expert'
  | 'brand-dimension'

export interface GraphNode {
  /**
   * 带种类前缀的 id（`brand:Notion`）。**标签可以一样，id 不能**——
   * 一个品牌和一个维度同名时，两个节点会缩成一个。
   */
  id: string
  kind: GraphNodeKind
  label: string
  /**
   * 这个节点名下的**证据条数**。`subject` 是进图的总数，`expert` 恒为 0。
   *
   * 维度节点上它等于边权之和（不变量 1）；品牌节点上边权之和可能比它大，
   * 差额是"一证多维"多记的那些（不变量 2，见文件头）。**不要把它读成
   * "连线的粗细加起来"**——它是"几条证据"，能点开一条条数。
   */
  weight: number
  /** 一条证据都没命中的维度。**要画出来**（铁律 1：没有证据本身是个结论）。 */
  uncovered: boolean
  /** 专家节点才有：`L1`/`L2`/`L3`，用来给颜色分档。取不到时空串 */
  level: string
}

export interface GraphLink {
  /**
   * 端点用节点 id（字符串）。
   *
   * **传给 d3 的必须是副本**：`forceLink` 会就地把它换成节点对象引用，
   * 而返回给渲染端的那一份仍然按 `GraphLink` 声明着 string——
   * 类型在骗人，错误会出现在很远的画线代码里（`x1 = source.x` 得 undefined）。
   */
  source: string
  target: string
  kind: GraphLinkKind
  /** 证据条数。只有 `brand-dimension` 非 0 */
  weight: number
}

export interface ReportGraph {
  nodes: GraphNode[]
  links: GraphLink[]
  /**
   * 没能进图的证据，**按原因分开数**。两个数互斥、加起来就是总数
   * （先判品牌，品牌不在名单就不再往下判维度）。
   *
   * 为什么不合成一个"丢了多少条"：因为这两种丢法的**读法完全相反**。
   * - `droppedNoBrand` 大：品牌名单不全，是这次调研的口径问题；
   * - `droppedNoDimension` 大：证据没能归到任何计划维度上。
   *   真报告里见过一整份 216 条全落在这里，图上一条边都没有——
   *   那时读者最需要知道的恰恰是"为什么"，而一个合并数说不出为什么。
   *
   * 必须报出来。不报的话，图看起来就是全部证据的画像，而实际上它只画了
   * 能被归类的那部分——"图上一共 108 条"与"这份报告有 216 条证据"
   * 是两个数，读者有权知道差在哪。
   */
  droppedNoBrand: number
  /** 品牌在名单里、但一条**计划**维度都没命中 */
  droppedNoDimension: number
  /**
   * `droppedNoDimension` 的一个子集：**证据有 `matchedDimensions`，
   * 但名字一个都不在计划维度里**。
   *
   * 这是三种丢法里唯一一种指向**数据本身对不上**的（另两种是数据没归好类）：
   * 报告的维度名单与证据的维度标注不是同一套词。真报告里出现过一整份
   * 216 条全落在这里，而页面上"0 个维度"与"0 条连线"看起来只像"这次没采到"。
   */
  droppedUnknownDimension: number
}

/**
 * 由一份报告正文构图。
 *
 * 规则只有一条：**一条证据进图，当且仅当它的品牌在名单里、且命中至少一个
 * 计划维度。** 两个条件是与的关系，因为边是 `品牌 × 维度`——缺任何一边
 * 都落不到任何一条边上。落不到边上的证据既不能进品牌权重、也不能进维度权重，
 * 进了的话不变量 1 立刻不成立。
 */
export function buildGraph(body: ReportBody): ReportGraph {
  const brands = unique(body.brands ?? [])
  const dimensions = unique(body.dimensions ?? [])
  const evidences = body.evidences ?? []
  const planned: Planned = { brands: new Set(brands), dimensions: new Set(dimensions) }

  const brandWeight = new Map<string, number>()
  const dimensionWeight = new Map<string, number>()
  /**
   * 品牌 → 维度 → 条数。
   *
   * **嵌套两层 Map，而不是 `"品牌|维度"` 这种复合键。** 复合键需要一个
   * "数据里不会出现的分隔符"，而那个假设总有一天不成立：这里第一版用的是
   * 空格，理由是"品牌名和维度名里都可能出现连字符"，但同一个理由恰好也
   * 否掉了空格——维度名是中文短语，完全可以带空格（"AI 编程"）。
   * 那时 `split` 会把它切成三段，维度只剩 "AI"：**边上的数字仍然是对的，
   * 点开却少了一批证据，而且不报错**。嵌套结构里没有这个假设可违反。
   */
  const pairs = new Map<string, Map<string, number>>()
  let droppedNoBrand = 0
  let droppedNoDimension = 0
  let droppedUnknownDimension = 0

  for (const evidence of evidences) {
    // 品牌先判一次，只为**把丢法归类**；判据本身仍然只有 `plannedMatches` 一份
    // （它内部也判品牌）。这里多一次 Set 查询，换来的是"两个丢法数互斥、
    // 加起来正好是总数"——否则一个同时缺品牌又缺维度的证据会被数两遍。
    if (!planned.brands.has(evidence.brand)) {
      droppedNoBrand += 1
      continue
    }
    const match = plannedMatches(evidence, planned)
    if (!match) {
      droppedNoDimension += 1
      if ((evidence.matchedDimensions ?? []).length > 0) droppedUnknownDimension += 1
      continue
    }
    brandWeight.set(match.brand, (brandWeight.get(match.brand) ?? 0) + 1)
    for (const dim of match.dimensions) {
      dimensionWeight.set(dim, (dimensionWeight.get(dim) ?? 0) + 1)
      let byDimension = pairs.get(match.brand)
      if (!byDimension) {
        byDimension = new Map<string, number>()
        pairs.set(match.brand, byDimension)
      }
      byDimension.set(dim, (byDimension.get(dim) ?? 0) + 1)
    }
  }

  const experts = expertNodes(body)
  const matchedTotal = [...brandWeight.values()].reduce((sum, value) => sum + value, 0)

  const nodes: GraphNode[] = [
    {
      id: SUBJECT_ID,
      kind: 'subject',
      label: body.subject || body.query || '本次调研',
      weight: matchedTotal,
      uncovered: false,
      level: '',
    },
    ...brands.map((brand) => ({
      id: `brand:${brand}`,
      kind: 'brand' as const,
      label: brand,
      weight: brandWeight.get(brand) ?? 0,
      // 品牌一条证据都没有不算"未覆盖"——覆盖与否是维度那一侧的问题。
      uncovered: false,
      level: '',
    })),
    ...dimensions.map((dim) => ({
      id: `dimension:${dim}`,
      kind: 'dimension' as const,
      label: dim,
      weight: dimensionWeight.get(dim) ?? 0,
      uncovered: (dimensionWeight.get(dim) ?? 0) === 0,
      level: '',
    })),
    ...experts,
  ]

  // 边的顺序：品牌、维度、专家三类"安排"边按名单顺序，带权重的边最后。
  // 带权重的边按 `pairs` 的插入顺序——Map 保序，而插入顺序由证据列表决定，
  // 所以同一份正文永远得到同一个顺序（不变量 2 靠这个）。
  const weighted: GraphLink[] = []
  for (const [brand, byDimension] of pairs) {
    for (const [dim, weight] of byDimension) {
      weighted.push({
        source: `brand:${brand}`,
        target: `dimension:${dim}`,
        kind: 'brand-dimension',
        weight,
      })
    }
  }

  const links: GraphLink[] = [
    ...brands.map((brand) => link(SUBJECT_ID, `brand:${brand}`, 'subject-brand')),
    ...dimensions.map((dim) => link(SUBJECT_ID, `dimension:${dim}`, 'subject-dimension')),
    ...experts.map((node) => link(SUBJECT_ID, node.id, 'subject-expert')),
    ...weighted,
  ]

  return { nodes, links, droppedNoBrand, droppedNoDimension, droppedUnknownDimension }
}

function link(source: string, target: string, kind: GraphLinkKind): GraphLink {
  return { source, target, kind, weight: 0 }
}

interface Planned {
  brands: Set<string>
  dimensions: Set<string>
}

/**
 * **一条证据进图的唯一判据**：品牌在名单里，且命中至少一个计划维度。
 * 不满足返回 `null`。
 *
 * 这个函数存在的全部理由，是让"什么算进图"只被写一遍。它原来被写了两遍：
 * `buildGraph` 一遍（品牌在名单 **且** 有命中维度），`pickEvidences` 一遍
 * （只按调用方传进来的字段过滤）。前者紧、后者松，于是在真报告上分叉了——
 * 品牌节点写着 **45** 条，点开却列出 **72** 行，多出来的那些是没命中任何
 * 计划维度、压根不在图上的证据。**两个数字各自都"没错"**，是"这个品牌有
 * 几条证据"这个问题有两个答案。
 *
 * 现在两边都调它，并且 `pickEvidences` 的入参里**必须**带上名单与维度——
 * 想绕过这条判据就得先显式地把名单传进来，做不到了。
 */
function plannedMatches(
  evidence: ReportEvidence,
  planned: Planned,
): { brand: string; dimensions: string[] } | null {
  if (!planned.brands.has(evidence.brand)) return null
  const dimensions = (evidence.matchedDimensions ?? []).filter((dim) =>
    planned.dimensions.has(dim),
  )
  if (dimensions.length === 0) return null
  return { brand: evidence.brand, dimensions }
}

/**
 * 相连的带权边之和比节点自身的证据条数**多出来的那部分**（不变量 2）。
 *
 * 维度节点上恒为 0；品牌节点上等于它名下**多记的笔数**：
 * `Σ(每条证据命中的计划维度数 − 1)`。
 *
 * 存在的意义是让页面**能把差额说出来**。不说的话，读者把六条边上的
 * 数字一加，得到 46，而品牌写着 45——他会以为自己数错了，或者以为
 * 页面上有个 bug，然后不再相信这张图上的任何数字。
 * 一个能被解释的差，是可以接受的；一个不能解释的差，会让人放弃全部。
 */
export function multiDimensionHits(graph: ReportGraph, nodeId: string): number {
  const incident = graph.links
    .filter(
      (edge) =>
        edge.kind === 'brand-dimension' && (edge.source === nodeId || edge.target === nodeId),
    )
    .reduce((sum, edge) => sum + edge.weight, 0)
  const node = graph.nodes.find((item) => item.id === nodeId)
  return node ? Math.max(0, incident - node.weight) : 0
}

/**
 * 专家节点。两个来源对不上时**以 `team` 为准**：
 * 它记录的是这次派了谁，而 `thoughts` 只记录谁说过话——
 * 一句话没说的专家也参与了，不能因为他安静就从图上消失。
 * 名字从 `thoughts` 里找（那里有 `expertName`），找不到就退回 id。
 *
 * **不为了名字多发起一个请求**（`/api/experts` 那一份会更好看）：
 * 这一页只依赖 `GET /api/reports/{id}`，多一个请求就多一种
 * "报告打开了但那个请求还没回来"的中间态。id 难看但正确；
 * 空白好看，但是错的。
 */
function expertNodes(body: ReportBody): GraphNode[] {
  const team = body.team ?? {}
  const names = new Map<string, string>()
  for (const thought of body.thoughts ?? []) {
    if (thought.expertId && thought.expertName && !names.has(thought.expertId)) {
      names.set(thought.expertId, thought.expertName)
    }
  }

  const ids: string[] = []
  for (const id of [...(team.lead ?? []), ...(team.strategists ?? []), ...(team.executors ?? [])]) {
    if (id && !ids.includes(id)) ids.push(id)
  }

  return ids.map((id) => ({
    id: `expert:${id}`,
    kind: 'expert' as const,
    label: names.get(id) ?? id,
    weight: 0,
    uncovered: false,
    level: /^(L[123])-/.exec(id)?.[1] ?? '',
  }))
}

function unique(values: string[]): string[] {
  return [...new Set(values.filter((value) => value !== ''))]
}

// ============================================================
// 布局
// ============================================================

export interface PositionedNode extends GraphNode {
  x: number
  y: number
}

export interface GraphLayout {
  nodes: PositionedNode[]
  /** 端点仍是 id 字符串。**画这个**，不是传给 d3 的那份 */
  links: GraphLink[]
  width: number
  height: number
}

/**
 * 力的布局。**同步跑到收敛，不做动画、不用 `requestAnimationFrame`。**
 *
 * 三个理由：
 * 1. **确定性**。每次都从同一个初始状态算起，所以两次打开位置相同。
 *    挂一个逐帧动画的话，"这张图长什么样"取决于它被看了多久。
 * 2. **没有中间态**。逐帧渲染会有若干个"节点还在飞"的画面，
 *    每一帧都要 `setState` 一次——25 个节点 300 帧就是 300 次渲染。
 * 3. 收敛后的位置是**这个布局问题的答案**，动画只是它的一种播放方式。
 *    真要播放，也该在算完之后播，而不是一边算一边播。
 *
 * 初始位置按**下标**摊在一个圆上，不用随机数。`forceSimulation` 只在节点
 * 缺 `x`/`y` 时才自己按黄金角铺开，显式给位置就绕开了它；各力函数里
 * 唯一的随机源是 `jiggle`，而它取的是 d3 自己那条**固定种子的 LCG**
 * （`d3-force/src/lcg.js`），不是 `Math.random`。所以这里有确定性可言，
 * 也有测试守着（见 `graph.test.ts`）。
 */
export function layoutGraph(
  graph: ReportGraph,
  options: { width?: number; height?: number; iterations?: number } = {},
): GraphLayout {
  const width = options.width ?? GRAPH_WIDTH
  const height = options.height ?? GRAPH_HEIGHT
  const iterations = options.iterations ?? 300
  const cx = width / 2
  const cy = height / 2

  const nodes: PositionedNode[] = graph.nodes.map((node, index) => {
    // 半径要按总数给足：全挤在一起时排斥力得花很多轮才散得开，
    // 而"跑了 300 轮还没散开"看起来就像布局坏了。
    const angle = (2 * Math.PI * index) / Math.max(1, graph.nodes.length)
    const radius = Math.min(width, height) * 0.34
    return { ...node, x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) }
  })

  const simulationLinks = graph.links.map((item) => ({ ...item }))

  const simulation = forceSimulation(nodes)
    .force(
      'link',
      forceLink<PositionedNode, (typeof simulationLinks)[number]>(simulationLinks)
        .id((node) => node.id)
        // 带权重的边（品牌↔维度）拉得紧：它们是图里唯一有度量含义的边，
        // 让品牌与它真正采到东西的维度靠近，图才有可读的结构。
        // "安排"类边只给一点点力，作用是别让节点飘出画布。
        .distance((item) => (item.kind === 'brand-dimension' ? 90 : 150))
        .strength((item) => (item.kind === 'brand-dimension' ? 0.35 : 0.06)),
    )
    .force('charge', forceManyBody().strength(-260))
    .force('center', forceCenter(cx, cy))
    .force(
      'collide',
      // 半径与 `weight` 同向，与页面上画的点一致——不一致的话，
      // 大点会压在小点身上，看起来像"这两个节点粘住了"。
      forceCollide<PositionedNode>().radius((node) => nodeRadius(node) + 4),
    )
    .stop()

  for (let i = 0; i < iterations; i += 1) simulation.tick()

  return { nodes, links: graph.links, width, height }
}

/**
 * 点画多大。**唯一的一处定义**——排斥半径、碰撞半径、SVG 的 `<circle r>`
 * 都读它。三处各写一个公式的话，点会互相压住或者离得老远，
 * 而那是"看起来有点怪"，不会有人报错。
 */
export function nodeRadius(node: Pick<GraphNode, 'kind' | 'weight'>): number {
  if (node.kind === 'subject') return 20
  if (node.kind === 'expert') return 7
  // 证据条数开平方：**面积**与条数成正比，视觉上才是"这条比那条大一倍"。
  // 直接用条数当半径会得到"大两倍看起来大四倍"。
  return 6 + Math.sqrt(Math.max(0, node.weight)) * 1.6
}

// ============================================================
// 选中一块之后要看哪些证据
// ============================================================

/**
 * 取某个节点或某条边背后的证据。`brand` 与 `dimension` 两个条件是**与**。
 *
 * `brands` / `dimensions` 是**这份报告的计划名单**（`body.brands` /
 * `body.dimensions`），必填。它们不是筛选条件，而是让这里能复用
 * `plannedMatches` 那条唯一判据——**图上说有几条，这里就列出几条**。
 * 一份不传名单的实现会退化成"这个品牌下的全部证据"，而那个数比图上的大：
 * 真报告上 72 vs 45，读者点开一看就再也不信这张图了。
 *
 * 排序：可信度降序。读者点开一格是想看最值得看的那一条。
 * 并列时保持原顺序（`sort` 在 V8 上稳定），所以两次点开顺序一致。
 */
export function pickEvidences(
  evidences: ReportEvidence[],
  filter: {
    brands: string[]
    dimensions: string[]
    brand?: string
    dimension?: string
  },
): ReportEvidence[] {
  const planned: Planned = {
    brands: new Set(filter.brands),
    dimensions: new Set(filter.dimensions),
  }
  const picked = evidences.filter((evidence) => {
    const match = plannedMatches(evidence, planned)
    if (!match) return false
    if (filter.brand !== undefined && match.brand !== filter.brand) return false
    if (filter.dimension !== undefined && !match.dimensions.includes(filter.dimension)) return false
    return true
  })
  return [...picked].sort((left, right) => right.credibility - left.credibility)
}
