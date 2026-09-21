/**
 * 任务状态与快照契约：这一侧的那条测试。
 *
 * 后端有一条对应的（`backend/tests/contract/test_task_states.py`），
 * 它断言 `runner.py` 的常量与 `snapshot()` 的真实键集合。
 * 这一条守的是另外半边：**TypeScript 的类型与那份 JSON**。
 *
 * 为什么这一半以前是烂的
 * --------------------
 * SSE 事件有契约文件守着，任务状态这一半没有。于是 `TaskStatus` 写成了
 * `'created' | 'clarifying' | 'running' | 'done' | 'failed'`，
 * 而后端会写进任务行的是 `pending / running / awaiting_clarify / done /
 * failed / cancelled`——六个里对得上三个，**而没有任何东西会报错**。
 * 这不是"忘了改"，是"没有机制"：契约只覆盖了它写下来的那一半。
 *
 * 怎么在没有运行时类型的情况下做到这件事
 * ------------------------------------
 * 和 `events.contract.test.ts` 同一套：TypeScript 的 interface 运行时不存在，
 * 所以写一份字面量样例，用 `satisfies` 标注——
 *   - 少一个必需字段 → 编译不过
 *   - 字段名写错 / 多一个字段 → 编译不过（字面量的多余属性检查）
 *   - 类型不对 → 编译不过
 * 运行时再把样例的键与 JSON 比一遍。
 *
 * 编译期那半边**尤其重要**，因为 `npm run build` 会跑 `tsc -b`：
 * 一个字段名写错的样例会让构建失败，而 `vitest` 里那些纯运行时断言
 * 是拦不住它的。
 */
import { describe, expect, it } from 'vitest'

import contract from '../../../contracts/task_states.json'
import type { StageId, StageState, Task, TaskSnapshot, TaskStatus } from './domain'
import { TASK_STATUSES, TERMINAL_STATUSES, isTerminal } from './domain'

const STATUS_CONTRACT = contract.statuses
const SNAPSHOT_CONTRACT = contract.snapshot

// ============================================================
// 样例：靠编译器把类型钉住
// ============================================================

/**
 * 每种状态一个样例值。用 `Record<TaskStatus, ...>` 标注，于是
 * **联合类型少一个成员就编译不过**——这是"后端加了状态、前端没加"
 * 唯一能被自动发现的地方。
 */
