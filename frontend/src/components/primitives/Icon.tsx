/**
 * 线条图标。**手写内联 SVG，不引图标库。**
 *
 * 理由和 `技术栈.md` 的「已知代价」是同一条：一个图标库为了 6 个图标
 * 要拖进来几百 KB 和一棵依赖树，而导航栏的图标是**最不会变**的那部分
 * 代码——它不值得一个依赖。
 *
 * 统一规格（改的时候一起改，否则一排图标粗细会不齐）：
 * `viewBox 24×24`、`stroke-width 1.6`、圆头圆角、`currentColor` 上色。
 * 颜色**永远从外面来**：`text-fg-muted` 之类。图标自己不定义颜色，
 * 这样它在选中态、禁用态、浅色深色底下都自动跟着走。
 */
import type { ReactNode } from 'react'

export type IconName =
  | 'home'
  | 'library'
  | 'knowledge'
  | 'experts'
  | 'radar'
  | 'spark'
  | 'chevronDown'
  | 'arrowRight'
  | 'download'
  | 'search'

const PATHS: Record<IconName, ReactNode> = {
  /* 工作台：屋顶 + 屋身 + 门。门那两笔让它在 16px 下也认得出是房子 */
  home: (
    <>
      <path d="M3.5 10.6 12 3.8l8.5 6.8" />
      <path d="M6 9.9V20h12V9.9" />
      <path d="M10 20v-4.6h4V20" />
    </>
  ),

  /* 我的调研：柱状图。三根柱子的高低是有意的（不是等高的）——
     等高的一排方块看起来像"列表"，不像"调研结果" */
  library: (
    <>
      <path d="M4 19.5V10" />
      <path d="M9.7 19.5V4.5" />
      <path d="M15.3 19.5v-6.2" />
      <path d="M21 19.5v-9.4" />
    </>
  ),

  /* 知识库：堆叠的三层。这是"库"的通用画法，
     比画一本书更贴切——证据是按层沉淀的，不是一本合上的书 */
  knowledge: (
    <>
      <path d="M12 3.4 21 8l-9 4.6L3 8z" />
      <path d="M3.4 12.4 12 16.8l8.6-4.4" />
      <path d="M3.4 16.4 12 20.8l8.6-4.4" />
    </>
  ),

  /* 专家公会：两个人。第二个人只画半边——
     画完整的话在 16px 下两团会糊在一起 */
  experts: (
    <>
      <circle cx="9.4" cy="8.2" r="3.3" />
      <path d="M3.4 19.6c0-3.3 2.7-5.6 6-5.6s6 2.3 6 5.6" />
      <path d="M16.2 5.3a3.3 3.3 0 0 1 0 5.8" />
      <path d="M17.6 14.4c2 .7 3.4 2.6 3.4 5.2" />
    </>
  ),

  /* 竞争情报中心：雷达。同心圆 + 一条扫描线，
     光有同心圆看起来像靶子（"目标"），加上扫描线才是"正在侦测" */
  radar: (
    <>
      <circle cx="12" cy="12" r="8.4" />
      <circle cx="12" cy="12" r="4.6" />
      <path d="M12 12l6.2-5.4" />
      <circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none" />
    </>
  ),

  /* 品牌标：四角星。用填充而不是描边——它是唯一的实心图形，
     在一排线性图标里立得住 */
  spark: (
    <path
      d="M12 3.2c.75 4.5 3.05 6.8 7.55 7.55-4.5.75-6.8 3.05-7.55 7.55-.75-4.5-3.05-6.8-7.55-7.55C8.95 10 11.25 7.7 12 3.2z"
      fill="currentColor"
      stroke="none"
    />
  ),

  chevronDown: <path d="m6.5 9.5 5.5 5.5 5.5-5.5" />,

  arrowRight: (
    <>
      <path d="M4.5 12h14.5" />
      <path d="M13.5 6.5 19.5 12l-6 5.5" />
    </>
  ),

  download: (
    <>
      <path d="M12 4v11" />
      <path d="m7.8 11 4.2 4.2 4.2-4.2" />
      <path d="M5 19.6h14" />
    </>
  ),

  search: (
    <>
      <circle cx="10.8" cy="10.8" r="6.3" />
      <path d="m15.4 15.4 4.1 4.1" />
    </>
  ),
}

export function Icon({
  name,
  size = 18,
  className = '',
  strokeWidth = 1.6,
}: {
  name: IconName
  size?: number
  className?: string
  /** 少数场合要更粗/更细（比如 32px 的装饰图标要更细才不笨重） */
  strokeWidth?: number
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={['shrink-0', className].join(' ')}
      // 纯装饰：图标旁边永远有文字。读屏软件念两遍是噪声。
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  )
}
