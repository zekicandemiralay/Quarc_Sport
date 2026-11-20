import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useStravaStatus, stravaBusy } from '../lib/stravaStatus'

// The strip above the dashboard while Strava is the shown source: import progress, or "reconnect".
// While an import runs it also tells the dashboard to reload when more activities have landed.

interface Props {
  onOpenSettings: () => void
  onNewData: () => void
}

const strip = 'rounded-xl px-4 py-3 text-sm flex flex-wrap items-center gap-3 border'

export default function StravaBanner({ onOpenSettings, onNewData }: Props) {
  const { t } = useTranslation()
  const { status } = useStravaStatus()

  const seen = useRef<string | null>(null)
  const key = status ? `${status.imported}|${status.last_sync_at}|${status.history_done}` : null
  useEffect(() => {
    if (!key) return
    if (seen.current !== null && seen.current !== key) onNewData()
    seen.current = key
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!status || !status.connected) return null

  if (status.status === 'auth_failed') {
    return (
      <div className={`${strip} border-red-700/50 bg-red-900/20 text-red-200`}>
        <span className="mr-auto">{t('settings.strava.authFailed')}</span>
        <button onClick={onOpenSettings} className="bg-blue-600 hover:bg-blue-500 text-white font-medium rounded-lg px-3 py-1.5 text-sm transition-colors">
          {t('settings.strava.reconnect')}
        </button>
      </div>
    )
  }

  if (stravaBusy(status) && !status.history_done) {
    return (
      <div className={`${strip} border-blue-700/40 bg-blue-900/20 text-blue-200`}>
        <span>{t('settings.strava.importing', { count: status.imported })}</span>
      </div>
    )
  }
  return null
}
