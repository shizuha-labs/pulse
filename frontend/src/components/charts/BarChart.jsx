import { niceMax, computeYTicks, linearScale } from '../../utils/chartMath'

/**
 * Accessible grouped-bar chart rendered as inline SVG (PLS-274). No chart
 * dependency — keeps the bundle lean and gives us full theming control.
 *
 * Props:
 *  - groups:  [{ label, values: { [seriesKey]: number } }]
 *  - series:  [{ key, label, color }]
 *  - height:  px (default 240)
 *  - formatValue: (n) => string   (tooltip / axis formatter)
 *  - unit:    optional axis unit label
 *
 * A11y: role="img" + <title>/<desc> summary, plus a visually-hidden data table
 * so screen-reader users get the underlying numbers, not just "image".
 */
export default function BarChart({
  groups = [],
  series = [],
  height = 240,
  formatValue = (n) => `${n}`,
  unit,
  ariaLabel,
}) {
  const padding = { top: 16, right: 12, bottom: 40, left: 44 }
  const width = 640 // viewBox width; SVG scales responsively to container
  const plotW = width - padding.left - padding.right
  const plotH = height - padding.top - padding.bottom

  const maxVal = Math.max(
    1,
    ...groups.flatMap((g) => series.map((s) => Number(g.values?.[s.key]) || 0)),
  )
  const top = niceMax(maxVal)
  const yScale = linearScale(0, top, plotH, 0) // invert: 0 at bottom
  const ticks = computeYTicks(maxVal, 4)

  const groupW = plotW / Math.max(1, groups.length)
  const innerPad = groupW * 0.18
  const barsW = groupW - innerPad * 2
  const barW = barsW / Math.max(1, series.length)

  const title =
    ariaLabel ||
    `Bar chart of ${series.map((s) => s.label).join(', ')} across ${groups.length} groups`

  if (!groups.length) {
    return (
      <p className="text-sm text-gray-400 dark:text-gray-500 py-8 text-center">
        No data for this selection yet.
      </p>
    )
  }

  return (
    <div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full h-auto"
        role="img"
        aria-label={title}
        preserveAspectRatio="xMidYMid meet"
      >
        <title>{title}</title>
        {/* Y grid + axis labels */}
        {ticks.map((t) => {
          const y = padding.top + yScale(t)
          return (
            <g key={t}>
              <line
                x1={padding.left}
                x2={width - padding.right}
                y1={y}
                y2={y}
                className="stroke-gray-200 dark:stroke-gray-700"
                strokeWidth="1"
              />
              <text
                x={padding.left - 6}
                y={y + 3}
                textAnchor="end"
                className="fill-gray-400 dark:fill-gray-500"
                fontSize="10"
              >
                {formatValue(t)}
              </text>
            </g>
          )
        })}

        {/* Bars */}
        {groups.map((g, gi) => {
          const gx = padding.left + gi * groupW + innerPad
          return (
            <g key={g.label}>
              {series.map((s, si) => {
                const raw = Number(g.values?.[s.key]) || 0
                const barH = plotH - yScale(raw)
                const x = gx + si * barW
                const y = padding.top + yScale(raw)
                return (
                  <rect
                    key={s.key}
                    x={x}
                    y={y}
                    width={Math.max(1, barW - 2)}
                    height={Math.max(0, barH)}
                    rx="2"
                    fill={s.color}
                  >
                    <title>{`${g.label} · ${s.label}: ${formatValue(raw)}${unit ? ' ' + unit : ''}`}</title>
                  </rect>
                )
              })}
              <text
                x={gx + barsW / 2}
                y={height - padding.bottom + 14}
                textAnchor="middle"
                className="fill-gray-500 dark:fill-gray-400"
                fontSize="10"
              >
                {truncate(g.label, 14)}
              </text>
            </g>
          )
        })}
      </svg>

      {/* Legend */}
      {series.length > 1 && (
        <div className="flex flex-wrap gap-3 justify-center mt-2">
          {series.map((s) => (
            <span key={s.key} className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
              <span className="inline-block w-3 h-3 rounded-sm" style={{ backgroundColor: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
      )}

      {/* Screen-reader data table */}
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th>Group</th>
            {series.map((s) => (
              <th key={s.key}>{s.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <tr key={g.label}>
              <td>{g.label}</td>
              {series.map((s) => (
                <td key={s.key}>{formatValue(Number(g.values?.[s.key]) || 0)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function truncate(str, n) {
  const s = String(str ?? '')
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}
