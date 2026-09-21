/**
 * 知识图谱：这份报告把哪些品牌、在哪些维度上、用谁的力气、采到了什么。
 *
 * 画法与数据的关系写在 `lib/graph.ts` 的文件头，这里只说**页面上必须交代的
 * 两件事**——不交代的话，这张图会被当成"全部证据的一张画像"，
 * 而它其实只是"能归类的那部分证据的一张画像"：
 *
 * 1. **实线与虚线的区别。** 带数字的实线（品牌↔维度）是采到的，
 *    没有数字的虚线是这次调研的安排。两者在数据上就有区别
 *    （`weight > 0` 只可能是前者），页面上必须画得出来。
 * 2. **没进图的证据有多少条。** 品牌不在名单里、或者一条维度都没命中的
 *    那些证据进不了这张图（图上没有它们的位置）。这个数印在图下面，
 *    否则"图上一共 108 条"会被读成"这份报告只有 108 条证据"。
 *
 * 布局是**算完再画**的（`layoutGraph` 同步跑 300 轮），不是逐帧动画。
 * 理由见 `layoutGraph` 的注释：确定性、没有中间态、少 300 次渲染。
 */
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Panel } from '../components/primitives/Panel'
import { Empty, ErrorNote, Loading } from '../components/primitives/States'
import { useAsync } from '../hooks/useAsync'
import { api } from '../lib/api'
import {
  buildGraph,
  layoutGraph,
  multiDimensionHits,
  nodeRadius,
  pickEvidences,
} from '../lib/graph'
import type { GraphLayout, GraphNodeKind, PositionedNode, ReportGraph } from '../lib/graph'
import { CREDIBILITY_TIER_STYLE, credibilityTier, formatInt } from '../lib/format'
import type { ReportEvidence, TeamRoster } from '../types/report'

const KIND_LABEL: Record<GraphNodeKind, string> = {
  subject: '调研对象',
  brand: '品牌',
  dimension: '维度',
  expert: '专家',
}

/**
 * 点的颜色。**种类决定色相，"没采到"只改描边**——两件事分开表达，
 * 否则一个没采到的维度既丢了种类、又多了个说不清含义的颜色。
 */
const NODE_FILL: Record<GraphNodeKind, string> = {
  subject: 'fill-brand',
  brand: 'fill-l2',
  dimension: 'fill-fg-faint',
  expert: 'fill-l3',
}

type Selection = { kind: 'node'; id: string } | { kind: 'link'; index: number }

