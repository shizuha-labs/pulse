import { niceMax, computeYTicks, linearScale, buildLinePath } from '../../utils/chartMath'

/**
 * Accessible multi-line chart rendered as inline SVG (PLS-274). Used for sprint
 * burndown (ideal vs remaining) and other time-series. No chart dependency.
 *
 * Props:
 *  - xLabels: string[]  — one label per x index (ticks thinned to avoid overlap)
 *  - lines:   [{ key, label, color, dashed?, points: (number|null)[] }]
 *             points[i] is the y value at xLabels[i]; null renders a gap.
 *  - height:  px (default 240)
 *  - formatValue / ariaLabel
 */
export default function LineChart({
  xLabels = [],
  lines = [],
  height = 240,
  formatValue = (n) => `${n}`,
  ariaLabel,
}) {
  const padding = { top: 16, right: 16, bottom: 40, left: 44 }
  const width = 640
  const plotW = width - padding.left - padding.right
  const plotH = height - padding.top - padding.bottom

  const allY = lines.flatMap((l) => l.points).filter((v) => v != null && Number.isFinite(v))
  const maxVal = Math.max(1, ...allY)
  const top = niceMax(maxVal)
  const yScale = linearScale(0, top, plotH, 0)
  const ticks = computeYTicks(maxVal, 4)

  const n = Math.max(1, xLabels.length - 1)
  const xScale = linearScale(0, n, 0, plotW)

  const title =
    ariaLabel || `Line chart of ${lines.map((l) => l.label).join(', ')} over ${xLabels.length} points`

  if (!xLabels.length || !lines.length) {
    return (
      <p className="text-sm text-gray-400 dark:text-gray-500 py-8 text-center">
        No data for this selection yet.
      </p>
    )
  }

  // Thin x labels so at most ~8 render.
  const xStep = Math.ceil(xLabels.length / 8)

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

        {/* x labels */}
        {xLabels.map((lbl, i) =>
          i % xStep === 0 ? (
            <text
              key={i}
              x={padding.left + xScale(i)}
              y={height - padding.bottom + 14}
              textAnchor="middle"
              className="fill-gray-500 dark:fill-gray-400"
              fontSize="10"
            >
              {lbl}
            </text>
          ) : null,
        )}

        {/* lines + markers */}
        {lines.map((l) => {
          const pts = l.points.map((v, i) => ({
            x: padding.left + xScale(i),
            y: v == null || !Number.isFinite(v) ? null : padding.top + yScale(v),
          }))
          return (
            <g key={l.key}>
              <path
                d={buildLinePath(pts)}
                fill="none"
                stroke={l.color}
                strokeWidth="2"
                strokeDasharray={l.dashed ? '5 4' : undefined}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {pts.map((p, i) =>
                p.y == null ? null : (
                  <circle key={i} cx={p.x} cy={p.y} r="2.5" fill={l.color}>
                    <title>{`${l.label} · ${xLabels[i]}: ${formatValue(l.points[i])}`}</title>
                  </circle>
                ),
              )}
            </g>
          )
        })}
      </svg>

      <div className="flex flex-wrap gap-3 justify-center mt-2">
        {lines.map((l) => (
          <span key={l.key} className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
            <span
              className="inline-block w-4 h-0"
              style={{ borderTop: `2px ${l.dashed ? 'dashed' : 'solid'} ${l.color}` }}
            />
            {l.label}
          </span>
        ))}
      </div>

      {/* Screen-reader data table */}
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th>Point</th>
            {lines.map((l) => (
              <th key={l.key}>{l.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {xLabels.map((lbl, i) => (
            <tr key={i}>
              <td>{lbl}</td>
              {lines.map((l) => (
                <td key={l.key}>{l.points[i] == null ? '—' : formatValue(l.points[i])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
