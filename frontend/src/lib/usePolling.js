import { useEffect, useRef, useState } from 'react'

// Simple interval polling with visibility awareness: pauses when the tab is
// hidden (no wasted requests), immediate first fetch, and no overlap (a slow
// response never stacks a second in-flight call). Chosen over WebSockets/SSE
// for this prompt deliberately -- see docs/frontend-architecture.md for what
// would change if the feed becomes push-based.
export function usePolling(fetcher, intervalMs, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const inFlight = useRef(false)
  const timerRef = useRef(null)

  useEffect(() => {
    let cancelled = false

    const tick = async () => {
      if (inFlight.current) return
      inFlight.current = true
      try {
        const result = await fetcher()
        if (!cancelled) {
          setData(result)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err)
      } finally {
        inFlight.current = false
        if (!cancelled) setLoading(false)
      }
    }

    const start = () => {
      tick()
      timerRef.current = setInterval(tick, intervalMs)
    }
    const stop = () => {
      if (timerRef.current) clearInterval(timerRef.current)
      timerRef.current = null
    }

    const onVisibility = () => {
      stop()
      if (document.visibilityState === 'visible') start()
    }
    document.addEventListener('visibilitychange', onVisibility)
    start()

    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', onVisibility)
      stop()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading }
}
