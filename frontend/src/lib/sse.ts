/**
 * `EventSource` 的薄封装。
 *
 * 它只做三件事：按类型分发、把水位记下来、**出错不主动断**。
 * 断线续传的整条链路（`id:` 帧 → 浏览器记住 → 重连带 `Last-Event-ID`
 * → 服务端补发）全部由浏览器和服务端完成，这里没有一行重连逻辑。
 * 自己写重连只会把它写坏。
 *
 * 两个必须知道的事实
 * ----------------
 * **一、命名事件不会触发 `onmessage`。**
 * 服务端发的帧是 `event: thought` 这样的**具名**帧（见后端
 * `PipelineEvent.to_sse()`）。`onmessage` 只接**不带 `event:` 行**的帧，
 * 所以只挂 `onmessage` 的话，一条思维流都收不到——而且不报错：
 * 连接是 200、控制台干净、页面只是永远是空的。
 * 这就是下面要按 `EVENT_TYPES` 逐个 `addEventListener` 的原因。
 *
 * **二、`EventSource` 发不了请求头。**
 * 于是"从我记着的水位续传"这件事，在一个**新建**的连接上只能靠
 * URL 上的 `?from_seq=`——浏览器自动重连时才会带 `Last-Event-ID` 头。
 * 两种续传方式服务端都支持，优先级是头高于查询参数
 * （见后端 `_resume_from` 的注释：重连时 URL 还带着旧的 `from_seq`，
 * 让查询参数优先会导致每断一次网就重传全部历史）。
 */

import { EVENT_TYPES, isPipelineEvent, type PipelineEvent } from '../types/events'

export interface TaskStreamHandlers {
  onEvent: (event: PipelineEvent) => void
  /** 连接建立（含自动重连成功）。用来把状态从"重连中"改回"实时" */
  onOpen?: () => void
  /**
   * 连接断了但**会自己回来**。浏览器正在退避重试。
   * 收到它不代表要做什么——只该把界面标成"重连中"。
   */
  onDropped?: (watermark: number) => void
  /**
   * 连接**不会**再回来了：`readyState` 变成 `CLOSED`。
   * 常见原因是任务不存在（404）——这类错误重连多少次都一样。
   */
  onFatal?: (reason: string) => void
}

export interface TaskStreamOptions {
  /**
   * 从哪条事件之后开始。新建连接时只能靠它续传（见文件头第二条）。
   * 传 0 表示全量，服务端会把历史一次性补发。
   */
  fromSeq?: number
}

export interface TaskStream {
  close: () => void
  /** 服务端最后一条事件的 seq。浏览器在重连时自动带上的就是它 */
  watermark: () => number
}

/**
 * 打开一个任务的事件流。
 *
 * 返回的对象里 `close()` 是**唯一**该主动断开的地方——unmount 时调它。
 * 断开之后不要再调 `watermark()`，那时它已经不再更新。
 */
export function openTaskStream(
  taskId: string,
  handlers: TaskStreamHandlers,
  options: TaskStreamOptions = {},
): TaskStream {
  const fromSeq = Math.max(0, options.fromSeq ?? 0)
  const query = fromSeq > 0 ? `?from_seq=${fromSeq}` : ''
  const source = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/stream${query}`)

  let watermark = fromSeq
  let closed = false

  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (raw) => {
      const frame = raw as MessageEvent<string>
      let parsed: unknown
      try {
        parsed = JSON.parse(frame.data)
      } catch {
        // 一帧解析不了不该把整条流废掉：后面还有几百帧。
        // 这里的失败是**可见**的（控制台 + 下面那条 onFatal 不触发），
        // 而吞掉整条流会让用户丢掉全部历史。
        console.warn('[sse] 收到一帧无法解析的数据，已跳过', frame.data)
        return
      }
      if (!isPipelineEvent(parsed)) {
        console.warn('[sse] 帧的形状不符合契约，已跳过', parsed)
        return
      }
      // 水位**先推进再分发**：分发出错时至少不会因为水位没动而
      // 在重连后重放同一帧、再错一次。
      watermark = Math.max(watermark, parsed.seq)
      handlers.onEvent(parsed)
    })
  }

  source.onopen = () => {
    if (!closed) handlers.onOpen?.()
  }

  source.onerror = () => {
    if (closed) return

    // `readyState` 是这里唯一要读的东西，它区分了两种完全不同的"出错"：
    //
    //   CONNECTING(0) —— 浏览器已经在退避重连了。什么都不用做。
    //                    在这里 `close()` 是**最坏的选择**：它把浏览器
    //                    自带的、带退避的自动重连关掉，然后我们手上
    //                    什么都没有，只能看着页面停在半路。
    //   CLOSED(1)     —— 不会再有重连。通常是 4xx（任务不存在）
    //                    或浏览器放弃。这时才该告诉调用方。
    //
    // 只写一个 `source.close()` 是最省事的写法，而它的表现是
    // "网络抖一下就永久停更"——恰恰是参考实现在这一块上的问题。
    if (source.readyState === EventSource.CONNECTING) {
      handlers.onDropped?.(watermark)
      return
    }
    handlers.onFatal?.(`事件流已关闭（readyState=${source.readyState}）`)
  }

  return {
    close: () => {
      closed = true
      source.close()
    },
    watermark: () => watermark,
  }
}
