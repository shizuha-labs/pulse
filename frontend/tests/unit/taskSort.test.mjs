import assert from 'node:assert/strict'
import { compareTasksBySort, DEFAULT_TASK_SORT } from '../../src/utils/taskSort.js'

const tasks = [
  { item_key: 'LOW-NEW', priority: 'low', created_at: '2026-06-26T10:00:00Z' },
  { item_key: 'HIGH-NEW', priority: 'high', created_at: '2026-06-26T11:00:00Z' },
  { item_key: 'URGENT', priority: 'urgent', created_at: '2026-06-26T12:00:00Z' },
  { item_key: 'NORMAL', priority: 'normal', created_at: '2026-06-26T09:00:00Z' },
  { item_key: 'HIGH-OLD', priority: 'high', created_at: '2026-06-26T08:00:00Z' },
]

assert.equal(DEFAULT_TASK_SORT, 'priority-desc')
assert.deepEqual(
  [...tasks].sort(compareTasksBySort(DEFAULT_TASK_SORT)).map(t => t.item_key),
  ['URGENT', 'HIGH-OLD', 'HIGH-NEW', 'NORMAL', 'LOW-NEW'],
)
assert.deepEqual(
  [...tasks].sort(compareTasksBySort('priority-asc')).map(t => t.item_key),
  ['LOW-NEW', 'NORMAL', 'HIGH-OLD', 'HIGH-NEW', 'URGENT'],
)
assert.deepEqual(
  [...tasks].sort(compareTasksBySort('created_at-desc')).map(t => t.item_key),
  ['URGENT', 'HIGH-NEW', 'LOW-NEW', 'NORMAL', 'HIGH-OLD'],
)

console.log('taskSort tests passed')
