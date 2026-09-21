/**
 * 审计与降级记录：这次跑了哪些检查、发现过什么、修掉了什么、还剩什么。
 *
 * 这一块存在的唯一理由是**让"没问题的部分"变得可信**
 * ------------------------------------------------
 * 一份报告说"我们做了交叉验证"，读者只能选择信或不信。把审计结果
 * 摊开之后，他可以看到具体发现了哪些问题、哪些已经返工处理掉
 * （`resolved`）、哪些还挂着。**留下没处理的比假装没有更可信**——
 * 一份列着三条未解决问题的报告，比一份"零问题"的报告更像真的做过检查。
 *
 * `coercion` 那一块是**模型输出的原始瑕疵**
 * -------------------------------------
 * `repairs` 是每次"模型给的形状不对、我们改成了对的"的记录，
 * `phantomIds` 是模型编造过的引用 id。它们和 `metrics.phantomCitations`
 * 不是一回事：那个数是比例，这里是**具体的 id**——想知道"模型到底编了
 * 什么"，只能在这里看。
 *
 * `resolved` 判断不了的，不硬判
 * --------------------------
 * 旧报告里没有这个字段（是返工闭环加进去的）。`undefined` 时
 * 显示成"未标注"而不是"未解决"——把"没有这个信息"显示成"问题还在"，
 * 会让一份老报告看起来比它实际的样子差。
 */
import { formatDateTime } from '../../lib/format'
import type { CoercionReport, ReportAuditIssue, ReportRefinement } from '../../types/report'
import { ReportPanel } from './ReportPanel'

const SEVERITY_TONE: Record<string, string> = {
  blocker: 'bg-danger/14 text-danger',
  major: 'bg-warn/16 text-warn',
  minor: 'bg-raised text-fg-muted',
}

const SEVERITY_LABEL: Record<string, string> = {
  blocker: '阻断',
  major: '重要',
  minor: '次要',
}

export interface ReportAuditProps {
  issues: ReportAuditIssue[] | undefined
  coercion: CoercionReport | undefined
  refinements: ReportRefinement[] | undefined
}

