import { useCallback, useEffect, useState } from 'react'
import { fetchGarminStatus } from '../api'
import type { GarminStatus } from '../types'

/** Something is still happening: a sync is queued/running or history is still coming in. */
export function importInProgress(s: GarminStatus | null): boolean {
  if (!s || s.status !== 'connected') return false
  return s.queued || s.syncing || s.first_import || !s.history_done
}

/**
 * The Garmin connection status, re-read every few seconds while an import runs
 * (quickly while the first month comes in, slower during the long history import).
 */
export function useGarminStatus(enabled = true) {
  const [status, setStatus] = useState<GarminStatus | null>(null)

  const refresh = useCallback(async () => {
    try {
      const s = await fetchGarminStatus()
      setStatus(s)
      return s
    } catch {
      return null
    }
  }, [])

  useEffect(() => {
    if (enabled) refresh()
  }, [enabled, refresh])

  const busy = enabled && importInProgress(status)
  const fast = busy && !!status && (status.queued || status.first_import)
  useEffect(() => {
    if (!busy) return
    const id = setInterval(refresh, fast ? 4000 : 15000)
    return () => clearInterval(id)
  }, [busy, fast, refresh])

  return { status, setStatus, refresh }
}

/** "5 min ago", "in 12 min", in the UI language. */
export function relativeTime(iso: string | null, lang: string): string {
  if (!iso) return ''
  const diffSec = (new Date(iso).getTime() - Date.now()) / 1000
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: 'auto' })
  const abs = Math.abs(diffSec)
  if (abs < 60) return rtf.format(Math.round(diffSec), 'second')
  if (abs < 3600) return rtf.format(Math.round(diffSec / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(diffSec / 3600), 'hour')
  return rtf.format(Math.round(diffSec / 86400), 'day')
}

/** "Mar 2021" for an ISO day, in the UI language. */
export function monthYear(isoDay: string | null, lang: string): string {
  if (!isoDay) return ''
  return new Date(`${isoDay}T00:00:00`).toLocaleDateString(lang, { month: 'short', year: 'numeric' })
}
