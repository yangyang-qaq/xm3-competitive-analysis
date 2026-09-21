import { useCallback, useEffect, useRef, useState } from 'react'
import type { DependencyList } from 'react'

export interface AsyncState<T> {
  data: T | null
  error: Error | null
  loading: boolean
  reload: () => void
}

/**
 * 拉取一次数据并管理 loading / error。
 *
 * `fn` 存在 ref 里而不是进依赖数组：否则调用方每次渲染新建的闭包都会触发重取，
 * 这类死循环在 useEffect 里排查起来很费时间。真正的重取由 `deps` 与 `reload()` 驱动。
 */
export function useAsync<T>(fn: () => Promise<T>, deps: DependencyList = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    fnRef
      .current()
      .then((value) => {
        if (!cancelled) setData(value)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err : new Error(String(err)))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])

  return { data, error, loading, reload }
}
