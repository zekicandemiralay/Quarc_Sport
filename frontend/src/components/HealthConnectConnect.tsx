import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { fetchHealthConnectStatus, disconnectHealthConnect, fetchSources } from '../api'
import {
  isAndroidApp, getHcState, requestHcAccess, openHealthConnect, syncHealthConnect, hcEnabled, setHcEnabled, lastHcUpload,
  type HcState,
} from '../lib/healthConnect'
import { relativeTime } from '../lib/garminStatus'
import { useSourceStore } from '../store/sourceStore'
import type { HealthConnectStatus } from '../types'

// Settings → Phone (Health Connect). Android only: Health Connect has no cloud API, so the Android
// app reads it on the phone and uploads it. On other platforms this just explains that.

const card   = 'bg-slate-900 border border-slate-800 rounded-2xl p-6'
const button = 'w-full bg-teal-700 hover:bg-teal-600 disabled:opacity-50 text-white font-medium rounded-lg py-2 text-sm transition-colors'
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

export default function HealthConnectConnect() {
  const { t, i18n } = useTranslation()
  const android = isAndroidApp()
  const [hc, setHc] = useState<HcState | null>(null)
  const [server, setServer] = useState<HealthConnectStatus | null>(null)
  const [enabled, setEnabled] = useState(hcEnabled())
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ kind: 'error' | 'ok'; text: string } | null>(null)

  const refresh = useCallback(async () => {
    try { setServer(await fetchHealthConnectStatus()) } catch { /* offline: keep what we had */ }
    if (android) {
      try { setHc(await getHcState()) } catch { setHc({ state: 'unavailable' }) }
    }
  }, [android])

  useEffect(() => { refresh() }, [refresh])

  async function updateSources() {
    try { useSourceStore.getState().setSources(await fetchSources()) } catch { /* ignore */ }
  }

  async function run(job: () => Promise<void>) {
    setBusy(true); setMessage(null)
    try { await job() } catch (e) {
      const code = e instanceof Error ? e.message : ''
      setMessage({ kind: 'error', text: code === 'permission' ? t('settings.phone.errors.permission') : t('settings.phone.errors.generic') })
    } finally { setBusy(false) }
  }

  const allow = () => run(async () => { setHc(await requestHcAccess()) })

  const start = () => run(async () => {
    setHcEnabled(true); setEnabled(true)
    const sent = await syncHealthConnect({ device: 'Android' })
    setMessage({ kind: 'ok', text: t('settings.phone.uploaded', sent) })
    await refresh(); await updateSources()
  })

  const syncNow = () => run(async () => {
    const sent = await syncHealthConnect({ device: 'Android' })
    setMessage({ kind: 'ok', text: t('settings.phone.uploaded', sent) })
    await refresh(); await updateSources()
  })

  const disconnect = () => run(async () => {
    if (!window.confirm(t('settings.phone.disconnectConfirm'))) return
    await disconnectHealthConnect()
    setHcEnabled(false); setEnabled(false)
    await refresh(); await updateSources()
  })

  const last = lastHcUpload() ?? (server?.last_sync_at ? new Date(server.last_sync_at) : null)
  const connected = !!server?.connected

  return (
    <div className={card}>
      <h2 className="text-sm font-semibold text-slate-200 mb-1">{t('settings.phone.title')}</h2>
      <p className="text-xs text-slate-500 mb-4">{t('settings.phone.hint')}</p>

      <div className="space-y-3">
        {message && <Note kind={message.kind}>{message.text}</Note>}

        {!android && (
          <>
            <Note kind="info">{t('settings.phone.androidOnly')}</Note>
            {connected && (
              <div className="flex items-center justify-between">
                <span className="text-sm text-slate-300">{t('settings.phone.connectedFrom', { when: last ? relativeTime(last.toISOString(), i18n.language) : '—' })}</span>
                <button onClick={disconnect} disabled={busy} className="text-sm text-red-400 hover:underline disabled:opacity-50">{t('settings.phone.disconnect')}</button>
              </div>
            )}
          </>
        )}

        {android && hc?.state === 'unavailable' && (
          <>
            <Note kind="warn">{t('settings.phone.unavailable')}</Note>
            <button onClick={() => run(openHealthConnect)} className={button}>{t('settings.phone.install')}</button>
          </>
        )}
        {android && hc?.state === 'update_required' && (
          <>
            <Note kind="warn">{t('settings.phone.updateRequired')}</Note>
            <button onClick={() => run(openHealthConnect)} className={button}>{t('settings.phone.update')}</button>
          </>
        )}
        {android && hc?.state === 'needs_permission' && (
          <>
            <p className="text-sm text-slate-300">{t('settings.phone.explain')}</p>
            <button onClick={allow} disabled={busy} className={button}>{t('settings.phone.allow')}</button>
          </>
        )}
        {android && hc?.state === 'ready' && (
          <>
            {enabled && connected ? (
              <>
                <div className="text-sm text-slate-200">
                  <span className="font-medium text-green-400">{t('settings.phone.syncing')}</span>
                  <span className="text-slate-400"> · {t('settings.phone.lastUpload', { when: last ? relativeTime(last.toISOString(), i18n.language) : '—' })}</span>
                </div>
                {server && <p className="text-xs text-slate-500">{t('settings.phone.counts', server.counts)}</p>}
              </>
            ) : (
              <button onClick={start} disabled={busy} className={button}>{t('settings.phone.start')}</button>
            )}
            {!hc.history && <Note kind="info">{t('settings.phone.historyHint')}</Note>}
            {!hc.complete && <p className="text-xs text-slate-500">{t('settings.phone.partial')}</p>}
            <div className="flex items-center justify-between pt-1 flex-wrap gap-2">
              {enabled && connected && <button onClick={syncNow} disabled={busy} className={quiet}>{busy ? t('settings.phone.working') : t('settings.phone.syncNow')}</button>}
              <button onClick={() => run(async () => { await requestHcAccess().then(setHc) })} disabled={busy} className={quiet}>{t('settings.phone.managePermissions')}</button>
              {(enabled || connected) && <button onClick={disconnect} disabled={busy} className="text-sm text-red-400 hover:underline disabled:opacity-50">{t('settings.phone.disconnect')}</button>}
            </div>
          </>
        )}
        {android && !hc && <p className="text-sm text-slate-500">{t('common.loading')}</p>}
      </div>
    </div>
  )
}
