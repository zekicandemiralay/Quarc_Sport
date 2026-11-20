import type { DailyRow, SleepRow, HrvRow, Activity, MapActivity, Summary, DataRange, CountryStat, TouringData, TourSummary, TourDetail, User, GarminStatus, GarminConnectResult, SourcesResponse, StravaStatus, HealthConnectStatus } from './types'
import { apiUrl } from './lib/apiUrl'
import { useAuthStore } from './store/authStore'
import { useSourceStore } from './store/sourceStore'

// Login is a cookie issued by the shared quarc-auth service (see store/authStore.ts),
// so every request just has to send cookies along.

// ─── Fetch helpers ────────────────────────────────────────────────────────────

// Data routes are asked for ONE source (the one picked in the header); these aren't about data.
const NOT_SOURCE_ROUTES = ['/api/me', '/api/auth', '/api/garmin', '/api/strava', '/api/health-connect', '/api/sources', '/api/radar']

function withSource(path: string): string {
  if (!path.startsWith('/api/') || NOT_SOURCE_ROUTES.some(p => path.startsWith(p))) return path
  const source = useSourceStore.getState().current
  if (!source) return path
  return `${path}${path.includes('?') ? '&' : '?'}source=${source}`
}

/** A 401 means the session is gone (expired or revoked elsewhere): go back to the login screen. */
function checkStatus(res: Response): void {
  if (res.status === 401) useAuthStore.getState().sessionLost()
}

async function errorMessage(res: Response): Promise<string> {
  const body = await res.json().catch(() => ({})) as { detail?: unknown; error?: string }
  return (typeof body.detail === 'string' && body.detail) || body.error || `${res.status} ${res.statusText}`
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(apiUrl(withSource(path)), { credentials: 'include' })
  checkStatus(res)
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json()
}

