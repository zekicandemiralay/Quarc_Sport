import { useTranslation } from 'react-i18next'
import type { Activity } from '../types'
import { dateLocale } from '../i18n'

interface Props {
  data: Activity[]
  earliest: string
  latest: string
}

// Monday-first weekday labels in the UI language (2024-01-01 was a Monday)
const weekdayLabels = () => [0, 1, 2, 3, 4, 5, 6].map(i => new Date(2024, 0, 1 + i).toLocaleDateString(dateLocale(), { weekday: 'short' }))
const DAY_COUNT = 7

function getMonday(d: Date): Date {
  const day = d.getDay()
  const monday = new Date(d)
  monday.setDate(d.getDate() - ((day + 6) % 7))
  monday.setHours(0, 0, 0, 0)
  return monday
}

export default function ActivityHeatmap({ data, earliest, latest }: Props) {
  const { t } = useTranslation()
  // Build a map of date → total duration minutes
  const durationByDate: Record<string, number> = {}
  for (const a of data) {
    const d = a.start_time.slice(0, 10)
    durationByDate[d] = (durationByDate[d] ?? 0) + (a.duration_seconds ?? 0) / 60
  }

  // Build week columns from earliest to latest
  const start = getMonday(new Date(earliest))
  const end   = new Date(latest)

  const weeks: Date[] = []
  const cur = new Date(start)
  while (cur <= end) {
    weeks.push(new Date(cur))
    cur.setDate(cur.getDate() + 7)
  }

  const maxMin = Math.max(...Object.values(durationByDate), 60)

  function cellColor(date: Date): string {
    const key = date.toISOString().slice(0, 10)
    const min = durationByDate[key] ?? 0
    if (min === 0) return 'bg-slate-700/40'
    const intensity = Math.min(min / maxMin, 1)
    if (intensity < 0.25) return 'bg-blue-900'
    if (intensity < 0.5)  return 'bg-blue-700'
    if (intensity < 0.75) return 'bg-blue-500'
    return 'bg-blue-400'
  }

  // Show month labels above columns
  const monthLabels: { label: string; index: number }[] = []
  let lastMonth = -1
  weeks.forEach((monday, i) => {
    if (monday.getMonth() !== lastMonth) {
      monthLabels.push({ label: monday.toLocaleString(dateLocale(), { month: 'short' }), index: i })
      lastMonth = monday.getMonth()
    }
  })

  return (
    <div className="bg-slate-800 rounded-xl p-4">
      <h3 className="text-sm font-semibold text-slate-300 mb-4">{t('heatmap.title')}</h3>
      <div className="overflow-x-auto">
        <div className="inline-block">
          {/* Month labels */}
          <div className="flex mb-1 ml-8">
            {weeks.map((_, i) => {
              const lbl = monthLabels.find(m => m.index === i)
              return (
                <div key={i} className="w-[13px] mr-[2px] text-xs text-slate-500 whitespace-nowrap">
                  {lbl?.label ?? ''}
                </div>
              )
            })}
          </div>
          {/* Grid */}
          <div className="flex gap-0">
            {/* Day labels */}
            <div className="flex flex-col mr-1 gap-[2px]">
              {weekdayLabels().map((d, i) => (
                <div key={i} className={`h-[13px] text-[10px] text-slate-500 leading-[13px] w-7 ${i % 2 === 0 ? '' : 'opacity-0'}`}>
                  {d}
                </div>
              ))}
            </div>
            {/* Week columns */}
            {weeks.map((monday, wi) => (
              <div key={wi} className="flex flex-col gap-[2px] mr-[2px]">
                {Array.from({ length: DAY_COUNT }, (_, di) => {
                  const d = new Date(monday)
                  d.setDate(monday.getDate() + di)
                  const key = d.toISOString().slice(0, 10)
                  const min = durationByDate[key] ?? 0
                  return (
                    <div
                      key={di}
                      title={min > 0 ? t('heatmap.tooltip', { date: key, min: Math.round(min) }) : key}
                      className={`w-[13px] h-[13px] rounded-sm ${cellColor(d)}`}
                    />
                  )
                })}
              </div>
            ))}
          </div>
          {/* Legend */}
          <div className="flex items-center gap-2 mt-3 ml-8">
            <span className="text-xs text-slate-500">{t('heatmap.less')}</span>
            {['bg-slate-700/40', 'bg-blue-900', 'bg-blue-700', 'bg-blue-500', 'bg-blue-400'].map(c => (
              <div key={c} className={`w-[13px] h-[13px] rounded-sm ${c}`} />
            ))}
            <span className="text-xs text-slate-500">{t('heatmap.more')}</span>
          </div>
        </div>
      </div>
    </div>
  )
}
