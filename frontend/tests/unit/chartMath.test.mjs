import assert from 'node:assert/strict'
import {
  niceMax,
  computeYTicks,
  linearScale,
  buildLinePath,
  formatDuration,
  percentileNearestRank,
  histogramBuckets,
  stackSeries,
} from '../../src/utils/chartMath.js'

// niceMax rounds up to 1/2/5 x 10^n and is always > 0
assert.equal(niceMax(0), 1)
assert.equal(niceMax(-5), 1)
assert.equal(niceMax(NaN), 1)
assert.equal(niceMax(1), 1)
assert.equal(niceMax(3), 5)
assert.equal(niceMax(7), 10)
assert.equal(niceMax(42), 50)
assert.equal(niceMax(120), 200)

// computeYTicks: count+1 evenly spaced values from 0..niceMax
assert.deepEqual(computeYTicks(42, 4), [0, 12.5, 25, 37.5, 50])
assert.deepEqual(computeYTicks(0, 4), [0, 0.25, 0.5, 0.75, 1])
assert.equal(computeYTicks(100, 5).length, 6)

// linearScale maps domain→range, degenerate domain → rangeMin (no NaN)
const s = linearScale(0, 10, 0, 100)
assert.equal(s(0), 0)
assert.equal(s(5), 50)
assert.equal(s(10), 100)
const flat = linearScale(5, 5, 20, 80)
assert.equal(flat(5), 20)
assert.ok(Number.isFinite(flat(5)))

// buildLinePath: M then L, gaps (null y) break the pen
assert.equal(
  buildLinePath([{ x: 0, y: 10 }, { x: 5, y: 20 }, { x: 10, y: 0 }]),
  'M0 10 L5 20 L10 0',
)
assert.equal(
  buildLinePath([{ x: 0, y: 10 }, { x: 5, y: null }, { x: 10, y: 0 }]),
  'M0 10 M10 0',
)
assert.equal(buildLinePath([]), '')

// formatDuration: compact human units
assert.equal(formatDuration(45), '45s')
assert.equal(formatDuration(90), '1.5m')
assert.equal(formatDuration(3600), '1h')
assert.equal(formatDuration(86400 * 2), '2d')
assert.equal(formatDuration(-1), '—')

console.log('chartMath tests passed')

// ---- PLS-362: percentileNearestRank mirrors backend nearest-rank ----
assert.equal(percentileNearestRank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), 9)
assert.equal(percentileNearestRank([5], 90), 5)
assert.equal(percentileNearestRank([], 90), null)
assert.equal(percentileNearestRank([3, 1, 2], 50), 2) // unsorted input is sorted first
assert.equal(percentileNearestRank([1, NaN, 3], 50), 1) // non-finite values dropped

// ---- PLS-362: histogramBuckets ----
assert.deepEqual(histogramBuckets([], 8), [])
{
  const buckets = histogramBuckets([1, 2, 3, 10], 4) // niceMax(10)=10, width 2.5
  assert.equal(buckets.length, 4)
  assert.equal(buckets[0].count, 2) // 1, 2
  assert.equal(buckets[1].count, 1) // 3
  assert.equal(buckets[3].count, 1) // 10 clamps into the last bucket
  assert.equal(buckets.reduce((n, b) => n + b.count, 0), 4)
}
{
  // negative / non-finite values are excluded, all-equal values still bucket
  const buckets = histogramBuckets([5, 5, 5, -1, Infinity], 5)
  assert.equal(buckets.reduce((n, b) => n + b.count, 0), 3)
}

console.log('PLS-362 chartMath additions passed')

// ---- PLS-361: stackSeries cumulative stacking ----
{
  const stacked = stackSeries([[1, 2], [3, 4], [0, 1]])
  assert.deepEqual(stacked, [[1, 2], [4, 6], [4, 7]])
}
assert.deepEqual(stackSeries([]), [])
{
  // non-finite values count as zero, lengths follow each series
  const stacked = stackSeries([[1, NaN], [1, 1]])
  assert.deepEqual(stacked, [[1, 0], [2, 1]])
}
console.log('PLS-361 chartMath additions passed')
