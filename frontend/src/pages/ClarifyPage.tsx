/**
 * 澄清页：需求不够清楚的时候，把问题摆出来让用户补齐。
 *
 * 三条与"一个表单"不同的地方：
 *
 * 1. **判断要不要停在这一页，只看 `awaitingClarify`，不看 `needClarify`。**
 *    前者是状态、后者是事实。用一个早就跑完的任务（它当时确实问过，
 *    所以 `needClarify` 是真）去判，每次打开都会被拉回这一页，
 *    而这一页上没有任何东西可做。
 * 2. **允许一个都不答。** 用户对某个问题没有偏好的时候，空着比瞎选一个
 *    更诚实——后端的 `answers` 本来就允许只答一部分。
 * 3. **`options` 为空就是自由文本。** 问题由模型生成，它的选项可能
 *    一个都没给；那时渲染一组空按钮，等于把这个任务卡死。
 */
import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { Panel } from '../components/primitives/Panel'
import { useTaskStream } from '../hooks/useTaskStream'
import { api } from '../lib/api'
import { useTaskStore } from '../store/taskStore'
import type { ClarifyQuestion } from '../types/domain'

export default function ClarifyPage() {
  const { taskId = '' } = useParams()
  const navigate = useNavigate()
  const { load, error } = useTaskStream(taskId)

  const questions = useTaskStore((state) => state.task.clarifyQuestions)
  const awaiting = useTaskStore((state) => state.task.awaitingClarify)
  const needClarify = useTaskStore((state) => state.task.needClarify)
  const subject = useTaskStore((state) => state.subject)
  const status = useTaskStore((state) => state.task.status)

  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

  // 答案已经收下、流水线接着跑了 —— 这时页面该自己走开。
  // 停在原地的话，用户会以为还得再点一次。
  useEffect(() => {
    if (load === 'ready' && !awaiting && needClarify) {
      navigate(`/workspace/${taskId}`, { replace: true })
    }
  }, [load, awaiting, needClarify, navigate, taskId])

  async function submit() {
    setSubmitting(true)
    setSubmitError('')
    // **空答案不往上报。** 把选项点掉（或自由文本填了又删空）之后，
    // `answers` 里会留下一个空串——它和"这一问没碰过"是同一件事，
    // 而报上去会让这一问看起来"用户答了"，只是答的是空。
    //
    // 于是在这里收敛成一条不变量：**发出去的键，恰好是用户真的答了的那些问题。**
    // 后端两种都收（`intake` 只判字典真不真），所以这不是在修一个报错，
    // 是在让"答了什么"这件事只有一种说法。
    const payload = Object.fromEntries(
      Object.entries(answers).filter(([, value]) => value.trim() !== ''),
    )
    try {
      await api.clarifyTask(taskId, payload)
      navigate(`/workspace/${taskId}`, { replace: true })
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : String(err))
      setSubmitting(false)
    }
  }

  if (load === 'loading') {
    return <Centered>正在读取任务…</Centered>
  }
  if (load === 'notFound') {
    return <Centered>找不到任务 {taskId}。</Centered>
  }
  if (load === 'error') {
    return <Centered>{error}</Centered>
  }

  // 快照回来了但任务**并没有**在等回答：可能已经答过了、可能已经跑完、
  // 也可能它从来就不需要澄清。三种情况下正确答案都是去工作台，
  // 而不是显示一个空表单。
  if (!awaiting) {
    return (
      <Centered>
        这个任务现在不在等回答（当前状态：{status}）。
        <Link to={`/workspace/${taskId}`} className="ml-1 text-brand hover:underline">
          去工作台
        </Link>
      </Centered>
    )
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center px-6 py-12">
      <header className="mb-6">
        <p className="text-xs text-fg-faint">需求澄清</p>
        <h1 className="mt-1 text-lg text-fg">
          {subject ? `关于「${subject}」还需要确认几件事` : '还需要确认几件事'}
        </h1>
        <p className="mt-2 text-xs leading-relaxed text-fg-muted">
          这几项会直接决定去查哪些品牌、看哪些维度。不确定的可以空着——
          空着比随便选一个更好，它会按默认策略继续。
        </p>
      </header>

      <Panel title={`${questions.length} 个问题`}>
        <ul className="divide-y divide-line/60">
          {questions.map((question) => (
            <li key={question.id} className="py-3 first:pt-0 last:pb-0">
              <QuestionField
                question={question}
                value={answers[question.id] ?? ''}
                onChange={(value) =>
                  setAnswers((current) => ({ ...current, [question.id]: value }))
                }
              />
            </li>
          ))}
        </ul>
      </Panel>

      {submitError && <p className="mt-3 text-xs text-danger">提交失败：{submitError}</p>}

      <div className="mt-5 flex items-center gap-3">
        <button
          type="button"
          disabled={submitting}
          onClick={submit}
          className="rounded bg-brand px-4 py-1.5 text-sm font-medium text-canvas disabled:opacity-50"
        >
          {submitting ? '提交中…' : '开始调研'}
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={() => navigate(`/workspace/${taskId}`)}
          className="text-xs text-fg-muted hover:text-fg disabled:opacity-50"
        >
          先不答，直接看它跑
        </button>
      </div>
    </div>
  )
}

function QuestionField({
  question,
  value,
  onChange,
}: {
  question: ClarifyQuestion
  value: string
  onChange: (value: string) => void
}) {
  // 没有选项就退化成自由文本。**这是硬要求**：选项是模型给的，
  // 它完全可以一个都不给，那时渲染一组空按钮会把这个任务卡死在
  // 这一页上，而用户没有任何办法提交。
  const freeText = question.options.length === 0 || question.kind === 'text'

  return (
    <div>
      <p className="text-sm text-fg">{question.question}</p>
      {question.recommended && (
        <p className="mt-0.5 text-[11px] text-fg-faint">
          建议：{question.recommended}
        </p>
      )}

      {freeText ? (
        <input
          type="text"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="不填就按默认策略"
          className="mt-2 w-full rounded border border-line bg-canvas px-2.5 py-1.5 text-sm text-fg placeholder:text-fg-faint focus:border-brand focus:outline-none"
        />
      ) : (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {question.options.map((option) => {
            // `multi` 的答案是一串用顿号连起来的选项。这是**约定**，
            // 不是数据结构：后端的 `answers` 是 `dict[str, str]`，
            // 为多选单独开一条通道会让"答了什么"有两种存法。
            const picked = question.kind === 'multi'
              ? value.split('、').filter(Boolean).includes(option)
              : value === option
            return (
              <button
                key={option}
                type="button"
                onClick={() => {
                  if (question.kind !== 'multi') {
                    // 再点一次取消选择，于是"我有偏好又反悔了"能回到
                    // 空答案，而不是被迫停在某个选项上。
                    onChange(picked ? '' : option)
                    return
                  }
                  const current = value.split('、').filter(Boolean)
                  const next = picked
                    ? current.filter((item) => item !== option)
                    : [...current, option]
                  onChange(next.join('、'))
                }}
                className={[
                  'rounded border px-2.5 py-1 text-xs transition-colors',
                  picked
                    ? 'border-brand bg-brand/15 text-brand'
                    : 'border-line text-fg-muted hover:border-line-strong hover:text-fg',
                ].join(' ')}
              >
                {option}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <p className="max-w-md text-center text-sm text-fg-muted">
        {children}
        <Link to="/" className="ml-2 text-brand hover:underline">
          回到首页
        </Link>
      </p>
    </div>
  )
}
