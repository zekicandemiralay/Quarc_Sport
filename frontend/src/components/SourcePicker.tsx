import { useTranslation } from 'react-i18next'
import { useSourceStore } from '../store/sourceStore'
import type { SourceId } from '../types'

/** Which source the dashboard shows. Only appears when there is a choice; sources are never mixed. */
export default function SourcePicker() {
  const { t } = useTranslation()
  const sources = useSourceStore(s => s.sources)
  const current = useSourceStore(s => s.current)
  const select = useSourceStore(s => s.select)

  if (sources.length < 2) return null

  return (
    <label className="flex items-center gap-2 text-xs text-slate-400" title={t('sources.onlyOne')}>
      <span className="hidden sm:inline">{t('sources.picker')}</span>
      <select
        value={current ?? ''}
        onChange={e => select(e.target.value as SourceId)}
        className="bg-slate-800 text-slate-100 text-sm rounded-lg px-3 py-1.5 border border-slate-700 focus:outline-none focus:border-blue-500"
      >
        {sources.map(s => (
          <option key={s.source} value={s.source}>{t(`sources.${s.source}`)}</option>
        ))}
      </select>
    </label>
  )
}
