/**
 * 左侧导航。
 *
 * 五个入口对应五件**不同的事**，不是五个筛选器：
 * 开新的调研 / 翻自己做过的 / 查沉淀下来的来源 / 看谁在干活 / 看整体盘子。
 * 命名照这个分法来（"我的调研"是"我发起的"，"知识库"是"攒下来的"），
 * 混着叫会让用户点进去才发现不是他要的那页。
 *
 * 「我的工作空间」那张卡上的进度条是**质量门通过率**，不是一个目标值。
 * 参考实现那里写的是"累计完成 1 次调研"配一条 10% 的条——那个 10%
 * 没有来源，是拿 1/10 编出来的。这里的数由 `/api/dashboard` 算出，
 * 所以它不会永远停在一个假的位置。
 */
import { NavLink } from 'react-router-dom'

import { useAsync } from '../../hooks/useAsync'
import { api } from '../../lib/api'
import { formatInt } from '../../lib/format'
import { Icon, type IconName } from '../primitives/Icon'

interface NavItem {
  to: string
  label: string
  icon: IconName
  /** 一行说明。放在 `title` 里——鼠标停一下能知道这页是干嘛的 */
  hint: string
}

const NAV: NavItem[] = [
  { to: '/', label: '工作台', icon: 'home', hint: '发起一次新调研' },
  { to: '/library', label: '我的调研', icon: 'library', hint: '做过的调研与报告' },
  { to: '/knowledge', label: '知识库', icon: 'knowledge', hint: '沉淀下来的证据来源' },
  { to: '/experts', label: '专家公会', icon: 'experts', hint: '48 位专家的分工与参与度' },
  { to: '/dashboard', label: '竞争情报中心', icon: 'radar', hint: '历次调研汇成的总体盘子' },
]

export function Sidebar() {
  // 侧边栏的这张卡是**全站唯一**读仪表盘的地方。放在这里而不是每个页面
  // 各读一次：它要显示的是"整体"，与当前在哪一页无关。
  const { data, error } = useAsync(() => api.dashboard())

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-line bg-panel">
      {/* ---------- 品牌 ---------- */}
      <div className="flex items-center gap-2.5 px-5 py-5">
        <span className="flex size-8 items-center justify-center rounded-[10px] bg-brand/12 text-brand">
          <Icon name="spark" size={17} />
        </span>
        <span className="text-[17px] font-semibold tracking-tight text-fg">xm3</span>
      </div>

      {/* ---------- 导航 ---------- */}
      <nav className="flex flex-col gap-0.5 px-3">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            // `end` 只给首页：不加的话 `/` 会匹配上每一条路由，
            // 于是"工作台"在每一页都是选中态。
            end={item.to === '/'}
            title={item.hint}
            className={({ isActive }) =>
              [
                'flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] transition-colors',
                isActive
                  ? 'bg-brand/12 font-medium text-brand'
                  : 'text-fg-muted hover:bg-raised hover:text-fg',
              ].join(' ')
            }
          >
            <Icon name={item.icon} />
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className="flex-1" />

      {/* ---------- 我的工作空间 ---------- */}
      <div className="mx-3 mb-3 rounded-card border border-line bg-canvas/60 px-3.5 py-3">
        <p className="text-[12px] font-medium text-fg">我的工作空间</p>
        {error ? (
          // **连不上后端要说出来。** 不说的话这张卡永远停在"…"，
          // 而用户会以为是自己还没有数据。
          <p className="mt-1.5 text-[11px] leading-relaxed text-danger">
            读不到统计：{error.message}
          </p>
        ) : !data ? (
          <p className="mt-1.5 text-[11px] text-fg-faint">正在读取…</p>
        ) : (
          <>
            <p className="mt-1.5 text-[11px] text-fg-muted">
              累计完成 {formatInt(data.runs.reports)} 次调研
            </p>
            <div className="mt-2 h-1 overflow-hidden rounded-full bg-raised">
              <div
                className="h-full rounded-full bg-brand transition-[width] duration-500"
                style={{ width: `${Math.round(data.runs.passRate * 100)}%` }}
              />
            </div>
            <div className="mt-1 flex items-baseline justify-between">
              <span className="text-[10px] text-fg-faint">
                质量门通过 {Math.round(data.runs.passRate * 100)}%
              </span>
              <span className="text-[11px] text-fg-muted">
                已沉淀 {formatInt(data.coverage.sources)} 条来源
              </span>
            </div>
          </>
        )}
      </div>

      {/* ---------- 身份 ---------- */}
      {/* 没有账号体系，所以这里**不编一个用户名**。
          "林研究员 / 青野科技"好看，但它会让每个看到界面的人以为
          这套系统接了登录——而它没有，这是单人本机工具。 */}
      <div className="flex items-center gap-2.5 border-t border-line px-5 py-3.5">
        <span className="flex size-8 items-center justify-center rounded-full bg-brand/12 text-[12px] font-medium text-brand">
          本
        </span>
        <div className="min-w-0">
          <p className="truncate text-[12px] font-medium text-fg">本机工作区</p>
          <p className="truncate text-[11px] text-fg-faint">单人模式 · 未接入账号</p>
        </div>
      </div>
    </aside>
  )
}