const STATUS_LABELS: Record<TaskStatus, string> = {
  pending: '排队中',
  running: '进行中',
  awaiting_clarify: '等待回答',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

/** 每个阶段一个样例。`Record<StageId, ...>` 同样把联合类型钉死。 */
const STAGE_LABELS: Record<StageId, string> = {
  intake: '需求理解',
  orchestrator: '专家调度',
  collect: '联网采集',
  analyze: '分析研判',
  audit: '质量审计',
  rework: '返工补采',
  write: '报告撰写',
  done: '完成',
}

/** 兜底分支那一份：`nodes` 是 `{}`、没有 `elapsedMs`、有 `evidenceCount`。 */
const FALLBACK_SNAPSHOT = {
  taskId: 'TK-000000000000',
  status: 'running',
  stage: 'collect',
  stageLabel: '联网采集',
  progress: 0.42,
  reportId: '',
  error: '',
  nodes: {},
  lastSeq: 317,
  needClarify: true,
  awaitingClarify: false,
  clarifyQuestions: [
    { id: 'q1', question: '主要看哪几家？', kind: 'single', options: ['A', 'B'], recommended: 'A' },
  ],
  subject: '笔记软件',
  brands: ['Notion', 'Obsidian'],
  evidenceCount: 12,
} satisfies TaskSnapshot

/** 内存快照那一份：`nodes` 有值、有 `elapsedMs`、**没有** `evidenceCount`。
 *
 * 两份样例是各写各的，不是用展开语法派生的：`{...a, evidenceCount: undefined}`
 * 里那个键**存在**（`'evidenceCount' in x` 为真），于是下面
 * "两个生产者的差异"那条断言会假绿——它测的是键在不在，
 * 而派生写法让键一直在。
 */
const LIVE_SNAPSHOT = {
  taskId: 'TK-000000000000',
  status: 'running',
  stage: 'collect',
  stageLabel: '联网采集',
  progress: 0.42,
  reportId: '',
  error: '',
  nodes: { intake: 'done', orchestrator: 'done', collect: 'running' },
  lastSeq: 317,
  needClarify: true,
  awaitingClarify: false,
  clarifyQuestions: [],
  subject: '笔记软件',
  brands: ['Notion', 'Obsidian'],
  elapsedMs: 12345,
} satisfies TaskSnapshot

/** 任务列表里的一行。字段名与后端 `TaskRecord.to_dict()` 逐字一致。 */
const TASK_ROW = {
  taskId: 'TK-000000000000',
  query: '对比 Notion 与 Obsidian',
  mode: 'quick',
  status: 'awaiting_clarify',
  stage: 'intake',
  progress: 0.06,
  needClarify: true,
  clarifyQuestions: [],
  clarifyAnswers: {},
  subject: '笔记软件',
  brands: ['Notion', 'Obsidian'],
  createdAt: '2026-01-01T00:00:00.000+00:00',
  updatedAt: '2026-01-01T00:00:01.000+00:00',
  error: '',
} satisfies Task

// ============================================================
// 状态集合
// ============================================================

describe('任务状态', () => {
  it('联合类型的成员与契约严格相等', () => {
    // 顺序不参与断言：两份清单的顺序没有含义，比顺序只会换来无谓的红。
    expect([...TASK_STATUSES].sort()).toEqual([...STATUS_CONTRACT.required].sort())
  })

  it('每个状态都有一个标签', () => {
    // `Record<TaskStatus, string>` 已经保证编译期不漏，这里断言的是
    // 运行时那份数组与它同源——两处各写一份清单就会在这里红。
    expect(Object.keys(STATUS_LABELS).sort()).toEqual([...TASK_STATUSES].sort())
  })

  it('终态集合与契约一致，且等待澄清不在里面', () => {
    expect([...TERMINAL_STATUSES].sort()).toEqual(
      [...STATUS_CONTRACT.terminal.values].sort(),
    )
    // 这一条是整份契约里最贵的一条：把 awaiting_clarify 当成终态，
    // 用户答完澄清之后流水线再也不会动了，而界面显示一切正常。
    expect(isTerminal('awaiting_clarify')).toBe(false)
    expect(STATUS_CONTRACT.awaiting.value).toBe('awaiting_clarify')
  })

  it('isTerminal 与集合同源', () => {
    for (const status of TASK_STATUSES) {
      expect(isTerminal(status)).toBe(
        (STATUS_CONTRACT.terminal.values as readonly string[]).includes(status),
      )
    }
  })
})

// ============================================================
// 快照的键集合
// ============================================================

describe('任务快照', () => {
  it('必需键一个不少', () => {
    const missing = SNAPSHOT_CONTRACT.required.filter(
      (key) => !(key in LIVE_SNAPSHOT) || !(key in FALLBACK_SNAPSHOT),
    )
    expect(missing).toEqual([])
  })

  it('契约声明的键都在两个样例里出现过', () => {
    // 必需键两边都得有；可选键**只要有一边给**就算出现过——
    // 它们本来就是"只有这个生产者才给"的键，要求两边都有会与
    // 下面那条"两个生产者的差异"直接打架。
    for (const key of SNAPSHOT_CONTRACT.required) {
      expect(key in LIVE_SNAPSHOT, `内存快照漏了必需键 ${key}`).toBe(true)
      expect(key in FALLBACK_SNAPSHOT, `兜底分支漏了必需键 ${key}`).toBe(true)
    }
    for (const key of SNAPSHOT_CONTRACT.optional.keys) {
      const seen = key in LIVE_SNAPSHOT || key in FALLBACK_SNAPSHOT
      expect(seen, `两个样例都没带可选键 ${key}，那它声明了做什么`).toBe(true)
    }
  })

  it('两个生产者的差异与契约一致', () => {
    // `elapsedMs` 只有内存快照给：它是 monotonic 减启动时刻，
    // 进程重启之后那个时刻已经不存在了。编一个出来比缺着更坏。
    expect(SNAPSHOT_CONTRACT.optional.producers.elapsedMs).toBe('runner.snapshot()')
    expect('elapsedMs' in FALLBACK_SNAPSHOT).toBe(false)
    expect(typeof LIVE_SNAPSHOT.elapsedMs).toBe('number')
    // `evidenceCount` 只有兜底分支给。
    expect('evidenceCount' in LIVE_SNAPSHOT).toBe(false)
    expect(typeof FALLBACK_SNAPSHOT.evidenceCount).toBe('number')
  })

  it('兜底分支的 nodes 是空对象而不是全 pending', () => {
    // 全填 `pending` 会谎称"还没开始"，而事实是"跑过一段但过程不在内存里"。
    // 这两种情况在 DAG 上如果长得一样，用户会以为任务还没启动，
    // 于是重复提交一次。
    expect(FALLBACK_SNAPSHOT.nodes).toEqual({})
    expect(Object.keys(LIVE_SNAPSHOT.nodes ?? {}).length).toBeGreaterThan(0)
  })
})

// ============================================================
// 阶段与阶段状态
// ============================================================

describe('阶段', () => {
  it('StageId 覆盖契约里的每个阶段', () => {
    const declared = new Set<string>(contract.stages.order)
    declared.add(contract.stages.terminal)
    expect(Object.keys(STAGE_LABELS).sort()).toEqual([...declared].sort())
  })

  it('StageState 的成员与契约严格相等', () => {
    const sample: Record<StageState, true> = {
      pending: true,
      running: true,
      done: true,
      degraded: true,
      error: true,
    }
    expect(Object.keys(sample).sort()).toEqual(
      [...contract.stage_states.required].sort(),
    )
  })

  it('可选阶段是返工，而且它排在质检之后', () => {
    const order = contract.stages.order
    expect(contract.stages.optional).toEqual(['rework'])
    // 返工接在质检之后、写作之前——这是整个设计里最值钱的一个位置选择：
    // 写作是最贵的一步，把质检放在它前面，返工的代价是补采而不是重写。
    expect(order.indexOf('rework')).toBeGreaterThan(order.indexOf('audit'))
    expect(order.indexOf('rework')).toBeLessThan(order.indexOf('write'))
  })
})

// ============================================================
// 任务行
// ============================================================

describe('任务行', () => {
  it('字段名与后端逐字一致', () => {
    // 这一条挡的是把 `clarifyAnswers` 写成 `answers` 那种错误：
    // 差一个词，读到的永远是 undefined，而两边各自都自洽。
    expect(Object.keys(TASK_ROW).sort()).toEqual([
      'brands',
      'clarifyAnswers',
      'clarifyQuestions',
      'createdAt',
      'error',
      'mode',
      'needClarify',
      'progress',
      'query',
      'stage',
      'status',
      'subject',
      'taskId',
      'updatedAt',
    ])
  })
})
