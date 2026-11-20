import { useState, useEffect, useCallback, useRef, Children } from 'react'
import { useTranslation } from 'react-i18next'
import { fetchDaily, fetchSleep, fetchHrv, fetchActivities, fetchSummary, fetchRange, fetchSources } from './api'
import useAuthStore from './store/authStore'
import { useSourceStore, useAvailability } from './store/sourceStore'
import SourcePicker from './components/SourcePicker'
import StravaBanner from './components/StravaBanner'
import { watchHealthConnect } from './lib/healthConnect'
import type { DailyRow, SleepRow, HrvRow, Activity, Summary, DataRange } from './types'

import LoginPage from './components/LoginPage'
import SettingsPanel from './components/SettingsPanel'
import GarminBanner from './components/GarminBanner'
import SummaryCards from './components/SummaryCards'
import BodyBatteryChart from './components/BodyBatteryChart'
import BodyBatteryDeltaChart from './components/BodyBatteryDeltaChart'
import SleepChart from './components/SleepChart'
import SleepDebtChart from './components/SleepDebtChart'
import SleepScheduleChart from './components/SleepScheduleChart'
import SleepRespirationChart from './components/SleepRespirationChart'
import StepsChart from './components/StepsChart'
import StepsRollingChart from './components/StepsRollingChart'
import HeartRateChart from './components/HeartRateChart'
import RollingHRChart from './components/RollingHRChart'
import DailyHRRangeChart from './components/DailyHRRangeChart'
import HrvChart from './components/HrvChart'
import CaloriesChart from './components/CaloriesChart'
import ActiveTimeChart from './components/ActiveTimeChart'
import FloorsChart from './components/FloorsChart'
import TrainingLoadChart from './components/TrainingLoadChart'
import ActivityHeatmap from './components/ActivityHeatmap'
import ActivitiesTable from './components/ActivitiesTable'
import ActivityMap from './components/ActivityMap'
import ActivityPaceChart from './components/ActivityPaceChart'
import PersonalBestsTable from './components/PersonalBestsTable'
import CountryStats from './components/CountryStats'
import TouringTab from './components/TouringTab'

// ─── Range ────────────────────────────────────────────────────────────────────

const PRESET_RANGES = [
  { label: '7d',  days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
  { label: '6m',  days: 180 },
  { label: '1y',  days: 365 },
  { label: 'All', days: null },
] as const  // shown via t(`range.${label}`)

type RangeLabel = typeof PRESET_RANGES[number]['label']

function today(): string {
  return new Date().toISOString().slice(0, 10)
}
function daysAgo(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d.toISOString().slice(0, 10)
}

// ─── Tabs ─────────────────────────────────────────────────────────────────────

const TABS = ['Overview', 'Health', 'Sleep', 'Activity', 'Touring'] as const
type Tab = typeof TABS[number]

// ─── Chart grid ───────────────────────────────────────────────────────────────

/** Two columns; children that render nothing (no data for this source) simply aren't there. */
function ChartGrid({ children }: { children: React.ReactNode }) {
  const items = Children.toArray(children).filter(Boolean)
  if (items.length === 0) return null
  return <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">{items}</div>
}

// ─── Spinner ──────────────────────────────────────────────────────────────────

function Spinner() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="w-8 h-8 border-2 border-slate-600 border-t-blue-400 rounded-full animate-spin" />
    </div>
  )
}

// ─── App ──────────────────────────────────────────────────────────────────────

type View = 'dashboard' | 'settings'

