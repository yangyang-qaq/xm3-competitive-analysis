/**
 * 报告页：一份跑完的报告。
 *
 * 三栏是照三个问题分的
 * ------------------
 *   左  —— **这份报告有什么**（目录）
 *   中  —— **它说了什么**（正文、图表、结构化块、附录）
 *   右  —— **它凭什么这么说**（证据）
 *
 * 右栏不是附录，是**随时可查的对照**。正文里每个角标点下去，
 * 右边就滚到那条证据并高亮——这个联动是这一页存在的形式本身。
 * 把证据做成正文下面的表格也能看，但读者就得在"看到一句话"和
 * "查这句话的依据"之间来回滚动十几屏，于是没人会去查。而
 * **没人去查的证据等于没写**。
 *
 * `minmax(0, 1fr)` 而不是 `1fr`
 * --------------------------
 * grid 项的默认 `min-width` 是 `auto`，意思是"不小于内容宽度"。
 * 正文里有宽表格（功能矩阵、定价表），用 `1fr` 时那一列会被内容
 * 撑宽、把整个三栏布局挤出去，而 `overflow-x-auto` 永远不会生效——
 * 因为列本身跟着内容一起变宽了。`minmax(0, 1fr)` 把下限按回 0。
 *
 * 深化之后**本地替换，不重新拉整份报告**
 * ----------------------------------
 * `/refine` 返回的就是改完的那一节，外加更新过的 `quality` /
 * `completeness` / `feedbackCount`。重新 `getReport` 一遍会丢掉
 * 读者的滚动位置（页面高度会变），而且在几十秒的等待之后再来一次
 * 整页 loading，看起来像是什么都没发生。
 *
 * 代价是本地这份会和服务器上有短暂的出入。所以补丁里带上 `reportId`：
 * 从 A 报告跳到 B 报告时，A 的补丁必须失效。不带的话 B 会显示 A 改过的
 * 那一节，而**那一节看上去完全正常**。
 */
import { useCallback, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Empty, ErrorNote, Loading } from '../components/primitives/States'
import { EvidenceAside } from '../components/report/EvidenceAside'
import { ReportAppendix } from '../components/report/ReportAppendix'
import { ReportAudit } from '../components/report/ReportAudit'
import { ReportCatalog } from '../components/report/ReportCatalog'
import { ReportCharts } from '../components/report/ReportCharts'
import { ReportClaims } from '../components/report/ReportClaims'
import { ReportHeader } from '../components/report/ReportHeader'
import { ReportMatrix } from '../components/report/ReportMatrix'
import { ReportMetrics } from '../components/report/ReportMetrics'
import { ReportQuality } from '../components/report/ReportQuality'
import { ReportSectionBody } from '../components/report/ReportSection'
import { ReportStructured } from '../components/report/ReportStructured'
import { ReportToc } from '../components/report/ReportToc'
import { SectionAnnotator } from '../components/report/SectionAnnotator'
import { SentimentPanel } from '../components/report/SentimentPanel'
import { useAsync } from '../hooks/useAsync'
import { api, reportExportUrl } from '../lib/api'
import type { RefineResult } from '../lib/api'
import { formatDateTime } from '../lib/format'
import { citationNumbers } from '../lib/reportCitation'
import type { ReportSection } from '../types/report'

/**
 * 深化之后就地打上的补丁。**带 `reportId`**，理由见文件头。
 */
interface Patch {
  reportId: string
  sections: ReportSection[]
  quality: RefineResult['quality']
  completeness: RefineResult['completeness']
  feedbackCount: number
}

