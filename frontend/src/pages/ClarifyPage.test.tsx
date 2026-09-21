/**
 * 澄清页的三条硬要求。它们都不是"界面好不好看"，是"这一页会不会卡死人"：
 *
 * 1. **`options` 为空的问题必须退化成自由文本。** 选项是模型给的，
 *    它完全可以一个都不给。那时渲染一组空按钮，用户没有任何办法作答，
 *    任务就停在这一页上——而后端其实允许只答一部分、甚至一个都不答。
 * 2. **判断要不要停在这一页，只看 `awaitingClarify`（状态），不看
 *    `needClarify`（事实）。** 看后者的表现是：一个上周就跑完的任务
 *    每次打开都被拉回这一页，而这一页上无事可做。
 * 3. **一个都不答也能提交。** `answers` 本来就是"只答了这些"，
 *    强行要求填满会让人瞎选一个——那比空着更坏。
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { FakeEventSource } from '../test/fakeEventSource'
import { useTaskStore } from '../store/taskStore'
import type { ClarifyQuestion, TaskSnapshot } from '../types/domain'
import ClarifyPage from './ClarifyPage'

// ---- fetch 桩 ----

interface PendingRequest {
  url: string
  method: string
  /** 提交体，原样存着。**"答了什么"这件事只能从这里验** */
  body: string
  respond: (response: Response) => void
  ok: (body: unknown) => void
  fail: (err: unknown) => void
}

let pending: PendingRequest[] = []
let restoreFetch: () => void

function json(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    json: async () => body,
  } as unknown as Response
}

function installFetch(): void {
  const original = globalThis.fetch
  pending = []
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    return new Promise<Response>((resolve, reject) => {
      pending.push({
        url,
        method: init?.method ?? 'GET',
        body: typeof init?.body === 'string' ? init.body : '',
        respond: resolve,
        ok: (body) => resolve(json(body)),
        fail: reject,
      })
    })
  }) as typeof fetch
  restoreFetch = () => {
    globalThis.fetch = original
  }
}

async function requestAt(index: number): Promise<PendingRequest> {
  await waitFor(() => expect(pending.length).toBeGreaterThan(index))
  const request = pending[index]
  if (!request) throw new Error(`第 ${index} 个请求还没发出来`)
  return request
}

// ---- 夹具 ----

let restoreEventSource: () => void

function question(overrides: Partial<ClarifyQuestion> = {}): ClarifyQuestion {
  return {
    id: 'Q1',
    question: '主要看哪几家？',
    kind: 'single',
    options: ['Notion', 'Obsidian'],
    recommended: '',
    ...overrides,
  }
}

function snapshot(overrides: Partial<TaskSnapshot> = {}): TaskSnapshot {
  return {
    taskId: 'TK-1',
    status: 'awaiting_clarify',
    stage: 'intake',
    stageLabel: '需求理解',
    progress: 0.06,
    reportId: '',
    error: '',
    nodes: {},
    lastSeq: 3,
    needClarify: true,
    awaitingClarify: true,
    clarifyQuestions: [question()],
    subject: '笔记软件',
    brands: [],
    ...overrides,
  }
}

