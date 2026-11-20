import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  connectGarmin, submitGarminCode, reconnectGarmin, cancelGarminCode, syncGarminNow, deleteGarminCredentials,
} from '../api'
import useAuthStore from '../store/authStore'
import { useGarminStatus, relativeTime, monthYear } from '../lib/garminStatus'
import type { GarminConnectResult } from '../types'

// Settings → Garmin Connect. Everyone connects their own account here, two-factor
// code included: the server checks the login with Garmin while they wait, and the
// import starts right away (latest month first, then older history).

const card   = 'bg-slate-900 border border-slate-800 rounded-2xl p-6'
const input  = 'w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500'
const button = 'w-full bg-green-700 hover:bg-green-600 disabled:opacity-50 text-white font-medium rounded-lg py-2 text-sm transition-colors'
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

export default function GarminConnect() {
  const { t, i18n } = useTranslation()
  const lang = i18n.language
  const { status, setStatus, refresh } = useGarminStatus()

  const [step, setStep]       = useState<'form' | 'code'>('form')
  const [editing, setEditing] = useState(false)   // entering a login while one is saved
  const [email, setEmail]     = useState('')
  const [pass, setPass]       = useState('')
  const [code, setCode]       = useState('')
  const [method, setMethod]   = useState<GarminConnectResult['method']>(null)
  const [busy, setBusy]       = useState(false)
  const [error, setError]     = useState<string | null>(null)
  const [justConnected, setJustConnected] = useState(false)

  // Reopened Settings while Garmin's code step was still open on the server
  useEffect(() => { if (status?.pending_code) setStep('code') }, [status?.pending_code])

  function errorText(err: unknown): string {
    const key = `settings.garmin.errors.${err instanceof Error ? err.message : ''}`
    return i18n.exists(key) ? t(key) : t('settings.garmin.errors.generic')
  }

  async function connected() {
    setStep('form'); setEditing(false); setJustConnected(true)
    setEmail(''); setPass(''); setCode('')
    await Promise.all([refresh(), useAuthStore.getState().checkSession()])
  }

  async function handle(result: GarminConnectResult) {
    if (result.status === 'needs_code') {
      setMethod(result.method ?? null); setStep('code'); setCode(''); setPass('')
    } else {
      await connected()
    }
  }

  async function run(action: () => Promise<void>) {
    setBusy(true); setError(null); setJustConnected(false)
    try { await action() } catch (err) {
      setError(errorText(err))
      if (err instanceof Error && err.message === 'code_expired') setStep('form')
      refresh()
    } finally { setBusy(false) }
  }

  const connect = (e: React.FormEvent) => {
    e.preventDefault()
    if (!email.trim() || !pass) return
    run(async () => handle(await connectGarmin(email.trim(), pass)))
  }
  const confirmCode = (e: React.FormEvent) => {
    e.preventDefault()
    if (!code.trim()) return
    run(async () => handle(await submitGarminCode(code)))
  }
  const sendNewCode = () => run(async () => handle(await reconnectGarmin()))
  const syncNow     = () => run(async () => setStatus(await syncGarminNow()))
  const cancelCode  = () => run(async () => { await cancelGarminCode(); setStep('form'); await refresh() })
  const disconnect  = () => {
    if (!window.confirm(t('settings.garmin.removeConfirm'))) return
    run(async () => {
      await deleteGarminCredentials()
      setEditing(false)
      await Promise.all([refresh(), useAuthStore.getState().checkSession()])
    })
  }

  const isConnected = !!status && status.status !== 'not_connected'

  // ── what to say about the connection / import ──
  function stateLine() {
    if (!status) return null
    switch (status.status) {
      case 'needs_code':
        return (
          <Note kind="warn">
            <p>{t('settings.garmin.state.needsCode')}</p>
            <button onClick={sendNewCode} disabled={busy} className="mt-2 font-medium underline">{t('settings.garmin.sendCode')}</button>
          </Note>
        )
      case 'auth_failed':
        return (
          <Note kind="error">
            <p>{t('settings.garmin.state.authFailed')}</p>
            <button onClick={() => { setEditing(true); setError(null) }} className="mt-2 font-medium underline">{t('settings.garmin.reenter')}</button>
          </Note>
        )
      case 'error':
        return (
          <Note kind="warn">
            {status.retry_after
              ? t('settings.garmin.state.error', { when: relativeTime(status.retry_after, lang) })
              : t('settings.garmin.state.errorSoon')}
          </Note>
        )
    }
    if (status.first_import) {
      return <Note kind="info">{status.queued && !status.syncing ? t('settings.garmin.state.queued') : t('settings.garmin.state.firstImport')}</Note>
    }
    return (
      <div className="space-y-2">
        <div className="text-xs text-slate-400">
          {status.syncing || status.queued
            ? t('settings.garmin.state.syncing')
            : t('settings.garmin.state.upToDate', { when: relativeTime(status.last_sync_at, lang) })}
        </div>
        {!status.history_done && status.history_from && (
          <div>
            <div className="text-xs text-slate-400 mb-1">
              {t('settings.garmin.state.history', { date: monthYear(status.history_from, lang), percent: status.history_percent ?? 0 })}
            </div>
            <div className="h-1.5 bg-slate-800 rounded-full overflow-hidden">
              <div className="h-full bg-green-600 transition-all" style={{ width: `${status.history_percent ?? 0}%` }} />
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className={card}>
      <h2 className="text-sm font-semibold text-slate-200 mb-1">{t('settings.garmin.title')}</h2>
      <p className="text-xs text-slate-500 mb-4">{t('settings.garmin.hint')}</p>

      {error && <div className="mb-3"><Note kind="error">{error}</Note></div>}
      {justConnected && <div className="mb-3"><Note kind="ok">{t('settings.garmin.justConnected')}</Note></div>}

      {!status ? (
        <p className="text-sm text-slate-500">{t('common.loading')}</p>
      ) : step === 'code' ? (
        // ── two-factor code ──
        <form onSubmit={confirmCode} className="space-y-3">
          <p className="text-sm text-slate-300">
            {method === 'email' ? t('settings.garmin.codeSentEmail')
              : method === 'phone' ? t('settings.garmin.codeSentPhone')
              : t('settings.garmin.codeSent')}
          </p>
          <input
            value={code} onChange={e => setCode(e.target.value)} required autoFocus
            inputMode="numeric" autoComplete="one-time-code" maxLength={10}
            className={`${input} text-center text-lg tracking-[0.4em]`} placeholder="123456"
            aria-label={t('settings.garmin.code')}
          />
          <button type="submit" disabled={busy} className={button}>
            {busy ? t('settings.garmin.checking') : t('settings.garmin.confirmCode')}
          </button>
          <button type="button" onClick={cancelCode} disabled={busy} className={`${quiet} w-full`}>{t('common.cancel')}</button>
        </form>
      ) : isConnected && !editing ? (
        // ── connected ──
        <div className="space-y-4">
          <div className="text-sm text-green-400 font-medium">
            {status.email ? t('settings.garmin.connectedAs', { email: status.email }) : t('settings.garmin.connected')}
          </div>
          {stateLine()}
          <div className="flex flex-col gap-2">
            {(status.status === 'connected' || status.status === 'error') && (
              <button onClick={syncNow} disabled={busy || status.queued || status.syncing} className={button}>
                {t('settings.garmin.syncNow')}
              </button>
            )}
            <button onClick={() => { setEditing(true); setError(null); setJustConnected(false) }} className={quiet}>
              {t('settings.garmin.change')}
            </button>
            <button onClick={disconnect} disabled={busy} className="text-sm text-red-400 hover:underline disabled:opacity-50">
              {t('settings.garmin.remove')}
            </button>
          </div>
        </div>
      ) : (
        // ── email + password ──
        <form onSubmit={connect} className="space-y-3">
          {!isConnected && <div className="text-xs font-medium text-amber-400">{t('settings.garmin.notConnected')}</div>}
          <div className="space-y-1">
            <label className="text-xs text-slate-400">{t('settings.garmin.email')}</label>
            <input
              type="email" autoComplete="off" value={email} required
              onChange={e => setEmail(e.target.value)} className={input} placeholder="you@example.com"
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-slate-400">{t('settings.garmin.password')}</label>
            <input
              type="password" autoComplete="new-password" value={pass} required
              onChange={e => setPass(e.target.value)} className={input} placeholder="••••••••"
            />
          </div>
          <button type="submit" disabled={busy} className={button}>
            {busy ? t('settings.garmin.checking') : t('settings.garmin.connect')}
          </button>
          {editing && (
            <button type="button" onClick={() => { setEditing(false); setError(null) }} className={`${quiet} w-full`}>
              {t('common.cancel')}
            </button>
          )}
        </form>
      )}
    </div>
  )
}