export default function App() {
  const { t } = useTranslation()
  const user = useAuthStore(s => s.user)
  const authChecked = !useAuthStore(s => s.loading)
  const logout = useAuthStore(s => s.logout)
  const userId = user?.id  // data reloads only when the signed-in person changes, not on every session refresh
  const source = useSourceStore(s => s.current)
  const sources = useSourceStore(s => s.sources)
  const sourcesLoaded = useSourceStore(s => s.loaded)
  const { info: sourceInfo, has, hasTable } = useAvailability()
  const [view, setView]   = useState<View>('dashboard')
  const [userMenuOpen, setUserMenuOpen] = useState(false)

  const [tab, setTab] = useState<Tab>('Overview')
  const [rangeLabel, setRangeLabel] = useState<RangeLabel | null>('30d')
  const [customStart, setCustomStart] = useState<string | null>(null)
  const [customEnd,   setCustomEnd]   = useState<string | null>(null)
  const [mapHighlightId, setMapHighlightId] = useState<number | null>(null)
  const [dataRange, setDataRange] = useState<DataRange>({ earliest: null, latest: null })

  const [daily, setDaily] = useState<DailyRow[]>([])
  const [sleep, setSleep] = useState<SleepRow[]>([])
  const [hrv, setHrv]   = useState<HrvRow[]>([])
  const [activities, setActivities] = useState<Activity[]>([])
  const [summary, setSummary] = useState<Summary | null>(null)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Check the existing login (shared quarc-auth cookie) on mount
  useEffect(() => { useAuthStore.getState().checkSession() }, [])

  // Android app: upload the phone's Health Connect data when the app opens and when it comes back
  useEffect(() => {
    if (userId) watchHealthConnect(() => { fetchSources().then(r => useSourceStore.getState().setSources(r)).catch(() => {}) })
  }, [userId])

  // Which sources this person has (and what each holds), once per login
  useEffect(() => {
    if (!userId) { useSourceStore.getState().reset(); return }
    fetchSources().then(r => useSourceStore.getState().setSources(r)).catch(() => {})
  }, [userId])

  // The data bounds of the shown source
  useEffect(() => {
    if (userId && sourcesLoaded) fetchRange().then(setDataRange).catch(() => {})
  }, [userId, source, sourcesLoaded])

  // Derive start/end — custom dates take priority over presets
  const { start, end } = (() => {
    if (customStart && customEnd) return { start: customStart, end: customEnd }
    const e = today()
    if (rangeLabel === 'All') return { start: dataRange.earliest ?? daysAgo(365), end: e }
    const preset = PRESET_RANGES.find(r => r.label === rangeLabel)!
    return { start: daysAgo(preset.days as number), end: e }
  })()

  // silent: refresh in place (new Garmin data arrived) instead of showing the spinner
  const load = useCallback((silent = false) => {
    if (!userId || !sourcesLoaded) return
    if (!silent) setLoading(true)
    setError(null)
    Promise.all([
      fetchDaily(start, end),
      fetchSleep(start, end),
      fetchHrv(start, end),
      fetchActivities(start, end),
      fetchSummary(),
    ])
      .then(([d, s, h, a, sum]) => {
        setDaily(d)
        setSleep(s)
        setHrv(h)
        setActivities(a)
        setSummary(sum)
      })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }, [start, end, userId, source, sourcesLoaded])

  useEffect(() => { load() }, [load])

  // The Garmin import stored new days: refresh the bounds and the charts quietly.
  const reloadAll = useCallback(() => {
    fetchSources().then(r => useSourceStore.getState().setSources(r)).catch(() => {})
    fetchRange().then(setDataRange).catch(() => {})
    load(true)
  }, [load])

  // Back from Settings (e.g. just connected Garmin): show whatever arrived meanwhile.
  const prevView = useRef(view)
  useEffect(() => {
    if (prevView.current !== 'dashboard' && view === 'dashboard') reloadAll()
    prevView.current = view
  }, [view, reloadAll])

  // Only offer what this source actually has. Nothing is calculated to fill a gap.
  const hasHealth = has('daily_summary', 'steps', 'resting_hr', 'min_hr_day', 'max_hr_day', 'active_calories', 'total_calories',
                        'active_time_seconds', 'floors_ascended', 'body_battery_high') || has('hrv', 'hrv_last_night', 'hrv_weekly_avg')
  const hasSleep = has('sleep', 'duration_seconds')
  const hasActivities = hasTable('activities')
  const hasRoutes = has('activities', 'polyline', 'start_lat')
  const visibleTabs = TABS.filter(tb =>
    tb === 'Overview' || (tb === 'Health' && hasHealth) || (tb === 'Sleep' && hasSleep) ||
    (tb === 'Activity' && hasActivities) || (tb === 'Touring' && hasRoutes))
  useEffect(() => {
    if (sourcesLoaded && !visibleTabs.includes(tab)) setTab('Overview')
  }, [sourcesLoaded, source, visibleTabs.join(','), tab]) // eslint-disable-line react-hooks/exhaustive-deps

  async function handleLogout() {
    setUserMenuOpen(false)
    setView('dashboard')
    await logout()
  }

  // ── Auth gate ───────────────────────────────────────────────────────────────

  if (!authChecked) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="w-8 h-8 border-2 border-slate-600 border-t-blue-400 rounded-full animate-spin" />
      </div>
    )
  }

  if (!user) {
    return <LoginPage />
  }

  // ── Settings view ──────────────────────────────────────────────────────────

  if (view === 'settings') {
    return <SettingsPanel onBack={() => setView('dashboard')} />
  }

  // ── Dashboard ──────────────────────────────────────────────────────────────

  // sources without daily rows (Strava) have no "N days" to report
  const dateLabel = daily.length > 0 ? t('app.dateLabel', { start, end, count: daily.length }) : t('app.dateRange', { start, end })

  return (
    <div className="min-h-screen bg-slate-950" onClick={() => setUserMenuOpen(false)}>
      {/* Header */}
      <header className="sticky top-0 z-10 border-b border-slate-800 bg-slate-950/95 backdrop-blur px-6 py-3 flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-3 mr-auto">
          <img src="/logo.png" alt="" className="w-8 h-8 rounded-lg shrink-0" />
          <div>
            <h1 className="text-base font-semibold text-white leading-tight">Quarc Sport</h1>
            <p className="text-xs text-slate-500 leading-tight">{dateLabel}</p>
          </div>
        </div>

        {/* Range selector */}
        <div className="flex bg-slate-800 rounded-lg p-1 gap-1">
          {PRESET_RANGES.map(({ label }) => (
            <button
              key={label}
              onClick={() => { setRangeLabel(label); setCustomStart(null); setCustomEnd(null) }}
              className={`px-3 py-1 rounded-md text-sm font-medium transition-colors ${
                rangeLabel === label ? 'bg-blue-600 text-white' : 'text-slate-400 hover:text-white'
              }`}
            >
              {t(`range.${label}`)}
            </button>
          ))}
          {customStart && customEnd && (
            <div className="px-3 py-1 rounded-md text-sm font-medium bg-blue-600 text-white">
              {customStart.slice(5)} – {customEnd.slice(5)}
            </div>
          )}
        </div>

        <SourcePicker />

        {/* User menu */}
        <div className="relative" onClick={e => e.stopPropagation()}>
          <button
            onClick={() => setUserMenuOpen(v => !v)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 transition-colors text-sm text-slate-200"
          >
            <svg className="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
            </svg>
            <span>{user.username}</span>
          </button>
          {userMenuOpen && (
            <div className="absolute right-0 mt-1 w-44 bg-slate-800 border border-slate-700 rounded-xl shadow-xl overflow-hidden z-50">
              <button
                onClick={() => { setView('settings'); setUserMenuOpen(false) }}
                className="w-full text-left px-4 py-2.5 text-sm text-slate-200 hover:bg-slate-700 flex items-center gap-2 transition-colors"
              >
                <svg className="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
                {t('settings.title')}
              </button>
              <div className="border-t border-slate-700" />
              <button
                onClick={handleLogout}
                className="w-full text-left px-4 py-2.5 text-sm text-red-400 hover:bg-slate-700 flex items-center gap-2 transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                </svg>
                {t('settings.logout')}
              </button>
            </div>
          )}
        </div>
      </header>

      {/* Tabs */}
      <div className="border-b border-slate-800 px-6">
        <div className="flex gap-1">
          {visibleTabs.map(tabKey => (
            <button
              key={tabKey}
              onClick={() => setTab(tabKey)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                tab === tabKey
                  ? 'border-blue-500 text-white'
                  : 'border-transparent text-slate-400 hover:text-slate-200'
              }`}
            >
              {t(`tabs.${tabKey}`)}
            </button>
          ))}
        </div>
      </div>

      <main className="px-6 py-6 max-w-7xl mx-auto space-y-5">
        {error && (
          <div className="bg-red-900/30 border border-red-700 text-red-300 rounded-xl px-4 py-3 text-sm">
            {t('app.loadFailed')}: {error}
          </div>
        )}

        {sourcesLoaded && sources.length === 0 ? (
          <div className="rounded-xl px-4 py-3 text-sm flex flex-wrap items-center gap-3 border border-blue-700/40 bg-blue-900/20 text-blue-200">
            <span className="mr-auto">{t('sources.connectNone')}</span>
            <button
              onClick={() => setView('settings')}
              className="bg-blue-600 hover:bg-blue-500 text-white font-medium rounded-lg px-3 py-1.5 text-sm transition-colors"
            >
              {t('sources.connectButton')}
            </button>
          </div>
        ) : (
          source === 'garmin' && <GarminBanner hasGarmin={user.has_garmin} onOpenSettings={() => setView('settings')} onNewData={reloadAll} />
          || (source === 'strava' && <StravaBanner onOpenSettings={() => setView('settings')} onNewData={reloadAll} />)
        )}

        {sourcesLoaded && sources.length > 0 && sourceInfo && !sourceInfo.has_data && (
          <div className="bg-slate-800 rounded-xl p-8 text-center">
            <p className="text-sm text-slate-300 font-medium">{t('sources.empty', { source: t(`sources.${sourceInfo.source}`) })}</p>
            <p className="text-xs text-slate-500 mt-1">{t('sources.emptyHint')}</p>
          </div>
        )}

        {!loading && sourcesLoaded && sourceInfo?.has_data && !daily.length && !sleep.length && !hrv.length && !activities.length && (
          <div className="bg-slate-800 rounded-xl p-6 text-center text-sm text-slate-400">{t('app.noDataInRange')}</div>
        )}

        {loading ? <Spinner /> : (
          <div key={source ?? 'none'} className="space-y-5">
            {/* ── Overview ─────────────────────────────────────────────── */}
            {tab === 'Overview' && (
              <>
                {summary && <SummaryCards summary={summary} />}
                <ChartGrid>
                  {has('daily_summary', 'body_battery_high') && <BodyBatteryChart data={daily} />}
                  {has('sleep', 'duration_seconds') && <SleepChart data={sleep} withScore={has('sleep', 'sleep_score')} />}
                  {has('daily_summary', 'steps') && <StepsRollingChart data={daily} />}
                  {has('daily_summary', 'resting_hr') && <RollingHRChart data={daily} />}
                </ChartGrid>
                {activities.length > 0 && dataRange.earliest && (
                  <ActivityHeatmap
                    data={activities}
                    earliest={dataRange.earliest}
                    latest={dataRange.latest ?? today()}
                  />
                )}
              </>
            )}

            {/* ── Health ───────────────────────────────────────────────── */}
            {tab === 'Health' && (
              <>
                {has('daily_summary', 'resting_hr', 'min_hr_day', 'max_hr_day') && <DailyHRRangeChart data={daily} />}
                <ChartGrid>
                  {has('daily_summary', 'resting_hr') && <RollingHRChart data={daily} />}
                  {has('hrv', 'hrv_last_night', 'hrv_weekly_avg') && <HrvChart data={hrv} />}
                  {has('daily_summary', 'body_battery_high') && <BodyBatteryChart data={daily} />}
                  {has('daily_summary', 'body_battery_high') && <BodyBatteryDeltaChart data={daily} />}
                  {has('daily_summary', 'active_calories', 'total_calories') && <CaloriesChart data={daily} />}
                  {has('daily_summary', 'active_time_seconds', 'sedentary_seconds') && <ActiveTimeChart data={daily} />}
                  {has('daily_summary', 'steps') && <StepsRollingChart data={daily} />}
                  {has('daily_summary', 'floors_ascended') && <FloorsChart data={daily} />}
                </ChartGrid>
              </>
            )}

            {/* ── Sleep ────────────────────────────────────────────────── */}
            {tab === 'Sleep' && (
              <ChartGrid>
                {has('sleep', 'duration_seconds') && <SleepChart data={sleep} withScore={has('sleep', 'sleep_score')} />}
                {has('sleep', 'duration_seconds') && <SleepDebtChart data={sleep} />}
                {has('sleep', 'avg_spo2', 'avg_respiration', 'sleep_score') && <SleepRespirationChart data={sleep} />}
                {has('sleep', 'start_time') && <SleepScheduleChart data={sleep} />}
              </ChartGrid>
            )}

            {/* ── Activity ─────────────────────────────────────────────── */}
            {tab === 'Activity' && (
              <>
                {hasRoutes && (
                  <ActivityMap
                    start={start}
                    end={end}
                    highlightedId={mapHighlightId}
                    onRangeChange={(s, e) => { setCustomStart(s); setCustomEnd(e); setRangeLabel(null) }}
                  />
                )}
                <ChartGrid>
                  {has('activities', 'avg_pace_sec_per_km') && <ActivityPaceChart data={activities} />}
                  <PersonalBestsTable
                    activities={activities}
                    daily={daily}
                    onSelectActivity={id => { setMapHighlightId(id); setTab('Activity'); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
                  />
                </ChartGrid>
                {has('activities', 'country') && <CountryStats start={start} end={end} />}
                {has('activities', 'distance_meters') && <TrainingLoadChart data={activities} />}
                {activities.length > 0 && dataRange.earliest && (
                  <ActivityHeatmap
                    data={activities}
                    earliest={dataRange.earliest}
                    latest={dataRange.latest ?? today()}
                  />
                )}
                <ActivitiesTable data={activities} onSelectActivity={id => { setMapHighlightId(id); setTab('Activity'); window.scrollTo({ top: 0, behavior: 'smooth' }) }} />
              </>
            )}
            {/* ── Touring ──────────────────────────────────────────────── */}
            {tab === 'Touring' && (
              <TouringTab start={start} end={end} />
            )}
          </div>
        )}
      </main>
    </div>
  )
}
