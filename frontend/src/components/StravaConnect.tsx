import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { authorizeStrava, saveStravaApp, syncStravaNow, disconnectStrava, fetchSources } from '../api'
import { useStravaStatus } from '../lib/stravaStatus'
import { relativeTime } from '../lib/garminStatus'
import { openExternal } from '../lib/openExternal'
import { useSourceStore } from '../store/sourceStore'

// Settings → Strava. Everyone connects with their OWN Strava app (Strava's official way in): they
// register it once on strava.com, paste its Client ID and Secret here, then sign in on Strava's own
// page and allow access. Nobody depends on anyone else's app, or on Strava's limit for shared apps.
// Strava has activities only (no sleep, steps or daily health data), shown on their own.

const card   = 'bg-slate-900 border border-slate-800 rounded-2xl p-6'
const button = 'w-full bg-orange-600 hover:bg-orange-500 disabled:opacity-50 text-white font-medium rounded-lg py-2 text-sm transition-colors'
const quiet  = 'text-sm text-slate-400 hover:text-slate-200 hover:underline disabled:opacity-50'
const input  = 'w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-orange-500'

function Note({ kind, children }: { kind: 'error' | 'ok' | 'warn' | 'info'; children: React.ReactNode }) {
  const cls = {
    error: 'bg-red-900/30 border-red-700/50 text-red-300',
    ok:    'bg-green-900/30 border-green-700/50 text-green-300',
    warn:  'bg-amber-900/20 border-amber-700/40 text-amber-300',
    info:  'bg-blue-900/20 border-blue-700/40 text-blue-200',
  }[kind]
  return <div className={`border rounded-lg px-3 py-2 text-sm ${cls}`}>{children}</div>
}

/** A value to type into Strava's form, with a copy button. */
function CopyField({ label, value }: { label: string; value: string }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  async function copy() {
    try { await navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* the value is selectable as a fallback */ }
  }
  return (
    <div>
      <div className="text-xs text-slate-500 mb-0.5">{label}</div>
      <div className="flex items-center gap-2">
        <code className="flex-1 min-w-0 truncate bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 select-all">{value}</code>
        <button type="button" onClick={copy} className="text-xs text-orange-400 hover:text-orange-300 shrink-0 w-14 text-right">
          {copied ? t('settings.strava.copied') : t('settings.strava.copy')}
        </button>
      </div>
    </div>
  )
}

