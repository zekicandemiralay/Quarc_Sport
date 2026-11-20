import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authorizeStrava, syncStravaNow, disconnectStrava, fetchSources } from '../api'
import { useStravaStatus } from '../lib/stravaStatus'
import { relativeTime } from '../lib/garminStatus'
import { openExternal } from '../lib/openExternal'
import { useSourceStore } from '../store/sourceStore'

// Settings → Strava. One button: Strava asks the person to allow access, then sends them back.
// Strava has activities only (no sleep, steps or daily health data), and they are shown on their
// own, never mixed with Garmin's.

const card   = 'bg-slate-900 border border-slate-800 rounded-2xl p-6'
const button = 'w-full bg-orange-600 hover:bg-orange-500 disabled:opacity-50 text-white font-medium rounded-lg py-2 text-sm transition-colors'
const quiet  = 'text-sm text-slate-400 hover:text-slate-200 hover:underline disabled:opacity-50'

function Note({ kind, children }: { kind: 'error' | 'ok' | 'warn' | 'info'; children: React.ReactNode }) {
  const cls = {
    error: 'bg-red-900/30 border-red-700/50 text-red-300',
    ok:    'bg-green-900/30 border-green-700/50 text-green-300',
    warn:  'bg-amber-900/20 border-amber-700/40 text-amber-300',
    info:  'bg-blue-900/20 border-blue-700/40 text-blue-200',
  }[kind]
  return <div className={`border rounded-lg px-3 py-2 text-sm ${cls}`}>{children}</div>
}

export default function StravaConnect() {
  const { t, i18n } = useTranslation()
  const [waiting, setWaiting] = useState(false)      // the person is on Strava's page
  const { status, refresh } = useStravaStatus(true, waiting)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // connected while we were waiting: stop waiting, and let the app know there is a new source
  useEffect(() => {
    if (waiting && status?.connected) {
      setWaiting(false)
      fetchSources().then(r => useSourceStore.getState().setSources(r)).catch(() => {})
    }
  }, [waiting, status?.connected])

  function errorText(err: unknown): string {
    const key = `settings.strava.errors.${err instanceof Error ? err.message : ''}`
    return i18n.exists(key) ? t(key) : t('settings.strava.errors.generic')
  }

  async function connect() {
    setBusy(true); setError(null)
    try {
      const { url } = await authorizeStrava()
      await openExternal(url)
      setWaiting(true)
      setTimeout(() => setWaiting(false), 5 * 60 * 1000)      // give up waiting after five minutes
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  async function syncNow() {
    setBusy(true); setError(null)
    try { await syncStravaNow(); await refresh() } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }

  async function disconnect() {
    if (!window.confirm(t('settings.strava.disconnectConfirm'))) return
    setBusy(true); setError(null)
    try {
      await disconnectStrava()
      await refresh()
      const r = await fetchSources()
      useSourceStore.getState().setSources(r)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  if (!status) return <div className={card}><h2 className="text-sm font-semibold text-slate-200">Strava</h2></div>

  return (
    <div className={card}>
      <h2 className="text-sm font-semibold text-slate-200 mb-1">Strava</h2>
      <p className="text-xs text-slate-500 mb-4">{t('settings.strava.hint')}</p>

      <div className="space-y-3">
        {error && <Note kind="error">{error}</Note>}

        {!status.configured && !status.connected && <Note kind="warn">{t('settings.strava.notConfigured')}</Note>}

        {status.connected ? (
          <>
            <div className="text-sm text-slate-200">
              <span className="font-medium text-green-400">{t('settings.strava.connected')}</span>
              {status.athlete && <span className="text-slate-400"> · {status.athlete}</span>}
            </div>

            {status.status === 'auth_failed' && (
              <Note kind="error">
                {t('settings.strava.authFailed')}
                <button onClick={connect} disabled={busy} className="mt-2 block underline">{t('settings.strava.reconnect')}</button>
              </Note>
            )}
            {status.status === 'error' && <Note kind="warn">{t('settings.strava.temporaryError')}</Note>}
            {status.status === 'connected' && !status.history_done && (
              <Note kind="info">{t('settings.strava.importing', { count: status.imported })}</Note>
            )}
            {status.status === 'connected' && status.history_done && (
              <p className="text-xs text-slate-500">
                {t('settings.strava.imported', { count: status.imported })}
                {status.last_sync_at && ` · ${t('settings.strava.lastSync', { when: relativeTime(status.last_sync_at, i18n.language) })}`}
              </p>
            )}
            {status.retry_after && new Date(status.retry_after).getTime() > Date.now() && (
              <p className="text-xs text-amber-400">{t('settings.strava.rateLimited', { when: relativeTime(status.retry_after, i18n.language) })}</p>
            )}

            <div className="flex items-center justify-between pt-1">
              <button onClick={syncNow} disabled={busy || status.queued || status.syncing || status.status === 'auth_failed'} className={quiet}>
                {status.syncing || status.queued ? t('settings.strava.syncing') : t('settings.strava.syncNow')}
              </button>
              <button onClick={disconnect} disabled={busy} className="text-sm text-red-400 hover:underline disabled:opacity-50">
                {t('settings.strava.disconnect')}
              </button>
            </div>
          </>
        ) : (
          <>
            {waiting && <Note kind="info">{t('settings.strava.waiting')}</Note>}
            <button onClick={connect} disabled={busy || !status.configured} className={button}>
              {t('settings.strava.connect')}
            </button>
            <p className="text-xs text-slate-500">{t('settings.strava.privacy')}</p>
          </>
        )}
      </div>
    </div>
  )
}
