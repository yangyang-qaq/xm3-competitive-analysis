/**
 * 工作台：填需求、选档位、开跑。
 *
 * 档位列表从 `GET /api/modes` 来，**不在这里写一份**。档位里的数字
 * （品牌数上限、检索次数预算、返工轮数）是会变的，硬编码一份就会在
 * 改了档位之后继续显示旧数字——而用户是照着那个数字决定选哪一档的。
 *
 * 档位做成**分段控件 + 一行明细**，而不是三张并排的大卡片
 * --------------------------------------------------
 * 大卡片把每个档位的所有参数都摊开，看着信息量大，实际反了：
 * 用户只需要比较三档的**一个区别**（要花多少检索、出多少章），
 * 摊开会逼他把六行数字横向对齐着读。分段控件先让他选，
 * 选完只显示这一档的明细——同一屏里少读五行。
 *
 * 建任务之后**按 `awaitingClarify` 决定去哪**，不看 `needClarify`：
 * 一个是状态、一个是事实。看后者的话，一个恰好在这次请求里被问过、
 * 但已经自动答完的任务会被送去澄清页，而那一页上无事可做。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { Icon } from '../components/primitives/Icon'
import { useAsync } from '../hooks/useAsync'
import { api } from '../lib/api'
import { formatInt } from '../lib/format'
import type { ResearchMode } from '../types/domain'

const EXAMPLES = [
  { text: '对比 Notion 与 Obsidian 在团队协作场景下的差异', tag: '工具对比' },
  { text: '国内新能源汽车充电桩运营商的竞争格局', tag: '行业格局' },
  { text: 'AI 编程助手在中小企业里的采用情况', tag: '采用调研' },
]

export default function HomePage() {
  const navigate = useNavigate()
  const { data: modes, error: modesError, reload: reloadModes } = useAsync(() => api.modes())

  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<ResearchMode | ''>('')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')

  // 档位还没加载出来时先不留空：用户可能已经打完字了，
  // 那时一个都没有选项的选择器会让"开始调研"看起来坏了。
  const activeMode = mode || modes?.default || ''
  const active = modes?.modes.find((item) => item.key === activeMode)

  async function start() {
    const text = query.trim()
    if (!text || creating) return
    setCreating(true)
    setError('')
    try {
      const created = await api.createTask({
        query: text,
        mode: (activeMode || undefined) as ResearchMode | undefined,
      })
      // `status === 'failed'` 也去工作台：那里会显示失败原因，
      // 而首页上没有位置放它。
      navigate(
        created.awaitingClarify ? `/clarify/${created.taskId}` : `/workspace/${created.taskId}`,
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setCreating(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center px-8 py-16">
      <div className="w-full max-w-2xl">
        <header className="mb-8 text-center">
          <h1 className="text-[26px] font-semibold tracking-tight text-fg">
            今天要调研点什么？
          </h1>
          <p className="mt-2 text-[13px] leading-relaxed text-fg-muted">
            说一句需求就够了。系统会先派专家拆解调研维度，再按维度去找证据 ——
            每条结论都带着它的来源回来。
          </p>
        </header>

        {/* ---------- 需求输入 ---------- */}
        <div className="rounded-card border border-line bg-panel shadow-card focus-within:border-brand">
          <label className="block">
            <span className="sr-only">要调研什么</span>
            <textarea
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                // `Enter` 直接开跑，`Shift+Enter` 换行。需求通常一句话就够，
                // 每次都去够鼠标会让这个框显得很重。
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  void start()
                }
              }}
              rows={3}
              autoFocus
              placeholder="例如：对比 Notion 与 Obsidian 在团队协作场景下的差异"
              className="w-full resize-none rounded-t-card bg-transparent px-4 py-3.5 text-[14px] leading-relaxed text-fg placeholder:text-fg-faint focus:outline-none"
            />
          </label>

          <div className="flex items-center gap-3 border-t border-line px-3.5 py-2.5">
            {/* 分段控件。`activeMode` 在档位还没读到时不选中任何一个——
                那是实话：确实还不知道默认是哪一档。 */}
            <div className="flex shrink-0 rounded-lg bg-raised p-0.5">
              {(modes?.modes ?? []).map((item) => (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setMode(item.key)}
                  title={item.description}
                  className={[
                    'rounded-[6px] px-2.5 py-1 text-[12px] transition-colors',
                    activeMode === item.key
                      ? 'bg-panel font-medium text-fg shadow-card'
                      : 'text-fg-muted hover:text-fg',
                  ].join(' ')}
                >
                  {item.label}
                </button>
              ))}
            </div>

            <span className="min-w-0 flex-1 text-[11px] leading-relaxed text-fg-faint">
              {active ? active.description : ''}
            </span>

            <button
              type="button"
              onClick={start}
              disabled={creating || query.trim().length === 0}
              className="flex shrink-0 items-center gap-1.5 rounded-lg bg-brand px-4 py-1.5 text-[13px] font-medium text-canvas transition-opacity disabled:opacity-40"
            >
              {creating ? (
                '正在理解需求…'
              ) : (
                <>
                  开始调研
                  <Icon name="arrowRight" size={14} />
                </>
              )}
            </button>
          </div>
        </div>

        {/* ---------- 选中档位的明细 ---------- */}
        {/* 返工额度是**另算**的，所以分开说。只报 `maxSearchCalls` 的话，
            "选了 64 就最多花 64"是个假承诺——返工还能再花 24。 */}
        {active && (
          <p className="tabular mt-2.5 text-center text-[11px] text-fg-faint">
            最多 {formatInt(active.maxBrands)} 个竞品 · {formatInt(active.maxSearchCalls)} 次检索
            · 产出 {formatInt(active.sectionCount)} 章
            {active.maxReworkRounds > 0 &&
              ` · 最多 ${formatInt(active.maxReworkRounds)} 轮返工（另 ${formatInt(
                active.reworkSearchCalls,
              )} 次检索额度）`}
          </p>
        )}

        {/* 档位读不到 = 后端连不上。**必须说出来**：不说的话这一块
            永远停在空白，而"开始调研"按下去只会报一个来自 POST 的错，
            让人以为是需求写得不对。 */}
        {modesError && (
          <div className="mt-3 rounded-card border border-danger/40 bg-danger/5 px-4 py-3">
            <p className="text-[12px] text-danger">连不上后端：{modesError.message}</p>
            <p className="mt-1.5 text-[11px] leading-relaxed text-fg-faint">
              确认后端已在 8020 端口启动：
              <code className="mx-1 rounded bg-raised px-1.5 py-0.5 font-mono">
                .venv/Scripts/python.exe -m uvicorn app.main:app --port 8020
              </code>
            </p>
            <button
              type="button"
              onClick={reloadModes}
              className="mt-2 rounded-lg border border-line px-3 py-1 text-[11px] text-fg-muted hover:border-line-strong hover:text-fg"
            >
              重试
            </button>
          </div>
        )}

        {error && <p className="mt-3 text-center text-[12px] text-danger">建任务失败：{error}</p>}

        {/* ---------- 示例 ---------- */}
        <div className="mt-10">
          <p className="mb-3 text-center text-[11px] text-fg-faint">或者，从这几个开始</p>
          <ul className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            {EXAMPLES.map((example) => (
              <li key={example.text}>
                <button
                  type="button"
                  onClick={() => setQuery(example.text)}
                  className="h-full w-full rounded-card border border-line bg-panel px-3.5 py-3 text-left transition-shadow hover:shadow-card"
                >
                  <span className="text-[10px] text-brand">{example.tag}</span>
                  <span className="mt-1 block text-[12px] leading-relaxed text-fg-muted">
                    {example.text}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
