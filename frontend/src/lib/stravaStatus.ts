import { useCallback, useEffect, useState } from 'react'
import { fetchStravaStatus } from '../api'
import type { StravaStatus } from '../types'

/** Something is still happening: a sync is queued/running or the history is still coming in. */
export function stravaBusy(s: StravaStatus | null): boolean {
  if (!s || !s.connected || s.status !== 'connected') return false
  return s.queued || s.syncing || !s.history_done
}

/**
 * The Strava connection status, re-read every few seconds while an import runs.
 * `waitForConnect` polls faster for a while after the person left for Strava's page.
 */
export function useStravaStatus(enabled = true, waitForConnect = false) {
  const [status, setStatus] = useState<StravaStatus | null>(null)

  const refresh = useCallback(async () => {
    try {
      const s = await fetchStravaStatus()
      setStatus(s)
      return s
    } catch {
      return null
    }
  }, [])

  useEffect(() => {
    if (enabled) refresh()
  }, [enabled, refresh])

  const busy = enabled && (stravaBusy(status) || waitForConnect)
  useEffect(() => {
    if (!busy) return
    const id = setInterval(refresh, waitForConnect ? 3000 : status?.queued ? 4000 : 12000)
    return () => clearInterval(id)
  }, [busy, waitForConnect, status?.queued, refresh])

  // coming back to the tab/app after Strava's page: look right away
  useEffect(() => {
    if (!enabled) return
    const onVisible = () => { if (document.visibilityState === 'visible') refresh() }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [enabled, refresh])

  return { status, setStatus, refresh }
}
