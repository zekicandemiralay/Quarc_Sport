import { useTranslation } from 'react-i18next'
import {
  ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts'
import type { HrvRow } from '../types'
import { rollingMean, rollingStd } from '../stats'

interface Props { data: HrvRow[] }

const fmt = (d: string) => d.slice(5)

const STATUS_COLOR: Record<string, string> = {
  BALANCED:   '#22c55e',
  UNBALANCED: '#f59e0b',
  POOR:       '#ef4444',
}

export default function HrvChart({ data }: Props) {
  const { t } = useTranslation()
  const hasData = data.some(r => r.hrv_last_night != null)

  const vals  = data.map(r => r.hrv_last_night)
  const mean  = rollingMean(vals, 7)
  const std   = rollingStd(vals, 7, mean)

  const chartData = data.map((r, i) => {
    const m = mean[i]; const s = std[i]
    return {
      date:      fmt(r.date),
      hrv:       r.hrv_last_night,
      weekly:    r.hrv_weekly_avg,
      bandMin:   m != null && s != null ? +(m - s).toFixed(1) : null,
      bandWidth: m != null && s != null ? +(2 * s).toFixed(1) : null,
      mean7:     m != null ? +m.toFixed(1) : null,
      statusColor: STATUS_COLOR[r.hrv_status ?? ''] ?? '#475569',
    }
  })

  if (!hasData) {
    return (
      <div className="bg-slate-800 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-slate-300 mb-1">{t('charts.hrv.title')}</h3>
        <p className="text-xs text-slate-500 mt-2">{t('charts.hrv.empty')}</p>
      </div>
    )
  }

  return (
    <div className="bg-slate-800 rounded-xl p-4">
      <h3 className="text-sm font-semibold text-slate-300 mb-1">{t('charts.hrv.titleFull')}</h3>
      <p className="text-xs text-slate-500 mb-4">{t('charts.hrv.hint')}</p>
      <ResponsiveContainer width="100%" height={230}>
        <ComposedChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="hrvBand" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor="#22c55e" stopOpacity={0.25} />
              <stop offset="95%" stopColor="#22c55e" stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#94a3b8' }} />
          <YAxis domain={['auto', 'auto']} tick={{ fontSize: 11, fill: '#94a3b8' }} unit=" ms" />
          <Tooltip
            contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: 8 }}
            labelStyle={{ color: '#cbd5e1' }}
            formatter={(v: number, name: string) => {
              if (name === 'bandMin' || name === 'bandWidth') return [null, null]
              return [`${v} ms`, name]
            }}
          />
          <Legend
            wrapperStyle={{ fontSize: 12, color: '#94a3b8' }}
            formatter={(v) => (v === 'bandMin' || v === 'bandWidth') ? null : v}
          />
          <Area type="monotone" dataKey="bandMin"   stackId="b" stroke="none" fill="transparent"   legendType="none" />
          <Area type="monotone" dataKey="bandWidth" stackId="b" stroke="none" fill="url(#hrvBand)" name={t('charts.hrv.band')} legendType="square" />
          <Line type="monotone" dataKey="hrv"    name={t('charts.hrv.lastNight')}stroke="#22c55e" strokeWidth={1}   dot={false} strokeOpacity={0.5} connectNulls />
          <Line type="monotone" dataKey="weekly" name={t('charts.hrv.weekly')}stroke="#86efac" strokeWidth={1.5} dot={false} strokeDasharray="4 2" connectNulls />
          <Line type="monotone" dataKey="mean7"  name={t('charts.hrv.mean7')} stroke="#4ade80" strokeWidth={2.5} dot={false} connectNulls />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
