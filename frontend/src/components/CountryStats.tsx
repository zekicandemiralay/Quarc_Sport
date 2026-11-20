import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { locale, typeLabel } from '../i18n'
import { fetchCountryStats } from '../api'
import type { CountryStat, CountryTypeStat } from '../types'

const regionNames = new Map<string, Intl.DisplayNames>()
const nameOf = (code: string) => {
  const lang = locale()
  if (!regionNames.has(lang)) regionNames.set(lang, new Intl.DisplayNames([lang], { type: 'region' }))
  try { return regionNames.get(lang)!.of(code) ?? code } catch { return code }
}

function flagOf(code: string): string {
  return code.toUpperCase().split('').map(c => String.fromCodePoint(c.charCodeAt(0) + 0x1F1A5)).join('')
}

function fmtKm(km: number)  { return km >= 1 ? `${km.toFixed(0)} km`  : `${(km * 1000).toFixed(0)} m` }
function fmtHr(h: number)   { return h >= 1  ? `${h.toFixed(0)}h`     : `${(h * 60).toFixed(0)}m` }
function fmtElev(m: number) { return m > 0   ? `+${m.toLocaleString()} m` : '—' }

const TYPE_COLOR: Record<string, string> = {
  CYCLING:         'bg-blue-500/20 text-blue-300',
  TRAIL_RUNNING:   'bg-orange-500/20 text-orange-300',
  RUNNING:         'bg-yellow-500/20 text-yellow-300',
  HIKING:          'bg-green-500/20 text-green-300',
  WALKING:         'bg-teal-500/20 text-teal-300',
  SWIMMING:        'bg-cyan-500/20 text-cyan-300',
  OPEN_WATER_SWIMMING: 'bg-cyan-500/20 text-cyan-300',
  MOUNTAIN_BIKING: 'bg-purple-500/20 text-purple-300',
}
function typeColor(type: string) { return TYPE_COLOR[type] ?? 'bg-slate-500/20 text-slate-300' }

function TypeRow({ ts }: { ts: CountryTypeStat }) {
  const { t } = useTranslation()
  return (
    <tr className="border-t border-slate-700/50 text-xs">
      <td className="py-1.5 pl-6 pr-2">
        <span className={`px-2 py-0.5 rounded-full font-medium ${typeColor(ts.type)}`}>
          {typeLabel(ts.type)}
        </span>
      </td>
      <td className="py-1.5 px-2 text-right text-slate-400">{ts.count}</td>
      <td className="py-1.5 px-2 text-right text-slate-200">{ts.total_km > 0 ? fmtKm(ts.total_km) : '—'}</td>
      <td className="py-1.5 px-2 text-right text-slate-300">{fmtHr(ts.total_hours)}</td>
      <td className="py-1.5 px-2 text-right text-slate-400">{fmtElev(ts.total_elevation_m)}</td>
      <td className="py-1.5 pl-2 pr-3 text-right text-slate-400">{ts.avg_hr ? t('countries.bpm', { hr: ts.avg_hr }) : '—'}</td>
    </tr>
  )
}

function CountryRow({ c }: { c: CountryStat }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const name = nameOf(c.country)
  const flag = flagOf(c.country)

  return (
    <>
      <tr
        className="border-t border-slate-700 cursor-pointer hover:bg-slate-700/40 transition-colors"
        onClick={() => setOpen(o => !o)}
      >
        <td className="py-2.5 px-3">
          <div className="flex items-center gap-2">
            <span className="text-lg leading-none">{flag}</span>
            <div>
              <div className="text-sm font-medium text-slate-200">{name}</div>
              <div className="text-xs text-slate-500">{t('countries.activities', { count: c.total_activities })}</div>
            </div>
          </div>
        </td>
        <td className="py-2.5 px-3 text-right">
          <span className="text-sm font-semibold text-slate-100">{c.total_km > 0 ? fmtKm(c.total_km) : '—'}</span>
        </td>
        <td className="py-2.5 px-3 text-right text-sm text-slate-300">{fmtHr(c.total_hours)}</td>
        <td className="py-2.5 px-3 text-right text-sm text-slate-400">{fmtElev(c.total_elevation_m)}</td>
        <td className="py-2.5 px-3 text-right">
          <div className="flex flex-wrap gap-1 justify-end">
            {c.types.map(ts => (
              <span key={ts.type} className={`text-xs px-1.5 py-0.5 rounded-full ${typeColor(ts.type)}`}>
                {typeLabel(ts.type)}
              </span>
            ))}
          </div>
        </td>
        <td className="py-2.5 px-3 text-slate-500 text-xs text-right">
          {open ? '▲' : '▼'}
        </td>
      </tr>

      {open && (
        <tr>
          <td colSpan={6} className="p-0 bg-slate-800/60">
            <table className="w-full">
              <thead>
                <tr className="text-xs text-slate-500">
                  <th className="pl-6 pr-2 py-1 text-left font-normal">{t('countries.type')}</th>
                  <th className="px-2 py-1 text-right font-normal">{t('countries.count')}</th>
                  <th className="px-2 py-1 text-right font-normal">{t('countries.distance')}</th>
                  <th className="px-2 py-1 text-right font-normal">{t('countries.time')}</th>
                  <th className="px-2 py-1 text-right font-normal">{t('countries.elevation')}</th>
                  <th className="pl-2 pr-3 py-1 text-right font-normal">{t('countries.avgHr')}</th>
                </tr>
              </thead>
              <tbody>
                {c.types.map(ts => <TypeRow key={ts.type} ts={ts} />)}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  )
}

interface Props {
  start: string
  end: string
}

export default function CountryStats({ start, end }: Props) {
  const { t } = useTranslation()
  const [data, setData] = useState<CountryStat[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    fetchCountryStats(start, end)
      .then(setData)
      .catch(() => setData([]))
      .finally(() => setLoading(false))
  }, [start, end])

  if (loading) return null
  if (data.length === 0) return null

  const totalKm = data.reduce((s, c) => s + c.total_km, 0)

  return (
    <div className="bg-slate-800 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-300">{t('countries.title')}</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            {t('countries.summary', { count: data.length, km: fmtKm(totalKm) })}
          </p>
        </div>
      </div>
      <table className="w-full">
        <thead>
          <tr className="text-xs text-slate-500 bg-slate-800/80">
            <th className="px-3 py-2 text-left font-normal">{t('countries.country')}</th>
            <th className="px-3 py-2 text-right font-normal">{t('countries.distance')}</th>
            <th className="px-3 py-2 text-right font-normal">{t('countries.time')}</th>
            <th className="px-3 py-2 text-right font-normal">{t('countries.elevation')}</th>
            <th className="px-3 py-2 text-right font-normal">{t('countries.types')}</th>
            <th className="px-3 py-2 text-right font-normal w-6" />
          </tr>
        </thead>
        <tbody>
          {data.map(c => <CountryRow key={c.country} c={c} />)}
        </tbody>
      </table>
    </div>
  )
}