export default function GraphPage() {
  const { reportId = '' } = useParams()
  const { data, error, loading, reload } = useAsync(() => api.getReport(reportId), [reportId])
  const [selection, setSelection] = useState<Selection | null>(null)

  const body = data?.data
  const graph = useMemo(() => buildGraph(body ?? {}), [body])
  // **只算一次。** 放进依赖 `graph` 的 `useMemo` 里：布局是纯函数，
  // 但它在 25 个节点上要跑 300 轮，每次渲染重算一遍是白花的力气，
  // 而"图在别的状态变化时抖一下"是那种没人会去查的怪现象。
  const layout = useMemo(() => layoutGraph(graph), [graph])
  const byId = useMemo(() => new Map(layout.nodes.map((node) => [node.id, node])), [layout])

  // 选中一个点之后，与它无关的点和线都压暗。不压暗的话，
  // 读者在一条 20 多条线的图上找不到"我刚才点的那个连到哪儿"。
  const focused = useMemo(() => {
    if (selection?.kind === 'node') {
      const neighbors = new Set<string>([selection.id])
      for (const edge of layout.links) {
        if (edge.source === selection.id) neighbors.add(edge.target)
        if (edge.target === selection.id) neighbors.add(edge.source)
      }
      return neighbors
    }
    if (selection?.kind === 'link') {
      const edge = layout.links[selection.index]
      return edge ? new Set([edge.source, edge.target]) : null
    }
    return null
  }, [selection, layout])

  if (loading && !data) {
    return (
      <div className="mx-auto max-w-3xl px-8 py-16">
        <Loading what="报告" />
      </div>
    )
  }
  if (error || !body) {
    return (
      <div className="mx-auto max-w-3xl px-8 py-16">
        {error ? (
          <ErrorNote error={error} onRetry={reload} />
        ) : (
          <Empty
            title="这份报告不在库里"
            hint={
              <>
                图谱是从报告正文画的。去{' '}
                <Link to="/library" className="text-brand hover:underline">
                  我的调研
                </Link>{' '}
                找找。
              </>
            }
          />
        )}
      </div>
    )
  }

  const brands = layout.nodes.filter((node) => node.kind === 'brand')
  const dimensions = layout.nodes.filter((node) => node.kind === 'dimension')

  return (
    <div className="flex h-screen flex-col">
      <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-line bg-panel px-4 py-2">
        <Link to={`/report/${reportId}`} className="shrink-0 text-[12px] text-brand hover:underline">
          ← 回报告
        </Link>
        <h1 className="shrink-0 text-sm text-fg">知识图谱</h1>
        <span className="shrink-0 font-mono text-[11px] text-fg-faint">{reportId}</span>
        <span className="shrink-0 text-[11px] text-fg-faint">
          {formatInt(brands.length)} 个品牌 · {formatInt(dimensions.length)} 个维度 ·{' '}
          {formatInt(layout.links.filter((edge) => edge.kind === 'brand-dimension').length)} 条
          有证据的连线
        </span>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_minmax(0,360px)]">
        <div className="relative min-h-0">
          {/* `role="group"` 而不是 `img`：里面有可点可聚焦的子元素，
              标成 img 会让读屏软件把整块当一个静态图，节点就摸不到了。 */}
          <svg
            viewBox={`0 0 ${layout.width} ${layout.height}`}
            className="h-full w-full"
            role="group"
            aria-label="调研对象、品牌、维度与专家的关系图"
          >
            {/* 点空白处取消选中。铺在最底下的那块透明矩形负责接这个点击——
                没有它的话，只有"点到某个圆圈上"才算点击，而用户会觉得
                "点了空白也该取消"。 */}
            <rect
              x={0}
              y={0}
              width={layout.width}
              height={layout.height}
              className="fill-transparent"
              onClick={() => setSelection(null)}
            />

            {/* 每条边画两遍：**看得见的那一遍不接事件，接事件的那一遍看不见。**
                理由是两个宽度要求正好相反——线本身要细（1px 的虚线已经很显眼），
                而可点的目标要粗（1px 的线几乎点不中，12px 才够）。
                用同一个元素做不到，所以拆开；热区那遍画在所有可见线之上，
                于是"鼠标在线附近"总是落在热区里，提示框也只有一条路径。 */}
            {layout.links.map((edge, index) => {
              const from = byId.get(edge.source)
              const to = byId.get(edge.target)
              if (!from || !to) return null
              const weighted = edge.kind === 'brand-dimension'
              const active = selection?.kind === 'link' && selection.index === index
              const dimmed =
                focused !== null &&
                !active &&
                !(focused.has(edge.source) && focused.has(edge.target))
              const hint = `${from.label} → ${to.label}${
                weighted ? ` · ${edge.weight} 条证据` : ' · 这次调研的安排'
              }`
              return (
                <g key={`${edge.kind}:${edge.source}->${edge.target}`}>
                  <line
                    x1={from.x}
                    y1={from.y}
                    x2={to.x}
                    y2={to.y}
                    // 粗细只表达**有证据的边**。没有权重的边一律 1px 虚线——
                    // 把它也按粗细画出来，读者会把"计划"当成"采到"。
                    strokeWidth={weighted ? Math.min(8, 1 + edge.weight) : 1}
                    strokeDasharray={weighted ? undefined : '4 4'}
                    className={[
                      weighted ? 'stroke-brand' : 'stroke-line-strong',
                      active ? 'stroke-warn' : '',
                      dimmed ? 'opacity-20' : 'opacity-70',
                    ].join(' ')}
                  />
                  <line
                    x1={from.x}
                    y1={from.y}
                    x2={to.x}
                    y2={to.y}
                    strokeWidth={12}
                    className="cursor-pointer stroke-transparent"
                    onClick={() => setSelection({ kind: 'link', index })}
                  >
                    <title>{hint}</title>
                  </line>
                </g>
              )
            })}

            {layout.nodes.map((item) => (
              <GraphNodeMark
                key={item.id}
                node={item}
                selected={selection?.kind === 'node' && selection.id === item.id}
                dimmed={focused !== null && !focused.has(item.id)}
                onPick={() => setSelection({ kind: 'node', id: item.id })}
              />
            ))}
          </svg>
        </div>

        <Panel
          title="详情"
          aside={
            selection && (
              <button
                type="button"
                onClick={() => setSelection(null)}
                className="text-[11px] text-fg-faint hover:text-fg"
              >
                取消选中
              </button>
            )
          }
          scroll
          className="border-l border-line"
        >
          <Detail
            selection={selection}
            graph={graph}
            layout={layout}
            byId={byId}
            // 名单与维度一起传下去：证据的取舍要跟图上用的是同一条判据。
            evidenceFilter={{
              brands: body.brands ?? [],
              dimensions: body.dimensions ?? [],
            }}
            evidences={body.evidences ?? []}
            team={body.team ?? {}}
          />
        </Panel>
      </div>
    </div>
  )
}