async function send<T>(method: 'POST' | 'PUT', path: string, body: unknown): Promise<T> {
  const res = await fetch(apiUrl(withSource(path)), {
    method,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  checkStatus(res)
  if (!res.ok) throw new Error(await errorMessage(res))
  return res.json()
}

const post = <T,>(path: string, body: unknown) => send<T>('POST', path, body)
const put = <T,>(path: string, body: unknown) => send<T>('PUT', path, body)

async function del(path: string): Promise<void> {
  const res = await fetch(apiUrl(withSource(path)), { method: 'DELETE', credentials: 'include' })
  checkStatus(res)
  if (!res.ok) throw new Error(await errorMessage(res))
}

// ─── Sources ──────────────────────────────────────────────────────────────────

export function fetchSources(): Promise<SourcesResponse> {
  return get('/api/sources')
}

// ─── Strava ───────────────────────────────────────────────────────────────────

export function fetchStravaStatus(): Promise<StravaStatus> {
  return get('/api/strava/status')
}

/** Strava's "allow access" address for this person (open it outside the app). */
export function authorizeStrava(): Promise<{ url: string }> {
  return post('/api/strava/authorize', {})
}

export function syncStravaNow(): Promise<StravaStatus> {
  return post('/api/strava/sync', {})
}

/** Disconnects Strava and deletes the Strava activities stored here. */
export function disconnectStrava(): Promise<void> {
  return del('/api/strava')
}

// ─── Phone (Health Connect) ───────────────────────────────────────────────────

export function fetchHealthConnectStatus(): Promise<HealthConnectStatus> {
  return get('/api/health-connect/status')
}

/** What the Android app read from Health Connect; the server stores it as the "health_connect" source. */
export function ingestHealthConnect(body: {
  daily: Record<string, unknown>[]; sleep: Record<string, unknown>[]; activities: Record<string, unknown>[]; device?: string
}): Promise<{ daily: number; sleep: number; activities: number }> {
  return post('/api/health-connect/ingest', body)
}

/** Stops the phone as a source and deletes what it uploaded. */
export function disconnectHealthConnect(): Promise<void> {
  return del('/api/health-connect')
}

// ─── Me + Garmin connection ───────────────────────────────────────────────────

export function fetchMe(): Promise<User> {
  return get('/api/me')
}

// Errors from these come back as a code in Error.message (e.g. "invalid_credentials"),
// translated under settings.garmin.errors.

export function fetchGarminStatus(): Promise<GarminStatus> {
  return get('/api/garmin/status')
}

/** Checks the login with Garmin right away; two-factor accounts answer "needs_code". */
export function connectGarmin(email: string, password: string): Promise<GarminConnectResult> {
  return post('/api/garmin/connect', { email, password })
}

export function submitGarminCode(code: string): Promise<GarminConnectResult> {
  return post('/api/garmin/code', { code })
}

/** Signs in again with the saved login (Garmin wants a new code); no password needed. */
export function reconnectGarmin(): Promise<GarminConnectResult> {
  return post('/api/garmin/reconnect', {})
}

export async function cancelGarminCode(): Promise<void> {
  const res = await fetch(apiUrl('/api/garmin/cancel'), { method: 'POST', credentials: 'include' })
  checkStatus(res)
}

export function syncGarminNow(): Promise<GarminStatus> {
  return post('/api/garmin/sync', {})
}

export function deleteGarminCredentials(): Promise<void> {
  return del('/api/garmin/credentials')
}

// ─── Data ─────────────────────────────────────────────────────────────────────

export function fetchDaily(start: string, end: string): Promise<DailyRow[]> {
  return get(`/api/daily?start=${start}&end=${end}`)
}

export function fetchSleep(start: string, end: string): Promise<SleepRow[]> {
  return get(`/api/sleep?start=${start}&end=${end}`)
}

export function fetchHrv(start: string, end: string): Promise<HrvRow[]> {
  return get(`/api/hrv?start=${start}&end=${end}`)
}

export function fetchActivities(start: string, end: string): Promise<Activity[]> {
  return get(`/api/activities?start=${start}&end=${end}`)
}

export function fetchSummary(): Promise<Summary> {
  return get('/api/summary')
}

export function fetchRange(): Promise<DataRange> {
  return get('/api/range')
}

export function fetchMapActivities(start: string, end: string): Promise<MapActivity[]> {
  return get(`/api/activities/map?start=${start}&end=${end}`)
}

export function fetchCountryStats(start: string, end: string): Promise<CountryStat[]> {
  return get(`/api/activities/countries?start=${start}&end=${end}`)
}

export function fetchTouringData(start: string, end: string): Promise<TouringData> {
  return get(`/api/touring?start=${start}&end=${end}`)
}

export function fetchTours(): Promise<TourSummary[]> {
  return get('/api/tours')
}

export function createTour(name: string, description: string | null, activity_ids: number[]): Promise<{ id: number }> {
  return post('/api/tours', { name, description, activity_ids })
}

export function fetchTourDetail(id: number): Promise<TourDetail> {
  return get(`/api/tours/${id}`)
}

export function updateTour(id: number, data: { name: string; description: string | null }): Promise<{ ok: boolean }> {
  return put(`/api/tours/${id}`, data)
}

export function deleteTour(id: number): Promise<void> {
  return del(`/api/tours/${id}`)
}

export function fetchWeatherGrid(activityId: number): Promise<{ points: import('./types').WeatherGridPoint[] }> {
  return get(`/api/activities/${activityId}/weather-grid`)
}

export function fetchRadarTimestamps(activityId: number): Promise<{ timestamps: number[]; zoom: number }> {
  return get(`/api/activities/${activityId}/radar-timestamps`)
}

export function fetchWeatherGridRegion(
  minLat: number, maxLat: number, minLng: number, maxLng: number,
  startDate: string, endDate: string,
): Promise<{ points: import('./types').WeatherGridPoint[] }> {
  return get(
    `/api/weather-grid?min_lat=${minLat}&max_lat=${maxLat}&min_lng=${minLng}&max_lng=${maxLng}&start_date=${startDate}&end_date=${endDate}`
  )
}