export default function StravaConnect() {
  const { t, i18n } = useTranslation()
  const [waiting, setWaiting] = useState(false)      // the person is on Strava's page
  const { status, refresh } = useStravaStatus(true, waiting)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)       // entering (or changing) the app's Client ID / Secret
  const [clientId, setClientId] = useState('')
  const [secret, setSecret] = useState('')

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

  async function startAuthorization() {
    const { url } = await authorizeStrava()
    await openExternal(url)
    setWaiting(true)
    setTimeout(() => setWaiting(false), 5 * 60 * 1000)      // give up waiting after five minutes
  }

  async function run(job: () => Promise<void>) {
    setBusy(true); setError(null)
    try { await job() } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }

  const saveAndConnect = (e: React.FormEvent) => {
    e.preventDefault()
    return run(async () => {
      await saveStravaApp(clientId, secret)
      setEditing(false); setClientId(''); setSecret('')
      await refresh()
      await startAuthorization()
    })
  }
  const connect = () => run(startAuthorization)
  const syncNow = () => run(async () => { await syncStravaNow(); await refresh() })
  const remove = (confirmKey: string) => run(async () => {
    if (!window.confirm(t(confirmKey))) return
    await disconnectStrava()
    setEditing(false)
    await refresh()
    useSourceStore.getState().setSources(await fetchSources())
  })

  if (!status) return <div className={card}><h2 className="text-sm font-semibold text-slate-200">Strava</h2></div>

  const needsApp = !status.app_saved || editing

  return (
    <div className={card}>
      <h2 className="text-sm font-semibold text-slate-200 mb-1">Strava</h2>
      <p className="text-xs text-slate-500 mb-4">{t('settings.strava.hint')}</p>

      <div className="space-y-3">
        {error && <Note kind="error">{error}</Note>}

        {/* ── 1. the person's own Strava app: guide + Client ID / Secret ── */}
        {needsApp && (
          <form onSubmit={saveAndConnect} className="space-y-3">
            <Note kind="warn">{t('settings.strava.subscriptionNote')}</Note>
            <p className="text-sm font-medium text-slate-200">{t('settings.strava.guideTitle')}</p>

            <ol className="space-y-3 text-sm text-slate-300 list-decimal pl-5 marker:text-slate-500">
              <li className="space-y-2">
                <span>{t('settings.strava.step1')}</span>
                <button type="button" onClick={() => openExternal('https://www.strava.com/settings/api')} className="block text-orange-400 hover:text-orange-300 underline text-xs">
                  strava.com/settings/api ↗
                </button>
              </li>
              <li className="space-y-2">
                <span>{t('settings.strava.step2')}</span>
                <CopyField label={t('settings.strava.fieldName')} value="Quarc Sport" />
                <CopyField label={t('settings.strava.fieldWebsite')} value={status.website || '—'} />
                <CopyField label={t('settings.strava.fieldCallback')} value={status.callback_domain || '—'} />
                <p className="text-xs text-slate-500">{t('settings.strava.step2Rest')}</p>
              </li>
              <li className="space-y-2">
                <span>{t('settings.strava.step3')}</span>
                <input
                  value={clientId} onChange={e => setClientId(e.target.value)} className={input}
                  placeholder={t('settings.strava.clientId')} inputMode="numeric" autoComplete="off" required
                />
                <input
                  value={secret} onChange={e => setSecret(e.target.value)} className={input} type="password"
                  placeholder={t('settings.strava.clientSecret')} autoComplete="off" required
                />
              </li>
            </ol>

            <button type="submit" disabled={busy || !status.configured || !clientId.trim() || !secret.trim()} className={button}>
              {t('settings.strava.saveAndConnect')}
            </button>
            {!status.configured && <Note kind="warn">{t('settings.strava.errors.no_public_url')}</Note>}
            {editing && <button type="button" onClick={() => setEditing(false)} className={quiet}>{t('common.cancel')}</button>}
            <p className="text-xs text-slate-500">{t('settings.strava.privacy')}</p>
          </form>
        )}

        {/* ── 2. app saved, access not allowed yet ── */}
        {!needsApp && !status.connected && (
          <>
            {waiting && <Note kind="info">{t('settings.strava.waiting')}</Note>}
            <p className="text-xs text-slate-500">{t('settings.strava.appSaved', { id: status.client_id })}</p>
            <button onClick={connect} disabled={busy} className={button}>{t('settings.strava.connect')}</button>
            <div className="flex items-center justify-between">
              <button onClick={() => setEditing(true)} className={quiet}>{t('settings.strava.changeApp')}</button>
              <button onClick={() => remove('settings.strava.removeConfirm')} disabled={busy} className="text-sm text-red-400 hover:underline disabled:opacity-50">
                {t('settings.strava.removeApp')}
              </button>
            </div>
          </>
        )}

        {/* ── 3. connected ── */}
        {!needsApp && status.connected && (
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

            <div className="flex items-center justify-between pt-1 flex-wrap gap-2">
              <button onClick={syncNow} disabled={busy || status.queued || status.syncing || status.status === 'auth_failed'} className={quiet}>
                {status.syncing || status.queued ? t('settings.strava.syncing') : t('settings.strava.syncNow')}
              </button>
              <button onClick={() => setEditing(true)} className={quiet}>{t('settings.strava.changeApp')}</button>
              <button onClick={() => remove('settings.strava.disconnectConfirm')} disabled={busy} className="text-sm text-red-400 hover:underline disabled:opacity-50">
                {t('settings.strava.disconnect')}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
