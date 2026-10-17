/**
 * Pure, dependency-free math helpers for the inline-SVG analytics charts
 * (PLS-274). Kept separate from the React components so the scaling logic is
 * unit-testable with plain `node` (see tests/unit/chartMath.test.mjs).
 */

/**
 * Round `value` up to a visually "nice" axis maximum (1/2/5 x 10^n).
 * Guarantees a strictly-positive result so charts never divide by zero.
 */
export function niceMax(value) {
  const v = Number(value)
  if (!Number.isFinite(v) || v <= 0) return 1
  const exp = Math.floor(Math.log10(v))
  const pow = Math.pow(10, exp)
  const frac = v / pow
  let niceFrac
  if (frac <= 1) niceFrac = 1
  else if (frac <= 2) niceFrac = 2
  else if (frac <= 5) niceFrac = 5
  else niceFrac = 10
  return niceFrac * pow
}

/**
 * Evenly-spaced tick values from 0..max inclusive.
 * Returns `count + 1` values (default 5 → [0, .., max]).
 */
export function computeYTicks(max, count = 4) {
  const top = niceMax(max)
  const n = Math.max(1, Math.floor(count))
  const ticks = []
  for (let i = 0; i <= n; i++) {
    ticks.push(Math.round(((top * i) / n) * 100) / 100)
  }
  return ticks
}

/**
 * Build a linear scale mapping [domainMin, domainMax] → [rangeMin, rangeMax].
 * Degenerate domains (min === max) map everything to rangeMin (avoids NaN).
 */
export function linearScale(domainMin, domainMax, rangeMin, rangeMax) {
  const span = domainMax - domainMin
  return (x) => {
    if (span === 0) return rangeMin
    return rangeMin + ((x - domainMin) / span) * (rangeMax - rangeMin)
  }
}

/**
 * SVG path `d` for a polyline through `points` ([{x,y}, ...] already in px).
 * Skips null/undefined y values (gaps) so a partial series still renders.
 */
export function buildLinePath(points) {
  let d = ''
  let penDown = false
  for (const p of points) {
    if (p == null || p.y == null || !Number.isFinite(p.y)) {
      penDown = false
      continue
    }
    d += `${penDown ? 'L' : 'M'}${round(p.x)} ${round(p.y)} `
    penDown = true
  }
  return d.trim()
}

/** Format seconds as a compact human duration (for dwell/cycle-time labels). */
export function formatDuration(seconds) {
  const s = Number(seconds)
  if (!Number.isFinite(s) || s < 0) return '—'
  if (s < 60) return `${Math.round(s)}s`
  const m = s / 60
  if (m < 60) return `${round1(m)}m`
  const h = m / 60
  if (h < 24) return `${round1(h)}h`
  const days = h / 24
  return `${round1(days)}d`
}

function round(n) {
  return Math.round(n * 100) / 100
}

function round1(n) {
  return Math.round(n * 10) / 10
}

/**
 * Nearest-rank percentile of `values` (pct in 0..100). Mirrors the backend's
 * stage_metrics._percentile so frontend reference lines agree with the API
 * summary. Returns null for an empty list. (PLS-362)
 */
export function percentileNearestRank(values, pct) {
  const nums = (values || []).filter((v) => Number.isFinite(v))
  if (!nums.length) return null
  const s = [...nums].sort((a, b) => a - b)
  const k = Math.max(1, Math.ceil((pct / 100) * s.length))
  return s[k - 1]
}

/**
 * Bucket `values` into `bucketCount` equal-width histogram bins over
 * [0, niceMax(max)] so the axis stays readable. Returns
 * [{ min, max, count }]; empty input returns []. (PLS-362)
 */
export function histogramBuckets(values, bucketCount = 8) {
  const nums = (values || []).filter((v) => Number.isFinite(v) && v >= 0)
  if (!nums.length) return []
  const top = niceMax(Math.max(...nums))
  const n = Math.max(1, Math.floor(bucketCount))
  const width = top / n
  const buckets = Array.from({ length: n }, (_, i) => ({
    min: i * width,
    max: (i + 1) * width,
    count: 0,
  }))
  for (const v of nums) {
    const idx = Math.min(n - 1, Math.floor(v / width))
    buckets[idx].count += 1
  }
  return buckets
}

/**
 * Cumulative stack of N series (arrays of equal length): returns
 * stacked[i][j] = sum of series 0..i at index j. Missing/non-finite values
 * count as 0. Used by the CFD stacked areas (PLS-361).
 */
export function stackSeries(seriesPoints) {
  const out = []
  let prev = null
  for (const points of seriesPoints || []) {
    const row = (points || []).map((v, j) => {
      const n = Number.isFinite(v) ? v : 0
      return n + (prev ? prev[j] ?? 0 : 0)
    })
    out.push(row)
    prev = row
  }
  return out
}
