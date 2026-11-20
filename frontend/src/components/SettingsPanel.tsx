import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import useAuthStore from '../store/authStore'
import { setLanguage } from '../i18n'
import StorePromo from './StorePromo'
import GarminConnect from './GarminConnect'
import StravaConnect from './StravaConnect'
import {
  semverGt, getPlatform, getCurrentVersion, fetchLatestRelease, getDownloadUrl, installUpdate, NO_RELEASE,
  type Release,
} from '../lib/updateCheck'

interface Props {
  onBack: () => void
}

const card  = 'bg-slate-900 border border-slate-800 rounded-2xl p-6'
const input = 'w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500'

// ─── App updates ──────────────────────────────────────────────────────────────

function UpdateSection() {
  const { t } = useTranslation()
  const platform = getPlatform()
  const [currentVersion, setCurrentVersion] = useState<string | null>(null)
  const [release, setRelease] = useState<Release | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState<string | null>(null)
  const [installing, setInstalling] = useState(false)

  function check() {
    setLoading(true); setError(null)
    Promise.all([getCurrentVersion(platform), fetchLatestRelease()])
      .then(([v, r]) => { setCurrentVersion(v); setRelease(r) })
      .catch(e => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }
  useEffect(check, []) // eslint-disable-line react-hooks/exhaustive-deps

  const latest = release?.tag_name?.replace(/^v/, '') ?? null
  const hasUpdate = !!currentVersion && !!latest && semverGt(latest, currentVersion)
  const url = getDownloadUrl(release, platform)

  return (
    <div className={card}>
      <h2 className="text-sm font-semibold text-slate-200 mb-3">{t('updates.title')}</h2>
      {loading && <p className="text-sm text-slate-400">{t('updates.checking')}</p>}
      {error && !loading && (
        <p className="text-sm text-slate-500">
          {error === NO_RELEASE ? t('updates.noRelease') : `${t('updates.error')}: ${error}`}
        </p>
      )}
      {release && !loading && (
        <div className="space-y-2 text-sm">
          {currentVersion && (
            <div className="flex justify-between"><span className="text-slate-500">{t('updates.installed')}</span><span className="font-medium">v{currentVersion}</span></div>
          )}
          <div className="flex justify-between"><span className="text-slate-500">{t('updates.latest')}</span><span className="font-medium">{release.tag_name}</span></div>
          {currentVersion && (
            <div className={`rounded-lg px-3 py-2 font-medium ${hasUpdate ? 'bg-amber-500/10 text-amber-400' : 'bg-green-500/10 text-green-400'}`}>
              {hasUpdate ? t('updates.updateAvailable') : t('updates.upToDate')}
            </div>
          )}
          {platform !== 'web' && hasUpdate && url && (
            <button
              disabled={installing}
              onClick={() => { setInstalling(true); installUpdate(platform, url, latest!); setTimeout(() => setInstalling(false), 4000) }}
              className="w-full bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white font-medium rounded-lg py-2 text-sm transition-colors"
            >
              {installing ? t('updates.downloading') : t('updates.install')}
            </button>
          )}
          <StorePromo />
          {platform === 'web' && (
            <a href={release.html_url} target="_blank" rel="noopener noreferrer" className="block text-center text-xs text-slate-500 hover:underline">
              {t('updates.viewRelease')}
            </a>
          )}
        </div>
      )}
      <button onClick={check} className="mt-3 text-xs text-slate-500 hover:underline">{t('updates.refresh')}</button>
    </div>
  )
}

// ─── Settings ─────────────────────────────────────────────────────────────────

export default function SettingsPanel({ onBack }: Props) {
  const { t, i18n } = useTranslation()
  const user   = useAuthStore(s => s.user)
  const logout = useAuthStore(s => s.logout)

  return (
    <div className="min-h-screen bg-slate-950">
      <header className="border-b border-slate-800 px-6 py-3 flex items-center gap-4">
        <button onClick={onBack} className="text-sm text-slate-400 hover:text-white transition-colors">← {t('common.back')}</button>
        <h1 className="text-base font-semibold text-white">{t('settings.title')}</h1>
      </header>

      <div className="max-w-md mx-auto px-4 py-8 space-y-6">
        <div className={card}>
          <h2 className="text-sm font-semibold text-slate-200 mb-3">{t('settings.account')}</h2>
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs text-slate-500">{t('settings.signedInAs')}</div>
              <div className="text-sm font-medium text-slate-100">{user?.username}</div>
            </div>
            <button onClick={() => logout()} className="text-sm font-medium text-red-400 hover:text-red-300">
              {t('settings.logout')}
            </button>
          </div>
          <p className="text-xs text-slate-500 mt-4">{t('settings.accountHint')}</p>
        </div>

        <GarminConnect />

        <StravaConnect />

        <div className={card}>
          <h2 className="text-sm font-semibold text-slate-200 mb-3">{t('settings.language')}</h2>
          <select
            value={i18n.language.slice(0, 2)}
            onChange={e => setLanguage(e.target.value)}
            className={input}
          >
            <option value="en">English</option>
            <option value="tr">Türkçe</option>
          </select>
        </div>

        <UpdateSection />
      </div>
    </div>
  )
}