export default function ReportPage() {
  const { reportId = '' } = useParams()
  const { data, error, loading, reload } = useAsync(() => api.getReport(reportId), [reportId])

  const [patch, setPatch] = useState<Patch | null>(null)
  const [activeEvidenceId, setActiveEvidenceId] = useState<string | null>(null)
  const [qualityOpen, setQualityOpen] = useState(false)

  const applied = patch && patch.reportId === reportId ? patch : null

  const body = data?.data
  const feedbackCount = applied?.feedbackCount ?? data?.feedbackCount ?? 0
  const quality = applied === null ? body?.quality : applied.quality
  const completeness = applied === null ? body?.completeness : applied.completeness

  /**
   * 章节数组。这里也 memo，是为了**引用稳定**而不是省计算：
   * 直接写 `?? []` 的话，服务器那份 `sections` 缺席时每次渲染都得到
   * 一个新数组，下面依赖它的 `view` 就每帧重建，`evidenceStats`
   * 那一路的 `useMemo(..., [body])` 跟着一起失效。
   */
  const sections = useMemo(() => applied?.sections ?? body?.sections ?? [], [applied, body])

  /**
   * 真正渲染用的正文：服务器那份 + 本地补丁。
   *
   * 补丁盖在 `sections`/`quality`/`completeness` 三处。`EvidenceAside`
   * 也要拿到**合并后**的这一份：它内部按正文里的角标反推编号，
   * 深化重写过的正文引用了新证据，用服务器那份算出来的编号会和
   * 正文里的对不上——而两边的角标各自看起来都对，只有并排看才发现错位。
   *
   * memo 的依赖里 `sections` 已经是稳定的（要么来自 `data`、要么来自
   * `applied`），所以这个对象不会每渲染都是新的，下游那几个
   * `useMemo(..., [body])` 才有意义。
   */
  const view = useMemo(
    () => (body ? { ...body, sections, quality, completeness } : null),
    [body, sections, quality, completeness],
  )

  const numbers = useMemo(
    () => (view ? citationNumbers(view) : new Map<string, number>()),
    [view],
  )

  /**
   * 目录里正文之外的那些锚点。
   *
   * **每一条都必须对应一个真的会渲染出来的 `id`。** 光凭"报告里应该有
   * 这一节"来列，会在数据缺的时候留下一条点了没反应的目录项——
   * 读者点一下没动，得到的信息是"这个页面坏了"，而不是"这份报告没有舆情"。
   *
   * 所以下面每一条的条件都**照抄子组件的早退条件**：
   *   `ReportMatrix` / `SentimentPanel` 在数据缺席时返回 `null`（连 id 都没有）；
   *   `ReportClaims` 在空数组时渲染的是一段没有 `id` 的提示；
   *   `ReportStructured` / `ReportCatalog` 里那六块**永远渲染**（数组为空时
   *   印一句"没有"），所以它们无条件列出；
   *   `ReportAudit` 的 `#audit` 永远在，`#coercion` 只在有 `coercion` 时在。
   *
   * 这条耦合是真的：子组件哪天改了早退条件，这里得跟着改，而**没有东西
   * 会报错**。所以每一条都写在 `if (view.x)` 这种最显眼的形式里，
   * 而不是靠算一个布尔变量。
   */
  const anchors = useMemo(() => {
    if (!view) return []
    const list: Array<{ id: string; label: string; count?: number }> = []
    list.push({ id: 'metrics', label: '指标' })
    if (view.matrix) list.push({ id: 'matrix', label: '功能矩阵' })
    list.push(
      { id: 'market-share', label: '市场份额' },
      { id: 'five-forces', label: '波特五力' },
      { id: 'trends', label: '趋势' },
      { id: 'feature-trees', label: '功能对比' },
      { id: 'pricing', label: '定价' },
      { id: 'personas', label: '用户画像' },
    )
    // 条件照抄 `ReportCharts` 的早退条件（数组为空时它整块不渲染）。
    // 写成 `(view.charts ?? []).length > 0` 而不是 `if (view.charts)`：
    // 后端发一个**空数组**和根本不发这个键，对那块而言是同一件事。
    if ((view.charts ?? []).length > 0) {
      list.push({ id: 'charts', label: '图表', count: view.charts?.length })
    }
    if (view.sentiment) list.push({ id: 'sentiment', label: '用户舆情' })
    if ((view.claims ?? []).length > 0) {
      list.push({ id: 'claims', label: '论点', count: view.claims?.length })
    }
    list.push({ id: 'audit', label: '审计发现' })
    if (view.coercion) list.push({ id: 'coercion', label: '模型输出改写' })
    if (view.evidenceStats) list.push({ id: 'evidence-stats', label: '证据统计' })
    if (completeness) list.push({ id: 'completeness', label: '模块完整度' })
    if ((view.glossary ?? []).length > 0) list.push({ id: 'glossary', label: '指标口径' })
    if ((view.gallery ?? []).length > 0) {
      list.push({ id: 'gallery', label: '图集', count: view.gallery?.length })
    }
    if (
      (view.team?.lead?.length ?? 0) +
        (view.team?.strategists?.length ?? 0) +
        (view.team?.executors?.length ?? 0) >
      0
    ) {
      list.push({ id: 'team', label: '参与专家' })
    }
    return list
  }, [view, completeness])

  const onCite = useCallback((evidenceId: string) => setActiveEvidenceId(evidenceId), [])

  const onRefined = useCallback(
    (result: RefineResult) => {
      setPatch((previous) => {
        const base = previous && previous.reportId === reportId ? previous.sections : sections
        return {
          reportId,
          sections: base.map((item) => (item.key === result.sectionKey ? result.section : item)),
          quality: result.quality,
          completeness: result.completeness,
          feedbackCount: result.feedbackCount,
        }
      })
    },
    [reportId, sections],
  )

  const onAnnotated = useCallback(
    (count: number) => {
      setPatch((previous) => ({
        reportId,
        sections: previous && previous.reportId === reportId ? previous.sections : sections,
        quality,
        completeness,
        feedbackCount: count,
      }))
    },
    [reportId, sections, quality, completeness],
  )

  if (loading && !data) {
    return (
      <div className="mx-auto max-w-3xl px-8 py-16">
        <Loading what="报告" />
      </div>
    )
  }

  // 守卫用 `view` 而不是 `body`：两者同源（`view` 为空当且仅当 `body` 为空），
  // 但只有守卫 `view` 之后 TS 才肯把它收窄成非空——后面整页都用它。
  if (error || !data || !view) {
    return (
      <div className="mx-auto max-w-3xl px-8 py-16">
        {error ? (
          <ErrorNote error={error} onRetry={reload} />
        ) : (
          <Empty
            title="这份报告不在库里"
            hint={
              <>
                可能还没有跑出报告，或者 id 不对。去{' '}
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

  return (
    <div className="flex h-screen flex-col">
      {/* ---- 顶栏 ---- */}
      <header className="flex shrink-0 items-center gap-3 border-b border-line bg-panel px-4 py-2">
        <Link to="/library" className="shrink-0 text-[12px] text-brand hover:underline">
          ← 我的调研
        </Link>
        <span className="shrink-0 font-mono text-[11px] text-fg-faint">{reportId}</span>
        <span className="ml-auto shrink-0 text-[11px] text-fg-faint">
          {view.generatedAt && <>生成于 {formatDateTime(view.generatedAt)}</>}
        </span>
        <Link
          to={`/trace/${data.taskId}?report=${reportId}`}
          className="shrink-0 rounded-lg border border-line px-2.5 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
        >
          决策回放
        </Link>
        <Link
          to={`/graph/${reportId}`}
          className="shrink-0 rounded-lg border border-line px-2.5 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
        >
          知识图谱
        </Link>
        <Link
          to={`/workspace/${data.taskId}`}
          className="shrink-0 rounded-lg border border-line px-2.5 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
        >
          看它怎么跑的
        </Link>
        <a
          href={reportExportUrl(reportId, 'md')}
          className="shrink-0 rounded-lg border border-line px-2.5 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
        >
          导出 Markdown
        </a>
        <a
          href={reportExportUrl(reportId, 'json')}
          className="shrink-0 rounded-lg border border-line px-2.5 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
        >
          JSON
        </a>
      </header>

      {/* ---- 三栏 ---- */}
      <div className="grid min-h-0 flex-1 grid-cols-[200px_minmax(0,1fr)_minmax(0,360px)] gap-0">
        {/* 左：目录 */}
        <nav className="min-h-0 overflow-y-auto border-r border-line px-3 py-4">
          <ReportToc sections={sections} extraAnchors={anchors} />
        </nav>

        {/* 中：正文 */}
        <main className="min-h-0 overflow-y-auto px-6 py-5">
          <div className="flex flex-col gap-4">
            <ReportHeader
              reportId={reportId}
              body={view}
              quality={quality}
              feedbackCount={feedbackCount}
              onOpenQuality={() => setQualityOpen((open) => !open)}
            />

            {qualityOpen && <ReportQuality quality={quality} onClose={() => setQualityOpen(false)} />}

            <ReportMetrics metrics={view.metrics} />
            <ReportMatrix matrix={view.matrix} />

            <ReportStructured
              marketShare={view.marketShare}
              fiveForces={view.fiveForces}
              trends={view.trends}
            />
            {/* 图形化的那一份，紧挨着它数据的来源（矩阵 / 份额）。
                这里曾整块缺失：`ReportChart` 写好了但没人引用，
                见 `ReportCharts.tsx` 的文件头。 */}
            <ReportCharts charts={view.charts} />
            <ReportCatalog
              featureTrees={view.featureTrees}
              pricingModels={view.pricingModels}
              personaSets={view.personaSets}
            />
            <SentimentPanel sentiment={view.sentiment} onCite={onCite} />

            {sections.map((section) => (
              <ReportSectionBody
                key={section.key}
                section={section}
                numbers={numbers}
                activeEvidenceId={activeEvidenceId}
                onCite={onCite}
                annotator={
                  <SectionAnnotator
                    reportId={reportId}
                    sectionKey={section.key}
                    onRefined={onRefined}
                    onAnnotated={onAnnotated}
                  />
                }
              />
            ))}

            <ReportClaims claims={view.claims} />
            <ReportAudit
              issues={view.audit?.issues}
              coercion={view.coercion}
              refinements={view.refinements}
            />
            <ReportAppendix
              evidenceStats={view.evidenceStats}
              completeness={completeness}
              glossary={view.glossary}
              gallery={view.gallery}
              team={view.team}
            />
          </div>
        </main>

        {/* 右：证据。**独立滚动**——正文滚到底部时证据栏要还在原处，
            否则点一个角标正文和证据一起动，读者立刻失去方位。 */}
        <aside className="min-h-0 overflow-y-auto border-l border-line px-4 py-4">
          <EvidenceAside body={view} activeEvidenceId={activeEvidenceId} onCite={onCite} />
        </aside>
      </div>
    </div>
  )
}
