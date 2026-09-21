/**
 * 一个任务的生命周期：**先取快照，再连流，离开时断开。**
 *
 * 三件事的顺序不能换
 * ----------------
 * `hydrate` 必须在 `connect` **之前**。快照带着 journal 的水位（`lastSeq`），
 * 而 `connect` 会拿水位算出 `?from_seq=`。反过来先连流的话，
 * 那一刻水位还是 0，服务端会把**整段历史**重发一遍——
 * 去重能让界面看起来正常，但跑到一半刷新一次页面要重传几百帧，
 * 而"刷新"是用户会反复做的事。
 *
 * 为什么不用 `useAsync`
 * ------------------
 * 那个钩子管的是"取一次数据"，而这里要的是"取一次、然后按结果去连一条长连接、
 * 卸载时断开"。把连接放进 `useAsync` 的 then 里就没人负责断开它了。
 */
import { useEffect, useState } from 'react'

import { ApiError } from '../lib/api'
import { fetchSnapshot, useTaskStore } from '../store/taskStore'
import type { ConnectionState } from '../store/taskStore'

export type TaskLoad =
  /** 还没开始取快照 */
  | 'loading'
  /** 快照到手，流已接上（或正在重连） */
  | 'ready'
  | 'notFound'
  | 'error'

export interface TaskStreamHandle {
  load: TaskLoad
  /** `load === 'error'` 时的说明 */
  error: string
  connection: ConnectionState
  connectionNote: string
  reload: () => void
}

/**
 * 加载状态**和它描述的那个 taskId 存在一起**。
 *
 * 分开存的话会有一个渲染帧的空窗：切任务时组件实例被复用（同一条路由，
 * 只是 `:taskId` 变了），而 effect 要等到渲染**之后**才跑——
 * 所以从 A 切到 B 的那一帧，渲染出来的是 **A 的** DAG 和思维流，
 * 顶着 B 的标题。把"这份状态是谁的"记下来，那一帧就能在渲染时判掉。
 */
interface LoadState {
  forTask: string
  load: TaskLoad
  error: string
}

export function useTaskStream(taskId: string): TaskStreamHandle {
  const [result, setResult] = useState<LoadState>({
    forTask: '',
    load: 'loading',
    error: '',
  })
  const [nonce, setNonce] = useState(0)

  const connection = useTaskStore((state) => state.connection)
  const connectionNote = useTaskStore((state) => state.connectionNote)

  useEffect(() => {
    // 空的 `taskId` 在渲染那一层就判掉了（见函数末尾的返回值），
    // 这里只是别去发请求。
    if (!taskId) return

    let cancelled = false

    fetchSnapshot(taskId)
      .then((snapshot) => {
        // `cancelled` 挡的是"用户已经切到别的任务了，这个响应才回来"。
        // 不挡的话，两个任务的快照会按到达顺序互相覆盖——
        // 而工作台显示的是哪个任务取决于网络快慢。
        if (cancelled) return
        const store = useTaskStore.getState()
        // **每次进入工作台都从空状态开始。** store 是模块级的，只装得下一个任务：
        // 不清的话，从一个任务切到另一个时，DAG 上会留着上一个任务的节点状态、
        // 思维流里混着上一个任务的思维，而新任务的事件还没到——
        // 看起来像"新任务一上来就已经跑完一半了"。
        //
        // 同一条任务来回进出也会因此重取一次快照，这是有意的：
        // 快照里带着水位的权威值，比复用内存里的旧状态可靠。
        store.reset()
        store.hydrate(snapshot)
        // **水位不在这里传。** `hydrate` 已经把它写进 store 了，而
        // `connect` 本来就会取 `max(fromSeq, 手上水位)`——再传一遍是冗余的，
        // 且是一处**没有任何测试抓得住**的冗余：反证时把那半个参数删掉，
        // 门禁照样全绿，因为 hydrate 那一份已经把 URL 撑起来了。
        // 水位只有一个来源（store），于是"先 hydrate 再 connect"从
        // "建议的顺序"变成**必需的顺序**——反过来水位就是 0。
        store.connect(taskId)
        setResult({ forTask: taskId, load: 'ready', error: '' })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        // 404 单独拎出来：它和"后端连不上"要给用户不同的说法。
        // 一个已经被清掉的任务 id 显示"网络错误"会让人去查网络。
        if (err instanceof ApiError && err.status === 404) {
          setResult({ forTask: taskId, load: 'notFound', error: '' })
          return
        }
        setResult({
          forTask: taskId,
          load: 'error',
          error: err instanceof Error ? err.message : String(err),
        })
      })

    return () => {
      cancelled = true
      useTaskStore.getState().disconnect()
    }
  }, [taskId, nonce])

  // 这份状态是不是当前任务的？不是就当作"还在加载"。
  // 注意 effect 里**一处同步的 setState 都没有**：所有状态变更都发生在
  // 请求的回调里。切任务和点重试这两种"该回到加载中"的情形，
  // 一个靠上面这个比较判掉，一个由 `reload` 自己在事件里说清楚。
  const fresh = result.forTask === taskId

  return {
    load: !taskId ? 'notFound' : fresh ? result.load : 'loading',
    error: fresh ? result.error : '',
    connection,
    connectionNote,
    reload: () => {
      // 状态由**导致这次重新加载的那次点击**来改，而不是等 effect 起来再改。
      setResult((current) => ({ ...current, load: 'loading', error: '' }))
      setNonce((value) => value + 1)
    },
  }
}
