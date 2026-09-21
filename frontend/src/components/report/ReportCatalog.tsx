/**
 * 按品牌铺开的三块明细：功能树、定价、用户画像。
 *
 * 三块的共同点是**它们的值可能整块没采到**，而且没采到时后端的表示
 * 各不相同——这一页必须把这三种"没有"都显示对：
 *
 * - `featureTrees[].features[].support === "unknown"`：**一个合法的取值**，
 *   意思是"这条没有证据判断"。它和 `none`（不支持）完全不同，
 *   混起来就会把"没查"说成"不支持"。
 * - `pricingModels[].degraded === true`：整块没采到。这时
 *   `currency` / `modelType` / `freeTier` 里存的是模型写的占位串
 *   （库里那份是 `"未获取到"`），**照原样印出来就成了三条事实**。
 *   所以 `degraded` 时这一块只印那句话，不印字段。
 * - `personaSets[].evidenceBackedRate`：画像里有多少条挂着证据。
 *   低的时候画像读起来仍然是"用户想要什么"，而它可能全是模型推的。
 *
 * 这三条都是同一个规矩：**缺数据就说缺数据，不要让缺数据的形状
 * 长得像有数据。**
 */
import type { ReactNode } from 'react'

import type { FeatureTree, PersonaSet, PricingModel } from '../../types/report'
import { ReportPanel } from './ReportPanel'

const SUPPORT_LABEL: Record<string, string> = {
  full: '支持',
  partial: '部分支持',
  none: '不支持',
  unknown: '未标注',
}

/** 支持度的配色。`unknown` 刻意用灰的——它不是"不支持"。 */
const SUPPORT_TONE: Record<string, string> = {
  full: 'bg-ok/12 text-ok',
  partial: 'bg-warn/14 text-warn',
  none: 'bg-danger/12 text-danger',
  unknown: 'bg-raised text-fg-faint',
}

function supportLabel(value: string): string {
  return SUPPORT_LABEL[value] ?? value
}

function Nothing({ children }: { children: ReactNode }) {
  return <p className="text-[12px] text-fg-faint">{children}</p>
}

/** 一条"这块没采到"的说明。比空白多一句话，比假数据少一个谎。 */
function Degraded({ what, detail }: { what: string; detail?: string }) {
  return (
    <p className="rounded-lg border border-warn/40 bg-warn/8 px-3 py-2 text-[11px] leading-relaxed text-fg-muted">
      {what}
      {detail && <span className="text-fg-faint">（{detail}）</span>}
    </p>
  )
}

// ----------------------------------------------------------------- 功能树