/** 把澄清页挂在路由里，好观察它有没有跳走。 */
function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/clarify/TK-1']}>
      <Routes>
        <Route path="/clarify/:taskId" element={<ClarifyPage />} />
        <Route path="/workspace/:taskId" element={<div>工作台到了</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

/** 等快照落地。 */
async function arrive(body: TaskSnapshot) {
  const request = await requestAt(0)
  request.ok(body)
  // 判据取"加载中"那行消失，而**不是**某个分支专属的文案：
  // 快照到了之后，页面会走到"停住"或"说明状态"两种分支里的一种，
  // 拿其中一种的文案去等，另一种用例就会超时——而那和被测行为无关。
  await waitFor(() =>
    expect(screen.queryByText('正在读取任务…')).not.toBeInTheDocument(),
  )
}

beforeEach(() => {
  useTaskStore.getState().reset()
  installFetch()
  const original = (globalThis as { EventSource?: unknown }).EventSource
  FakeEventSource.reset()
  ;(globalThis as { EventSource?: unknown }).EventSource = FakeEventSource
  restoreEventSource = () => {
    ;(globalThis as { EventSource?: unknown }).EventSource = original
    FakeEventSource.reset()
  }
})

afterEach(() => {
  useTaskStore.getState().reset()
  restoreEventSource()
  restoreFetch()
})

describe('问题怎么渲染', () => {
  it('有选项的渲染成按钮', async () => {
    renderPage()
    await arrive(snapshot())

    expect(screen.getByRole('button', { name: 'Notion' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Obsidian' })).toBeInTheDocument()
  })

  it('**一个选项都没有的问题退化成自由文本**，而不是一组空按钮', async () => {
    renderPage()
    await arrive(
      snapshot({
        clarifyQuestions: [
          question({ id: 'Q1', question: '有没有特别想看的品牌？', options: [] }),
        ],
      }),
    )

    // 这条是这一页上最容易卡死人的地方：退化成按钮的话，
    // 用户既没得点、也没处填，而这个任务就停在这一页上不动了。
    const input = screen.getByPlaceholderText('不填就按默认策略')
    expect(input).toBeInTheDocument()

    await userEvent.type(input, '还想看看 Logseq')
    expect(input).toHaveValue('还想看看 Logseq')
  })

  it('`kind` 是 text 的同样走自由文本，哪怕它带了选项', async () => {
    renderPage()
    await arrive(
      snapshot({
        clarifyQuestions: [
          question({ id: 'Q1', question: '补充说明', kind: 'text', options: ['A', 'B'] }),
        ],
      }),
    )

    expect(screen.getByPlaceholderText('不填就按默认策略')).toBeInTheDocument()
    // 选项虽然给了，但 `kind` 说了算——两个判据只能有一个生效。
    expect(screen.queryByRole('button', { name: 'A' })).not.toBeInTheDocument()
  })
})

describe('答案怎么交', () => {
  it('选了单选项，提交时把这个答案发出去；再点一次能取消', async () => {
    renderPage()
    await arrive(snapshot())

    await userEvent.click(screen.getByRole('button', { name: 'Obsidian' }))
    await userEvent.click(screen.getByRole('button', { name: '开始调研' }))

    const post = await requestAt(1)
    expect(post.method).toBe('POST')
    expect(post.url).toBe('/api/tasks/TK-1/clarify')
    expect(JSON.parse(post.body)).toEqual({ answers: { Q1: 'Obsidian' } })
  })

  it('单选项再点一次回到空答案，而不是被迫停在某个选项上', async () => {
    renderPage()
    await arrive(snapshot())

    const obsidian = screen.getByRole('button', { name: 'Obsidian' })
    await userEvent.click(obsidian)
    await userEvent.click(obsidian)

    await userEvent.click(screen.getByRole('button', { name: '开始调研' }))
    const post = await requestAt(1)
    expect(JSON.parse(post.body)).toEqual({ answers: {} })
  })

  it('多选的答案用顿号连起来，再点一次是取消', async () => {
    renderPage()
    await arrive(
      snapshot({
        clarifyQuestions: [
          question({ id: 'Q1', kind: 'multi', options: ['Notion', 'Obsidian', 'Logseq'] }),
        ],
      }),
    )

    await userEvent.click(screen.getByRole('button', { name: 'Notion' }))
    await userEvent.click(screen.getByRole('button', { name: 'Logseq' }))
    // **"我有偏好又反悔了"要能回到空答案**，而不是被迫停在某个选项上。
    await userEvent.click(screen.getByRole('button', { name: 'Obsidian' }))
    await userEvent.click(screen.getByRole('button', { name: 'Obsidian' }))

    await userEvent.click(screen.getByRole('button', { name: '开始调研' }))
    const post = await requestAt(1)
    // 顿号拼接是**约定**，不是数据结构：后端的 answers 是 dict[str, str]，
    // 给多选单开一条通道会让"答了什么"有两种存法。
    expect(JSON.parse(post.body)).toEqual({ answers: { Q1: 'Notion、Logseq' } })
  })

  it('一个都不答也能提交，发出去的是空答案', async () => {
    renderPage()
    await arrive(snapshot())

    await userEvent.click(screen.getByRole('button', { name: '开始调研' }))
    const post = await requestAt(1)
    // 空着比瞎选一个更诚实——后端的 answers 本就允许只答一部分。
    // 这里断言的是**真的发了空对象**，而不是"界面看起来没报错"。
    expect(JSON.parse(post.body)).toEqual({ answers: {} })
  })

  it('自由文本只填了空白，等于没答', async () => {
    renderPage()
    await arrive(
      snapshot({ clarifyQuestions: [question({ id: 'Q1', options: [] })] }),
    )

    await userEvent.type(screen.getByPlaceholderText('不填就按默认策略'), '   ')
    await userEvent.click(screen.getByRole('button', { name: '开始调研' }))

    const post = await requestAt(1)
    // 空格不是答案。**发出去的键恰好是用户真答了的那些问题**——
    // 这条不变量让"答了什么"只有一种说法。
    expect(JSON.parse(post.body)).toEqual({ answers: {} })
  })
})

describe('该不该停在这一页', () => {
  it('在等回答：停住，把问题摆出来', async () => {
    renderPage()
    await arrive(snapshot())

    expect(screen.queryByText('工作台到了')).not.toBeInTheDocument()
    expect(screen.getByText('主要看哪几家？')).toBeInTheDocument()
  })

  it('已经答完了：自动去工作台，不让用户干等', async () => {
    renderPage()
    // `needClarify` 还是真（当时确实问过），但 `awaitingClarify` 已经转假。
    await arrive(snapshot({ awaitingClarify: false, needClarify: true }))

    await waitFor(() => expect(screen.getByText('工作台到了')).toBeInTheDocument())
  })

  it('从来没需要过澄清：停在原地说明状态，**不跳走**', async () => {
    renderPage()
    await arrive(
      snapshot({ needClarify: false, awaitingClarify: false, status: 'running' }),
    )

    // 判据用 `needClarify` 的话这里会跳去工作台——一个从来不问的任务
    // 被送去澄清页、再从澄清页被弹走，用户看到的是白屏闪一下。
    expect(screen.queryByText('工作台到了')).not.toBeInTheDocument()
    expect(screen.getByText(/这个任务现在不在等回答/)).toBeInTheDocument()
  })
})