// ============================================================
// 图上的一个点
// ============================================================

function GraphNodeMark({
  node,
  selected,
  dimmed,
  onPick,
}: {
  node: PositionedNode
  selected: boolean
  dimmed: boolean
  onPick: () => void
}) {
  const radius = nodeRadius(node)
  return (
    <g
      className={['cursor-pointer', dimmed ? 'opacity-20' : ''].join(' ')}
      onClick={onPick}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') onPick()
      }}
    >
      <title>
        {`${KIND_LABEL[node.kind]}：${node.label}`}
        {node.weight > 0 ? ` · ${node.weight} 条证据` : ''}
        {node.uncovered ? ' · 没有采到任何证据' : ''}
      </title>
      <circle
        cx={node.x}
        cy={node.y}
        r={radius}
        // 没证据的维度用**虚线描边**：实心圆会被读成"有东西"。
        // 这是铁律 1 在图上的落点——没有证据本身是个结论，要让它在图上看得见。
        strokeDasharray={node.uncovered ? '3 3' : undefined}
        strokeWidth={selected ? 3 : node.uncovered ? 2 : 1}
        className={[
          NODE_FILL[node.kind],
          node.uncovered ? 'stroke-warn' : selected ? 'stroke-warn' : 'stroke-panel',
        ].join(' ')}
      />
      <text
        x={node.x}
        y={node.y + radius + 11}
        textAnchor="middle"
        className="fill-fg-muted text-[11px]"
        // 标签不参与点击，否则点标签会落到"没点中"上。
        pointerEvents="none"
      >
        {node.label.length > 12 ? `${node.label.slice(0, 12)}…` : node.label}
      </text>
    </g>
  )
}

// ============================================================
// 右栏
// ============================================================