function FeatureTrees({ items }: { items: FeatureTree[] }) {
  if (items.length === 0) return <Nothing>这次没有做功能对比。</Nothing>

  return (
    <div className="flex flex-col gap-4">
      {/* key 里带下标而不是只用品牌名：**品牌重复过**。
          真报告里同一个品牌出现过两棵树（后端把调研对象也拿去问了，
          见 `问题记录.md` 问题 54），而 React 遇到重复 key 的行为是
          "可能重复渲染、也可能整个丢掉一棵"——它只在控制台说一句，
          页面上看不出来。带上下标之后，真有重复也是**两块都render出来**，
          一眼能看见，而不是随机少一块。

          后端已经改成同类重复不再产生（归属对不上就丢弃并记账），
          这里的下标是第二道：前端不该因为后端某天又出一个重复
          就静默少显示一份数据。 */}
      {items.map((tree, index) => (
        <div key={`${tree.brand}-${index}`}>
          <div className="flex items-baseline gap-2">
            <span className="text-[12px] font-medium text-fg">{tree.brand}</span>
            <span className="text-[10px] text-fg-faint tabular">
              覆盖 {Math.round(tree.coverage * 100)}%
              {tree.unknownCount > 0 && ` · ${tree.unknownCount} 项未标注`}
            </span>
          </div>

          {tree.degraded && <Degraded what="这个品牌的功能树没采全。" />}

          {tree.categories.length === 0 ? (
            <Nothing>没有可对比的功能点。</Nothing>
          ) : (
            <div className="mt-1.5 flex flex-col gap-2">
              {tree.categories.map((category) => (
                <div key={category.category}>
                  <p className="text-[11px] text-fg-muted">{category.category}</p>
                  <ul className="mt-0.5">
                    {category.features.map((feature) => (
                      <li key={feature.name} className="flex items-baseline gap-2 py-0.5">
                        <span
                          className={[
                            'shrink-0 rounded px-1.5 text-[10px]',
                            SUPPORT_TONE[feature.support] ?? SUPPORT_TONE.unknown,
                          ].join(' ')}
                        >
                          {supportLabel(feature.support)}
                        </span>
                        <span className="text-[11.5px] text-fg">{feature.name}</span>
                        {feature.note && (
                          <span className="text-[11px] leading-relaxed text-fg-faint">
                            {feature.note}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ------------------------------------------------------------------- 定价

function Pricing({ items }: { items: PricingModel[] }) {
  if (items.length === 0) return <Nothing>这次没有采到定价信息。</Nothing>

  return (
    <div className="flex flex-col gap-4">
      {/* key 带下标：理由见上面 `FeatureTrees` 那段（品牌重复，问题 54）。 */}
      {items.map((model, index) => (
        <div key={`${model.brand}-${index}`}>
          <div className="flex items-baseline gap-2">
            <span className="text-[12px] font-medium text-fg">{model.brand}</span>
            {/* 三个元字段只在**没降级**时印。降级时它们里面是
                "未获取到"这类占位串，印出来就是三条假事实。 */}
            {!model.degraded && (
              <span className="flex flex-wrap items-baseline gap-x-2 text-[10px] text-fg-faint">
                {model.modelType && <span>{model.modelType}</span>}
                {model.currency && <span>{model.currency}</span>}
                {model.freeTier && <span>免费版：{model.freeTier}</span>}
              </span>
            )}
          </div>

          {model.degraded && <Degraded what="这个品牌的定价没采到。" />}

          {model.tiers.length > 0 && (
            <div className="mt-1.5 overflow-x-auto">
              <table className="w-full border-collapse text-[11.5px]">
                <thead>
                  <tr className="text-fg-faint">
                    <th className="py-1 text-left font-medium">档位</th>
                    <th className="py-1 text-left font-medium">价格</th>
                    <th className="py-1 text-left font-medium">周期</th>
                    <th className="py-1 text-left font-medium">适用对象</th>
                    <th className="py-1 text-left font-medium">包含</th>
                  </tr>
                </thead>
                <tbody>
                  {model.tiers.map((tier) => (
                    <tr key={tier.name} className="border-t border-line align-top">
                      <th className="py-1 pr-3 text-left font-normal text-fg">{tier.name}</th>
                      <td className="py-1 pr-3 text-fg">
                        {tier.priceText || '—'}
                        {tier.unit && <span className="text-fg-faint">/{tier.unit}</span>}
                      </td>
                      <td className="py-1 pr-3 text-fg-muted">{tier.period || '—'}</td>
                      <td className="py-1 pr-3 text-fg-muted">{tier.targetUser || '—'}</td>
                      <td className="py-1 text-[11px] text-fg-faint">
                        {tier.includes.length > 0 ? tier.includes.join('、') : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ----------------------------------------------------------------- 用户画像

function Personas({ items }: { items: PersonaSet[] }) {
  if (items.length === 0) return <Nothing>这次没有做用户画像。</Nothing>

  return (
    <div className="flex flex-col gap-4">
      {/* key 带下标：理由见上面 `FeatureTrees` 那段（品牌重复，问题 54）。 */}
      {items.map((set, index) => (
        <div key={`${set.brand}-${index}`}>
          <div className="flex items-baseline gap-2">
            <span className="text-[12px] font-medium text-fg">{set.brand}</span>
            {/* 带证据比例低于一半时标黄：画像读起来永远是"用户想要什么"，
                而它可能全是模型推的。 */}
            <span
              className={[
                'text-[10px] tabular',
                set.evidenceBackedRate < 0.5 ? 'text-warn' : 'text-fg-faint',
              ].join(' ')}
              title="有证据支撑的画像占比"
            >
              {set.personas.length} 个画像 · 带证据{' '}
              {Math.round(set.evidenceBackedRate * 100)}%
            </span>
          </div>

          {set.degraded && <Degraded what="这个品牌的画像没采全。" />}

          <div className="mt-1.5 flex flex-col gap-2">
            {set.personas.map((persona) => (
              <div key={persona.name} className="rounded-lg bg-raised/60 px-3 py-2">
                <div className="flex items-baseline gap-2">
                  <span className="text-[12px] text-fg">{persona.name}</span>
                  {persona.segment && (
                    <span className="text-[10px] text-fg-faint">{persona.segment}</span>
                  )}
                  {/* `unknown` 是后端声明的取值（`MIGRATION_COSTS`），
                      不是空值——翻成"未知"而不是画一条空白。 */}
                  <span className="ml-auto shrink-0 text-[10px] text-fg-faint">
                    迁移成本：
                    {persona.migrationCost === 'unknown' ? '未知' : persona.migrationCost || '未知'}
                  </span>
                </div>
                {/* 四个列表都可能为空（模型只填了名字和场景）。
                    空的那些**整行不出现**——一行"痛点：—"看起来像
                    "这个画像没有痛点"，而真相是"这一项没写"。 */}
                {(
                  [
                    ['需求', persona.needs],
                    ['场景', persona.scenarios],
                    ['痛点', persona.painPoints],
                    ['决策因素', persona.decisionFactors],
                  ] as const
                ).map(([label, values]) =>
                  values.length > 0 ? (
                    <p key={label} className="mt-0.5 text-[11px] leading-relaxed text-fg-muted">
                      <span className="text-fg-faint">{label}：</span>
                      {values.join('、')}
                    </p>
                  ) : null,
                )}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

// -------------------------------------------------------------------- 出口

export interface ReportCatalogProps {
  featureTrees: FeatureTree[] | undefined
  pricingModels: PricingModel[] | undefined
  personaSets: PersonaSet[] | undefined
}

export function ReportCatalog({ featureTrees, pricingModels, personaSets }: ReportCatalogProps) {
  return (
    <>
      <ReportPanel id="feature-trees" title="功能对比" count={featureTrees?.length}>
        <FeatureTrees items={featureTrees ?? []} />
      </ReportPanel>
      <ReportPanel id="pricing" title="定价" count={pricingModels?.length}>
        <Pricing items={pricingModels ?? []} />
      </ReportPanel>
      <ReportPanel id="personas" title="用户画像" count={personaSets?.length}>
        <Personas items={personaSets ?? []} />
      </ReportPanel>
    </>
  )
}
