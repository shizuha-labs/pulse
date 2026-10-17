import { linearScale, niceMax, computeYTicks, formatDuration } from '../../utils/chartMath'
import { SERIES_COLORS } from './chartColors'

/**
 * Accessible cycle-time scatterplot rendered as inline SVG (PLS-362), matching
 * the PLS-274 chart idiom (no chart dependency, themed, SR data table).
 *
 * Props:
 *  - points: [{ x: epochMs, y: seconds, label }]  (x = completion time)
 *  - refLines: [{ value: seconds, label, color }] (e.g. p50/p85)
 *  - height: px (default 260)
 *  - ariaLabel: accessible summary
 */
export default function ScatterChart({ points = [], refLines = [], height = 260, ariaLabel }) {
  const padding = { top: 16, right: 12, bottom: 40, left: 56 }
  const width = 640
  const plotW = width - padding.left - padding.right
  const plotH = height - padding.top - padding.bottom

  if (!points.length) {
    return (
      <p className="text-sm text-gray-400 dark:text-gray-500 py-8 text-center">
        No completed items in this window yet.
      </p>
    )
  }

  const xs = points.map((p) => p.x)
  const xMin = Math.min(...xs)
  const xMax = Math.max(...xs)
  const yTop = niceMax(Math.max(...points.map((p) => p.y), ...refLines.map((r) => r.value || 0)))
  const xScale = linearScale(xMin, xMax === xMin ? xMin + 1 : xMax, 0, plotW)
  const yScale = linearScale(0, yTop, plotH, 0)
  const yTicks = computeYTicks(yTop, 4)

  const fmtDate = (ms) => {
    const d = new Date(ms)
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  // 3 x labels: first / middle / last completion date
  const xLabels = [xMin, (xMin + xMax) / 2, xMax]

  const title = ariaLabel || `Cycle-time scatterplot of ${points.length} completed items`

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto" role="img" aria-label={title}>
        <title>{title}</title>
        <g transform={`translate(${padding.left},${padding.top})`}>
          {yTicks.map((t) => (
            <g key={t}>
              <line
                x1={0}
                x2={plotW}
                y1={yScale(t)}
                y2={yScale(t)}
                className="stroke-gray-200 dark:stroke-gray-700"
                strokeWidth="1"
              />
              <text
                x={-8}
                y={yScale(t)}
                textAnchor="end"
                dominantBaseline="middle"
                className="fill-gray-400 dark:fill-gray-500"
                fontSize="10"
              >
                {formatDuration(t)}
              </text>
            </g>
          ))}
          {refLines
            .filter((r) => Number.isFinite(r.value))
            .map((r) => (
              <g key={r.label}>
                <line
                  x1={0}
                  x2={plotW}
                  y1={yScale(r.value)}
                  y2={yScale(r.value)}
                  stroke={r.color}
                  strokeWidth="1.5"
                  strokeDasharray="6 4"
                />
                <text
                  x={plotW - 4}
                  y={yScale(r.value) - 4}
                  textAnchor="end"
                  fontSize="10"
                  fill={r.color}
                >
                  {r.label} {formatDuration(r.value)}
                </text>
              </g>
            ))}
          {points.map((p, i) => (
            <circle
              key={`${p.label}-${i}`}
              cx={xScale(p.x)}
              cy={yScale(Math.min(p.y, yTop))}
              r="4"
              fill={SERIES_COLORS.completed}
              fillOpacity="0.65"
            >
              <title>{`${p.label}: ${formatDuration(p.y)} (completed ${fmtDate(p.x)})`}</title>
            </circle>
          ))}
          {xLabels.map((ms, i) => (
            <text
              key={i}
              x={xScale(ms)}
              y={plotH + 24}
              textAnchor={i === 0 ? 'start' : i === xLabels.length - 1 ? 'end' : 'middle'}
              className="fill-gray-400 dark:fill-gray-500"
              fontSize="10"
            >
              {fmtDate(ms)}
            </text>
          ))}
        </g>
      </svg>
      {/* Screen-reader data table (same a11y contract as BarChart/LineChart) */}
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th scope="col">Item</th>
            <th scope="col">Completed</th>
            <th scope="col">Cycle time</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p, i) => (
            <tr key={`${p.label}-${i}`}>
              <th scope="row">{p.label}</th>
              <td>{fmtDate(p.x)}</td>
              <td>{formatDuration(p.y)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
