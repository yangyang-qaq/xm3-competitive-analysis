/**
 * 一个**忠实**的 `EventSource` 替身。
 *
 * "忠实"是这里唯一重要的事，因为要测的那条缺陷正是靠"不忠实"才会假绿：
 *
 *   - **具名帧只发给 `addEventListener(type, ...)`，不发给 `onmessage`。**
 *     真实的 `EventSource` 就是这样（规范里 `onmessage` 只接没有 `event:` 行的帧）。
 *     如果一个替身不管三七二十一都调 `onmessage`，那么
 *     "用 `onmessage` 收所有事件"这个缺陷在替身下能跑通、在浏览器里收不到一条
 *     ——测试会说一切正常，而这正是要防的那种假绿。
 *
 *   - **`close()` 之后不再分发**。真实实现会摘掉监听器。
 *
 *   - **`readyState` 由测试显式设置**。它是 `sse.ts` 里唯一区分
 *     "浏览器在退避重连"与"永远不回来了"的依据，而这两种情况
 *     在真实世界里长得一模一样（都是 `onerror`）。
 */

export class FakeEventSource {
  // 与规范同值。`sse.ts` 读的是 `EventSource.CONNECTING` 这个**静态**属性，
  // 所以替身必须把它挂在类上，而不是实例上。
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2

  /** 建过的每一个实例。测试拿它当"服务端收到的那个连接"。 */
  static instances: FakeEventSource[] = []

  static reset(): void {
    FakeEventSource.instances = []
  }

  /** 最后建的那个。绝大多数用例只开一条连接。 */
  static get last(): FakeEventSource {
    const source = FakeEventSource.instances.at(-1)
    if (!source) throw new Error('还没有建过 EventSource')
    return source
  }

  readonly url: string
  readyState: number = FakeEventSource.CONNECTING
  closed = false

  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null

  private readonly listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    const bucket = this.listeners.get(type) ?? []
    bucket.push(listener)
    this.listeners.set(type, bucket)
  }

  removeEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    const bucket = this.listeners.get(type)
    if (bucket) this.listeners.set(type, bucket.filter((item) => item !== listener))
  }

  close(): void {
    this.closed = true
    this.readyState = FakeEventSource.CLOSED
    this.listeners.clear()
  }

  // ---- 以下四个是测试用的"服务端动作"，浏览器里没有 ----

  /** 连接建立。真实实现是在响应头到达时触发。 */
  open(): void {
    this.readyState = FakeEventSource.OPEN
    this.onopen?.(new Event('open'))
  }

  /**
   * 推一个**具名**帧（`event: thought`）。
   *
   * `message` 这个名字是特例：规范里 `event: message` 会走 `onmessage`。
   * 事件契约里确实有 `message` 这一种，所以这里照着规范做，不特判。
   */
  emit(type: string, data: unknown): void {
    if (this.closed) return
    const frame = { data: typeof data === 'string' ? data : JSON.stringify(data) } as MessageEvent<string>
    for (const listener of this.listeners.get(type) ?? []) listener(frame)
    if (type === 'message') this.onmessage?.(frame)
  }

  /** 推一个**匿名**帧（没有 `event:` 行）。只有它该走 `onmessage`。 */
  emitUnnamed(data: unknown): void {
    if (this.closed) return
    const frame = { data: typeof data === 'string' ? data : JSON.stringify(data) } as MessageEvent<string>
    this.onmessage?.(frame)
  }

  /** 连接出错。`readyState` 要由调用方先设好——它就是被测代码唯一读的东西。 */
  error(readyState: number): void {
    this.readyState = readyState
    this.onerror?.(new Event('error'))
  }
}

/** 装上替身，返回卸载函数。 */
export function installFakeEventSource(): () => void {
  const original = (globalThis as { EventSource?: unknown }).EventSource
  FakeEventSource.reset()
  ;(globalThis as { EventSource?: unknown }).EventSource = FakeEventSource
  return () => {
    ;(globalThis as { EventSource?: unknown }).EventSource = original
    FakeEventSource.reset()
  }
}
