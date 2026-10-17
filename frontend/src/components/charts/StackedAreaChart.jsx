import { linearScale, niceMax, computeYTicks, buildLinePath, stackSeries } from '../../utils/chartMath'

/**
 * Accessible cumulative-flow stacked-area chart rendered as inline SVG
 * (PLS-361), matching the PLS-274 chart idiom (no chart dependency, themed,
 * SR data table).
 *
 * Props:
 *  - xLabels: [string]                       (one per day)
 *  - series:  [{ key, label, color, points: [number] }]  (stack order: first = bottom)
 *  - height:  px (default 260)
 *  - ariaLabel: accessible summary
 */
export default function StackedAreaChart({ xLabels = [], series = [], height = 260, ariaLabel }) {
  const padding = { top: 16, right: 12, bottom: 40, left: 44 }
  const width = 640
  const plotW = width - padding.left - padding.right
  const plotH = height - padding.top - padding.bottom

  const hasData = series.some((s) => (s.points || []).some((v) => v > 0))
  if (!xLabels.length || !hasData) {
    return (
      <p className="text-sm text-gray-400 dark:text-gray-500 py-8 text-center">
        No status history in this window yet.
      </p>
    )
  }

  const stacked = stackSeries(series.map((s) => s.points || []))
  const totals = stacked.length ? stacked[stacked.length - 1] : []
  const yTop = niceMax(Math.max(1, ...totals))
  const xScale = linearScale(0, Math.max(1, xLabels.length - 1), 0, plotW)
  const yScale = linearScale(0, yTop, plotH, 0)
  const yTicks = computeYTicks(yTop, 4)

  const areaPath = (upper, lower) => {
    const up = upper.map((v, i) => ({ x: xScale(i), y: yScale(v) }))
    const down = lower.map((v, i) => ({ x: xScale(i), y: yScale(v) })).reverse()
    const upPath = buildLinePath(up)
    if (!upPath) return ''
    const downPath = down.map((p) => `L${Math.round(p.x * 100) / 100} ${Math.round(p.y * 100) / 100}`).join(' ')
    return `${upPath} ${downPath} Z`
  }

  const title = ariaLabel || `Cumulative flow of ${series.length} statuses over ${xLabels.length} days`
  // 3 x labels: first / middle / last day
  const xTickIdx = [0, Math.floor((xLabels.length - 1) / 2), xLabels.length - 1]

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto" role="img" aria-label={title}>
        <title>{title}</title>
        <g transform={`translate(${padding.left},${padding.top})`}>
          {yTicks.map((t) => (
            <g key={t}>
              <line x1={0} x2={plotW} y1={yScale(t)} y2={yScale(t)} className="stroke-gray-200 dark:stroke-gray-700" strokeWidth="1" />
              <text x={-8} y={yScale(t)} textAnchor="end" dominantBaseline="middle" className="fill-gray-400 dark:fill-gray-500" fontSize="10">
                {t}
              </text>
            </g>
          ))}
          {series.map((s, i) => (
            <path
              key={s.key}
              d={areaPath(stacked[i], i === 0 ? stacked[i].map(() => 0) : stacked[i - 1])}
              fill={s.color}
              fillOpacity="0.55"
              stroke={s.color}
              strokeWidth="1"
            >
              <title>{s.label}</title>
            </path>
          ))}
          {xTickIdx.map((i) => (
            <text
              key={i}
              x={xScale(i)}
              y={plotH + 24}
              textAnchor={i === 0 ? 'start' : i === xLabels.length - 1 ? 'end' : 'middle'}
              className="fill-gray-400 dark:fill-gray-500"
              fontSize="10"
            >
              {(xLabels[i] || '').slice(5)}
            </text>
          ))}
        </g>
      </svg>
      {/* Legend */}
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600 dark:text-gray-300">
        {series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
      {/* Screen-reader data table (same a11y contract as the other charts) */}
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th scope="col">Day</th>
            {series.map((s) => (
              <th key={s.key} scope="col">{s.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {xLabels.map((day, i) => (
            <tr key={day}>
              <th scope="row">{day}</th>
              {series.map((s) => (
                <td key={s.key}>{(s.points || [])[i] ?? 0}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
