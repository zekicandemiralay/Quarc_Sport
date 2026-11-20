import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useGarminStatus, monthYear } from '../lib/garminStatus'

// The strip above the dashboard: connect Garmin / Garmin needs attention / import
// progress. While an import runs it also tells the dashboard to reload when new
// days have landed, so the charts fill in by themselves.

interface Props {
  hasGarmin: boolean
  onOpenSettings: () => void
  onNewData: () => void
}

const strip = 'rounded-xl px-4 py-3 text-sm flex flex-wrap items-center gap-3 border'
const action = 'bg-blue-600 hover:bg-blue-500 text-white font-medium rounded-lg px-3 py-1.5 text-sm transition-colors'

export default function GarminBanner({ hasGarmin, onOpenSettings, onNewData }: Props) {
  const { t, i18n } = useTranslation()
  const { status } = useGarminStatus(hasGarmin)

  // Reload when the recent days were synced or the history import finished.
  const seen = useRef<string | null>(null)
  const key = status ? `${status.last_sync_at}|${status.history_done}` : null
  useEffect(() => {
    if (!key) return
    if (seen.current !== null && seen.current !== key) onNewData()
    seen.current = key
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!hasGarmin) {
    return (
      <div className={`${strip} bg-blue-900/20 border-blue-700/40 text-blue-200`}>
        <span className="mr-auto">{t('app.connectGarmin')}</span>
        <button onClick={onOpenSettings} className={action}>{t('app.connectGarminButton')}</button>
      </div>
    )
  }
  if (!status || status.status === 'not_connected') return null

  if (status.status === 'needs_code' || status.status === 'auth_failed') {
    return (
      <div className={`${strip} bg-amber-900/20 border-amber-700/40 text-amber-200`}>
        <span className="mr-auto">{t('app.garminAttention')}</span>
        <button onClick={onOpenSettings} className={action}>{t('app.openSettings')}</button>
      </div>
    )
  }
  if (status.status !== 'connected') return null

  if (status.first_import) {
    return (
      <div className={`${strip} bg-blue-900/20 border-blue-700/40 text-blue-200`}>
        <span className="inline-block w-3 h-3 rounded-full border-2 border-blue-300 border-t-transparent animate-spin" />
        <span>{t('app.importingRecent')}</span>
      </div>
    )
  }
  if (!status.history_done && status.history_from) {
    return (
      <div className={`${strip} bg-slate-900 border-slate-800 text-slate-400`}>
        <span className="mr-auto">
          {t('app.importingHistory', { date: monthYear(status.history_from, i18n.language), percent: status.history_percent ?? 0 })}
        </span>
        <div className="w-32 h-1.5 bg-slate-800 rounded-full overflow-hidden">
          <div className="h-full bg-green-600 transition-all" style={{ width: `${status.history_percent ?? 0}%` }} />
        </div>
      </div>
    )
  }
  return null
}