function Detail({
  selection,
  graph,
  layout,
  byId,
  evidenceFilter,
  evidences,
  team,
}: {
  selection: Selection | null
  graph: ReportGraph
  layout: GraphLayout
  byId: Map<string, PositionedNode>
  evidenceFilter: { brands: string[]; dimensions: string[] }
  evidences: ReportEvidence[]
  team: TeamRoster
}) {
  // 调研对象的标签只在这里取一次：品牌那一栏要拿它比"这个品牌是不是就是对象本身"。
  const subjectLabel = layout.nodes.find((item) => item.kind === 'subject')?.label ?? ''

  if (!selection) {
    return (
      <div className="space-y-3 text-[11px] leading-relaxed text-fg-muted">
        <p>点一个节点或一条线看它背后是什么。</p>
        <ul className="space-y-1.5 border-t border-line pt-2.5">
          <Legend className="fill-brand" text="调研对象" />
          <Legend className="fill-l2" text="品牌" />
          <Legend className="fill-fg-faint" text="维度" />
          <Legend className="fill-l3" text="专家" />
        </ul>
        <ul className="space-y-1.5 border-t border-line pt-2.5 text-fg-faint">
          <li>
            <span className="mr-1.5 inline-block h-0.5 w-6 translate-y-[-2px] bg-brand align-middle" />
            实线（有粗有细）= <span className="text-fg-muted">采到了</span>，粗细就是证据条数
          </li>
          <li>
            <span className="mr-1.5 inline-block h-0.5 w-6 translate-y-[-2px] border-t border-dashed border-line-strong align-middle" />
            虚线（一律 1px）= <span className="text-fg-muted">这次调研的安排</span>，不是计量出来的
          </li>
          <li>
            <span className="mr-1.5 inline-block size-3 translate-y-[2px] rounded-full border-2 border-dashed border-warn align-middle" />
            虚线描边的维度 = 一条证据都没采到
          </li>
        </ul>
        {(graph.droppedNoBrand > 0 || graph.droppedNoDimension > 0) && (
          <div className="space-y-1 border-t border-line pt-2.5 text-fg-faint">
            <p>图上没有它们的位置，所以下面这些证据不在图里：</p>
            {graph.droppedNoBrand > 0 && (
              <p>
                · <span className="text-fg-muted">{formatInt(graph.droppedNoBrand)}</span> 条
                品牌不在这次调研的名单里
              </p>
            )}
            {graph.droppedNoDimension > 0 && (
              <p>
                · <span className="text-fg-muted">{formatInt(graph.droppedNoDimension)}</span> 条
                一条计划维度都没命中
              </p>
            )}
            {/* 这一条是**数据对不上**的信号，不是"这次没采到"。不单独说出来的话，
                "0 个维度 + 0 条连线"看起来只像这次调研没收获。
                两种"对不上"要说成两句不同的话：名单是空的、和名单是另一套词，
                读者要做的事不一样（前者是这份报告没有维度这一层，
                后者是两份数据用了不同的说法）。 */}
            {graph.droppedUnknownDimension > 0 &&
              (evidenceFilter.dimensions.length === 0 ? (
                <p className="text-warn">
                  · 其中 {formatInt(graph.droppedUnknownDimension)} 条
                  <span className="text-fg">标注了维度，但这份报告根本没有计划维度</span>
                  ——正文里的 `dimensions` 是空的。所以图上一条实线都不会有：
                  不是没采到，是这次调研没有"按维度看"这一层。
                </p>
              ) : (
                <p className="text-warn">
                  · 其中 {formatInt(graph.droppedUnknownDimension)} 条
                  <span className="text-fg">标注了维度、但名字都不在报告的计划维度里</span>
                  ——这份报告的维度名单和证据的标注不是同一套词。这不是"没采到"，
                  是两份数据对不上。
                </p>
              ))}
          </div>
        )}
      </div>
    )
  }

  if (selection.kind === 'link') {
    const edge = layout.links[selection.index]
    if (!edge) return <p className="text-[11px] text-fg-faint">这条线已经不在图上了。</p>
    const from = byId.get(edge.source)
    const to = byId.get(edge.target)
    const rows =
      edge.kind === 'brand-dimension' && from && to
        ? pickEvidences(evidences, { ...evidenceFilter, brand: from.label, dimension: to.label })
        : []

    return (
      <div className="space-y-2 text-[11px]">
        <p className="text-fg">
          {from?.label ?? edge.source} → {to?.label ?? edge.target}
        </p>
        {edge.kind === 'brand-dimension' ? (
          <>
            <p className="text-fg-muted">
              这个品牌在这个维度上采到 <span className="text-fg">{formatInt(edge.weight)}</span> 条证据。
            </p>
            <EvidenceList rows={rows} />
          </>
        ) : (
          <p className="text-fg-faint">
            这条线是<span className="text-fg-muted">安排</span>，不是计量：它来自这次调研的
            品牌名单、计划维度与派出的专家，没有"几条证据"这回事。虚线画的就是这一点。
          </p>
        )}
      </div>
    )
  }

  const node = byId.get(selection.id)
  if (!node) return <p className="text-[11px] text-fg-faint">这个节点已经不在图上了。</p>

  // 品牌节点独有：边权之和比证据条数多出来的部分。见 `lib/graph.ts` 不变量 2。
  const surplus = node.kind === 'brand' ? multiDimensionHits(graph, node.id) : 0
  // 真报告里出现过：品牌名单把调研对象自己也列了进去，于是图上两个同名点。
  const selfNamed = node.kind === 'brand' && node.label === subjectLabel

  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex items-baseline gap-2">
        <span className="rounded bg-raised px-1.5 py-0.5 text-fg-muted">
          {KIND_LABEL[node.kind]}
        </span>
        <span className="min-w-0 flex-1 truncate text-[13px] text-fg" title={node.label}>
          {node.label}
        </span>
      </div>

      {node.kind === 'subject' && (
        <p className="leading-relaxed text-fg-muted">
          这张图围着它展开：{formatInt(layout.nodes.filter((n) => n.kind === 'brand').length)} 个品牌、
          {formatInt(layout.nodes.filter((n) => n.kind === 'dimension').length)} 个维度、
          {formatInt(layout.nodes.filter((n) => n.kind === 'expert').length)} 位专家。
          进图的证据共 <span className="text-fg">{formatInt(node.weight)}</span> 条。
        </p>
      )}

      {node.kind === 'brand' && (
        <>
          <p className="text-fg-muted">
            这个品牌名下有 <span className="text-fg">{formatInt(node.weight)}</span> 条证据进图。
          </p>
          {/* 与图上、与下面的列表**用的是同一条判据**（`lib/graph.ts` 的
              `plannedMatches`）：所以这个数、边的粗细、列出来的行数三者一致。
              曾经不一致过——这里写 45，列表列出 72。 */}
          {/* 差额必须解释。不解释的话，读者把几条边上的数字一加，发现对不上，
              就会开始怀疑图上所有的数。 */}
          {surplus > 0 && (
            <p className="text-fg-faint">
              有几条证据同时命中了多个维度，每条每命中一个维度就记一笔，
              一共多记了 <span className="text-fg-muted">{formatInt(surplus)}</span> 笔——
              所以它那几条边上的数字加起来是{' '}
              {formatInt(node.weight + surplus)}。差额不是错。
            </p>
          )}
          {selfNamed && (
            <p className="text-fg-faint">
              这个"品牌"与调研对象同名：报告的品牌名单把调研对象自己也列进去了，
              所以图上会看到两个同名的点（大的那个是对象，小的是名单里的一项）。
            </p>
          )}
          <EvidenceList
            rows={pickEvidences(evidences, { ...evidenceFilter, brand: node.label })}
          />
        </>
      )}

      {node.kind === 'dimension' && (
        <>
          {node.uncovered ? (
            <p className="text-warn">
              这个维度一条证据都没采到。它在图上是个<span className="text-fg">结论</span>，
              不是一个漏画：报告的质量门会按它算维度覆盖率。
            </p>
          ) : (
            <p className="text-fg-muted">
              这个维度上有 <span className="text-fg">{formatInt(node.weight)}</span> 条证据。
            </p>
          )}
          <EvidenceList
            rows={pickEvidences(evidences, { ...evidenceFilter, dimension: node.label })}
          />
        </>
      )}

      {node.kind === 'expert' && <ExpertDetail node={node} team={team} />}
    </div>
  )
}

