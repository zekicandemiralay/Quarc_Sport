import { create } from 'zustand'
import type { SourceId, SourceInfo, SourcesResponse } from '../types'

// Data comes from ONE source at a time (Garmin, Strava or the phone's Health Connect) and
// sources are never mixed. This store holds which sources the signed-in person has, which
// one is shown, and what each one really has — so the dashboard only offers what exists.

const KEY = 'quarc_source'

interface SourceState {
  sources: SourceInfo[]
  current: SourceId | null
  loaded: boolean
  setSources: (res: SourcesResponse) => void
  select: (source: SourceId) => void
  reset: () => void
}

const stored = (): SourceId | null => {
  try {
    return (localStorage.getItem(KEY) as SourceId | null) || null
  } catch {
    return null
  }
}

export const useSourceStore = create<SourceState>()((set, get) => ({
  sources: [],
  current: stored(),
  loaded: false,

  setSources(res) {
    const listed = res.sources.map(s => s.source)
    const keep = get().current
    // keep the person's choice if that source is still there; otherwise the server's pick
    const current = keep && listed.includes(keep) ? keep : listed.includes(res.default) ? res.default : (listed[0] ?? res.default)
    set({ sources: res.sources, current, loaded: true })
  },

  select(source) {
    try {
      localStorage.setItem(KEY, source)
    } catch {
      /* storage unavailable: the choice still applies for this session */
    }
    set({ current: source })
  },

  reset() {
    set({ sources: [], current: stored(), loaded: false })
  },
}))

/** What the currently shown source has: has('daily_summary', 'steps', 'resting_hr') = any of those columns holds data. */
export function availability(sources: SourceInfo[], current: SourceId | null) {
  const info = sources.find(s => s.source === current)
  const available = info?.available ?? {}
  return {
    info,
    /** the source has rows in this table at all (even if every optional column is empty) */
    hasTable: (table: string) => available[table] !== undefined,
    /** at least one of these columns holds a value */
    has: (table: string, ...columns: string[]) => columns.some(c => available[table]?.includes(c)),
  }
}

export function useAvailability() {
  const sources = useSourceStore(s => s.sources)
  const current = useSourceStore(s => s.current)
  return availability(sources, current)
}