function Audit({ issues }: { issues: ReportAuditIssue[] }) {
  if (issues.length === 0) {
    return <p className="text-[12px] text-fg-faint">这次审计没有发现问题。</p>
  }

  const unresolved = issues.filter((issue) => issue.resolved !== true).length

  return (
    <div className="flex flex-col gap-2">
      <p className="text-[11px] text-fg-faint">
        共 {issues.length} 条，其中 <span className="tabular">{unresolved}</span> 条还没有处理。
      </p>
      <ul className="flex flex-col">
        {issues.map((issue) => (
          <li key={issue.issueId} className="border-t border-line py-2 first:border-t-0">
            <div className="flex flex-wrap items-baseline gap-2">
              <span
                className={[
                  'shrink-0 rounded px-1.5 py-0.5 text-[10px]',
                  SEVERITY_TONE[issue.severity] ?? SEVERITY_TONE.minor,
                ].join(' ')}
              >
                {SEVERITY_LABEL[issue.severity] ?? issue.severity}
              </span>
              {/* 标签直接用服务端发来的 `kindLabel`。前端**不建
                  kind → 标签的映射表**：建了就会在后端加一种问题时
                  静默显示一个英文 key。 */}
              <span className="text-[11.5px] text-fg">{issue.kindLabel}</span>
              {issue.brand && <span className="text-[10px] text-fg-faint">{issue.brand}</span>}
              {issue.dimension && (
                <span className="text-[10px] text-fg-faint">· {issue.dimension}</span>
              )}

              <span
                className={[
                  'ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px]',
                  issue.resolved === true
                    ? 'bg-ok/12 text-ok'
                    : issue.resolved === false
                      ? 'bg-warn/14 text-warn'
                      : 'bg-raised text-fg-faint',
                ].join(' ')}
              >
                {issue.resolved === true ? '已处理' : issue.resolved === false ? '未处理' : '未标注'}
              </span>
            </div>

            <p className="mt-1 text-[11.5px] leading-relaxed text-fg-muted">{issue.detail}</p>
            {issue.suggestion && (
              <p className="mt-0.5 text-[11px] leading-relaxed text-fg-faint">
                建议：{issue.suggestion}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

function Coercion({ coercion }: { coercion: CoercionReport }) {
  const hasSomething =
    coercion.repairs.length > 0 ||
    coercion.phantomIds.length > 0 ||
    coercion.missing.length > 0 ||
    coercion.degraded

  return (
    <div className="flex flex-col gap-2">
      <p className="text-[11px] leading-relaxed text-fg-faint">
        模型输出里有 <span className="tabular">{coercion.producedIds}</span> 个引用 id，
        其中 <span className="tabular">{coercion.resolvedIds}</span> 个在证据表里查得到。
      </p>

      {!hasSomething && (
        <p className="text-[12px] text-fg-faint">模型这次的输出形状都是对的，没有需要改写的地方。</p>
      )}

      {coercion.phantomIds.length > 0 && (
        <div className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2">
          <p className="text-[11.5px] text-danger">
            模型编造了 {coercion.phantomIds.length} 个不存在的引用 id（已从正文剔除）
          </p>
          <p className="mt-0.5 break-all font-mono text-[10.5px] text-fg-faint">
            {coercion.phantomIds.join('、')}
          </p>
        </div>
      )}

      {coercion.repairs.length > 0 && (
        <div>
          <p className="mb-0.5 text-[11px] text-fg-muted">改写过的地方（{coercion.repairs.length}）</p>
          <ul className="flex flex-col gap-0.5">
            {coercion.repairs.map((repair, index) => (
              <li key={index} className="text-[11px] leading-relaxed text-fg-faint">
                · {repair}
              </li>
            ))}
          </ul>
        </div>
      )}

      {coercion.missing.length > 0 && (
        <div>
          <p className="mb-0.5 text-[11px] text-fg-muted">缺的字段（{coercion.missing.length}）</p>
          <p className="break-all font-mono text-[10.5px] text-fg-faint">
            {coercion.missing.join('、')}
          </p>
        </div>
      )}
    </div>
  )
}

function Refinements({ items }: { items: ReportRefinement[] }) {
  if (items.length === 0) return null

  return (
    <div className="mt-3 border-t border-line pt-3">
      <p className="mb-1.5 text-[11px] text-fg-muted">深化记录（{items.length}）</p>
      <ul className="flex flex-col gap-2">
        {items.map((item, index) => (
          <li key={index} className="text-[11px] leading-relaxed">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="font-mono text-[10.5px] text-fg-faint">{item.sectionKey}</span>
              <span className="text-fg-faint">{formatDateTime(item.at)}</span>
              <span className="ml-auto shrink-0 tabular text-fg-muted">
                +{item.addedEvidences} 条证据
              </span>
            </div>
            {/* 批注是**要求**，深化是对它的执行。两者都印出来：
                只说"这一节被深化过"，读者不知道是按什么要求改的。 */}
            <p className="mt-0.5 text-fg-muted">批注：{item.annotation}</p>
            {item.degraded.length > 0 && (
              <p className="mt-0.5 text-warn">这次深化有降级：{item.degraded.join('；')}</p>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function ReportAudit({ issues, coercion, refinements }: ReportAuditProps) {
  return (
    <>
      <ReportPanel id="audit" title="审计发现">
        <Audit issues={issues ?? []} />
        {/* 深化记录挂在审计块里：它是"这份报告被人工干预过"的一部分，
            和审计问题回答的是同一类问题（这份报告还有哪儿不踏实）。 */}
        <Refinements items={refinements ?? []} />
      </ReportPanel>

      {coercion && (
        <ReportPanel id="coercion" title="模型输出改写记录">
          <Coercion coercion={coercion} />
        </ReportPanel>
      )}
    </>
  )
}