function ExpertDetail({ node, team }: { node: PositionedNode; team: TeamRoster }) {
  // 节点 id 是 `expert:L2-003`，路由参数要的是后半截。**只在这里剥一次**：
  // 三处各写一次 `replace` 的话，改前缀时漏掉一处，那处会拿整个 id 去查，
  // 得到"查无此人"——而"这位专家没有报告"看起来也像正常结果。
  const expertId = node.id.slice('expert:'.length)
  const role = team.lead?.includes(expertId)
    ? '决策层 · 定边界'
    : team.strategists?.includes(expertId)
      ? '战略层 · 切维度'
      : '执行层 · 取证'

  return (
    <>
      <p className="text-fg-muted">这次的分工：{role}</p>
      <p className="font-mono text-[11px] text-fg-faint">{expertId}</p>
      <Link to={`/experts/${expertId}`} className="inline-block text-brand hover:underline">
        看这位专家的档案 →
      </Link>
      <p className="leading-relaxed text-fg-faint">
        专家节点上<span className="text-fg-muted">没有证据条数</span>：
        证据不按专家归属记录，硬算一个出来就是编数据。连线只表示"这次派了他"。
      </p>
    </>
  )
}

function EvidenceList({ rows }: { rows: ReportEvidence[] }) {
  if (rows.length === 0) {
    return <p className="text-fg-faint">这里没有证据。</p>
  }
  return (
    <ul className="space-y-1.5 border-t border-line pt-2">
      {rows.slice(0, 30).map((row) => (
        <li key={row.evidenceId} className="flex items-start gap-2">
          <span
            className={[
              'shrink-0 rounded px-1 py-0.5 font-mono text-[10px]',
              CREDIBILITY_TIER_STYLE[credibilityTier(row.credibility)],
            ].join(' ')}
            title="可信度 0–100。分档阈值与后端 evidence/credibility.py 一致"
          >
            {row.credibility}
          </span>
          <a
            href={row.url}
            target="_blank"
            rel="noreferrer noopener"
            className="min-w-0 flex-1 leading-snug text-fg-muted hover:text-brand"
            title={`${row.title}\n${row.url}`}
          >
            {row.title || row.url}
            {row.degraded && (
              <span className="ml-1 text-warn" title="正文没抓到，只有搜索摘要">
                · 降级
              </span>
            )}
          </a>
        </li>
      ))}
      {rows.length > 30 && (
        <li className="text-fg-faint">只列出前 30 条，共 {formatInt(rows.length)} 条。</li>
      )}
    </ul>
  )
}

function Legend({ className, text }: { className: string; text: string }) {
  return (
    <li className="flex items-center gap-2">
      <span className={['inline-block size-3 rounded-full', className].join(' ')} />
      <span>{text}</span>
    </li>
  )
}
