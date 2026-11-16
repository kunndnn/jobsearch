import { useCallback, useEffect, useRef, useState } from 'react'
import { getScrapeStatus } from '../api.js'

const RUNNING_POLL_MS = 3000
const IDLE_POLL_MS = 30000
const ERROR_RETRY_MS = 5000

const isFinished = (run) => Boolean(run) && run.status !== 'running'

function runsIn(data) {
  const runs = [...(data.history || [])]
  const current = data.current_run
  if (current && !runs.some((r) => r.id === current.id)) runs.push(current)
  return runs
}

/**
 * Polls GET /api/scrape/status (3 s while a run is active, 30 s idle) and calls
 * onRunFinished(run) exactly once for every run that finishes while the page is open.
 */
export function useScrapeStatus({ onRunFinished } = {}) {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)
  const onRunFinishedRef = useRef(onRunFinished)
  const finishedIdsRef = useRef(null)
  const pollNowRef = useRef(() => {})

  useEffect(() => {
    onRunFinishedRef.current = onRunFinished
  }, [onRunFinished])

  useEffect(() => {
    let disposed = false
    let timer = null
    let controller = null

    const reportFinished = (data) => {
      const finished = runsIn(data).filter(isFinished)
      if (finishedIdsRef.current === null) {
        // First successful poll: runs that ended before the page opened are not news.
        finishedIdsRef.current = new Set(finished.map((r) => r.id))
        return
      }
      const fresh = finished.filter((r) => !finishedIdsRef.current.has(r.id)).sort((a, b) => a.id - b.id)
      for (const run of fresh) {
        finishedIdsRef.current.add(run.id)
        onRunFinishedRef.current?.(run)
      }
    }

    const poll = async () => {
      clearTimeout(timer)
      controller?.abort()
      const ctrl = new AbortController()
      controller = ctrl
      try {
        const data = await getScrapeStatus(ctrl.signal)
        if (disposed || ctrl.signal.aborted) return
        setStatus(data)
        setError(null)
        reportFinished(data)
        timer = setTimeout(poll, data.is_scraping ? RUNNING_POLL_MS : IDLE_POLL_MS)
      } catch (err) {
        if (disposed || err.name === 'AbortError') return
        setError(err)
        timer = setTimeout(poll, ERROR_RETRY_MS)
      }
    }

    pollNowRef.current = poll
    poll()
    return () => {
      disposed = true
      clearTimeout(timer)
      controller?.abort()
      pollNowRef.current = () => {}
    }
  }, [])

  const refresh = useCallback(() => pollNowRef.current(), [])

  return {
    status,
    error,
    refresh,
    isScraping: Boolean(status?.is_scraping),
    currentRun: status?.current_run ?? null,
  }
}
