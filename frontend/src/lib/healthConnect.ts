import { ingestHealthConnect } from '../api'

// The phone as a source: the Android app reads Health Connect through the native plugin
// (HealthConnectPlugin.kt) and uploads what it finds. This file is the glue; everything is
// a no-op outside the Android app.

interface HcPlugin {
  availability(): Promise<{ status: 'available' | 'update_required' | 'unavailable' }>
  permissions(): Promise<{ granted: boolean; complete: boolean; history: boolean }>
  requestAccess(): Promise<{ granted: boolean; complete: boolean; history: boolean }>
  openHealthConnect(): Promise<void>
  read(opts: { days: number }): Promise<HcPayload>
}

export interface HcPayload {
  daily: Record<string, unknown>[]
  sleep: Record<string, unknown>[]
  activities: Record<string, unknown>[]
}

type NativeWindow = {
  Capacitor?: { isNativePlatform?: () => boolean; Plugins: Record<string, any> }
}
const w = () => (globalThis as unknown as { window?: NativeWindow }).window

export const isAndroidApp = (): boolean => !!w()?.Capacitor?.isNativePlatform?.()

function plugin(): HcPlugin | null {
  return isAndroidApp() ? ((w()!.Capacitor!.Plugins.HealthConnect as HcPlugin) ?? null) : null
}

// What the person turned on in this app, and when it last uploaded. Per device (not synced).
const ENABLED_KEY = 'quarc_hc_enabled'
const LAST_KEY = 'quarc_hc_last_upload'

const store = {
  get: (k: string) => { try { return localStorage.getItem(k) } catch { return null } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v) } catch { /* private mode: it just won't be remembered */ } },
  del: (k: string) => { try { localStorage.removeItem(k) } catch { /* ignore */ } },
}

export const hcEnabled = () => store.get(ENABLED_KEY) === '1'
export const setHcEnabled = (on: boolean) => (on ? store.set(ENABLED_KEY, '1') : (store.del(ENABLED_KEY), store.del(LAST_KEY)))
export const lastHcUpload = (): Date | null => {
  const v = store.get(LAST_KEY)
  return v ? new Date(v) : null
}

export type HcState =
  | { state: 'unsupported' }                                   // not the Android app
  | { state: 'unavailable' | 'update_required' }               // Health Connect missing / too old
  | { state: 'needs_permission'; history: boolean }
  | { state: 'ready'; history: boolean; complete: boolean }

export async function getHcState(): Promise<HcState> {
  const p = plugin()
  if (!p) return { state: 'unsupported' }
  const { status } = await p.availability()
  if (status !== 'available') return { state: status }
  const perms = await p.permissions()
  return perms.granted
    ? { state: 'ready', history: perms.history, complete: perms.complete }
    : { state: 'needs_permission', history: false }
}

export async function requestHcAccess(): Promise<HcState> {
  const p = plugin()
  if (!p) return { state: 'unsupported' }
  await p.requestAccess()
  return getHcState()
}

export const openHealthConnect = async () => { await plugin()?.openHealthConnect() }

const CHUNK = 700          // rows of one kind per upload (the server accepts 800)
const DAY_MS = 86_400_000

/** How far back to read: the first time as far as allowed (a year with the history permission, else 30 days),
 *  afterwards just the last week, which also picks up late-arriving data and edits. */
export function daysToRead(hasHistory: boolean, lastUpload: Date | null): number {
  if (!lastUpload) return hasHistory ? 365 : 30
  return 7
}

/** Reads from Health Connect and uploads in chunks. Returns how many rows were sent. */
export async function syncHealthConnect(opts: { device?: string } = {}): Promise<{ daily: number; sleep: number; activities: number }> {
  const p = plugin()
  if (!p) throw new Error('unsupported')
  const perms = await p.permissions()
  if (!perms.granted) throw new Error('permission')

  const payload = await p.read({ days: daysToRead(perms.history, lastHcUpload()) })
  const total = { daily: 0, sleep: 0, activities: 0 }
  const rounds = Math.max(
    Math.ceil(payload.daily.length / CHUNK), Math.ceil(payload.sleep.length / CHUNK), Math.ceil(payload.activities.length / CHUNK), 1,
  )
  for (let i = 0; i < rounds; i++) {
    const slice = (rows: Record<string, unknown>[]) => rows.slice(i * CHUNK, (i + 1) * CHUNK)
    const body = { daily: slice(payload.daily), sleep: slice(payload.sleep), activities: slice(payload.activities), device: opts.device }
    const sent = await ingestHealthConnect(body)
    total.daily += sent.daily; total.sleep += sent.sleep; total.activities += sent.activities
  }
  store.set(LAST_KEY, new Date().toISOString())
  return total
}

// ─── automatic sync while the app is used ─────────────────────────────────────

const MIN_GAP_MS = 10 * 60 * 1000
let running = false

/** Uploads if the person turned syncing on and the last upload is a while ago. Never throws. */
export async function autoSyncHealthConnect(force = false): Promise<boolean> {
  if (!isAndroidApp() || !hcEnabled() || running) return false
  const last = lastHcUpload()
  if (!force && last && Date.now() - last.getTime() < MIN_GAP_MS) return false
  running = true
  try {
    await syncHealthConnect()
    return true
  } catch {
    return false
  } finally {
    running = false
  }
}

let watching = false

/** Sync when the app opens and each time it comes back to the foreground. */
export function watchHealthConnect(onSynced?: () => void): void {
  if (watching || !isAndroidApp()) return
  watching = true
  const run = () => autoSyncHealthConnect().then(ok => ok && onSynced?.())
  run()
  w()?.Capacitor?.Plugins?.App?.addListener?.('resume', run)
}

export { DAY_MS }
