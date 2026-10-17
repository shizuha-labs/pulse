import assert from 'node:assert/strict'
import { formatAge, reasonFamily, summarizeLane } from '../../src/utils/laneHealth.js'

// --- reasonFamily: contract §4.2 family table + anti-conflation ---

assert.equal(reasonFamily('seat_not_live'), 'liveness')
assert.equal(reasonFamily('status_role_ineligible'), 'status_role') // NOT liveness (anti-conflation #1)
assert.equal(reasonFamily('capacity_limited'), 'capacity')
assert.equal(reasonFamily('team_unstaffed'), 'staffing')
assert.equal(reasonFamily('seat_not_team_member'), 'staffing')
assert.equal(reasonFamily('org_roster_mismatch'), 'staffing')
assert.equal(reasonFamily('seat_incapable'), 'capability')
assert.equal(reasonFamily('seat_incapable_substrate'), 'capability')
assert.equal(reasonFamily('provider_down'), 'other') // producer-map string, family-agnostic here

// --- formatAge: buckets + null/invalid safety ---

const NOW = new Date('2026-09-02T12:00:00Z').getTime()
assert.equal(formatAge(null), '—')
assert.equal(formatAge('not-a-date'), '—')
assert.equal(formatAge('2026-09-02T11:45:00Z', NOW), '15m ago')
assert.equal(formatAge('2026-09-02T09:00:00Z', NOW), '3h ago')
assert.equal(formatAge('2026-08-30T12:00:00Z', NOW), '3d ago')

// --- summarizeLane: stalled lane with failure mode + evidence fields ---

const stalledLane = {
  team: 'merge',
  organization: 'shizuha',
  roster: { version: 3, member_count: 4 },
  seats: [
    { email: 'a@x', live: false, reasons: ['seat_not_live'], capacity: { over_capacity: false, active_load: 0, active_wip_cap: 10 } },
    { email: 'b@x', live: false, reasons: ['status_role_ineligible'], capacity: { over_capacity: false } },
    { email: 'c@x', live: true, reasons: [], capacity: { over_capacity: true, active_load: 12, active_wip_cap: 10 } },
  ],
  resolver: { staffed: true, routable_seats: 1, seat_count: 3, unavailable_seats: 2 },
  queue_depth: { by_status: { awaiting_merge: 71, todo: 9 }, total_active: 80, total_all: 2542 },
  last_assignment: { at: '2026-09-02T10:42:15Z', assignee: 'system@pulse', item_key: 'PLAT-6503' },
  stall: { is_stalled: true, failure_mode: 'seat_not_live', evidence: 'zero routable seats', routable_seats: 0, demand: 80 },
  merge_sod: { count: 2, holds: [] },
}
const s1 = summarizeLane(stalledLane)
assert.equal(s1.stalled, true)
assert.equal(s1.failureMode, 'seat_not_live')
assert.equal(s1.rosterVersion, 3)
assert.equal(s1.seatCount, 3)
assert.equal(s1.liveSeatCount, 1)
assert.equal(s1.routableSeats, 1)
assert.equal(s1.totalActive, 80)
assert.equal(s1.mergeSodCount, 2)
assert.equal(s1.overCapacitySeatCount, 1) // capacity verdict separate from liveness (anti-conflation #4)
assert.deepEqual(s1.queueByStatus, { awaiting_merge: 71, todo: 9 })
assert.ok(s1.nonRoutableFamilies.includes('liveness'))
assert.ok(s1.nonRoutableFamilies.includes('status_role'))

// --- summarizeLane: healthy lane + empty/missing-field tolerance ---

const healthyLane = {
  team: 'review',
  roster: { version: 1 },
  seats: [{ email: 'r@x', live: true, reasons: [], capacity: { over_capacity: false } }],
  resolver: { staffed: true, routable_seats: 1, seat_count: 1, unavailable_seats: 0 },
  queue_depth: { by_status: {}, total_active: 0 },
  stall: { is_stalled: false, failure_mode: 'none' },
}
const s2 = summarizeLane(healthyLane)
assert.equal(s2.stalled, false)
assert.equal(s2.failureMode, 'none') // not stalled → mode forced to none even if served oddly
assert.equal(s2.mergeSodCount, 0) // merge_sod absent on non-merge lanes
assert.equal(s2.lastAssignmentAt, null)

// empty lane (no seats, no queue)
const s3 = summarizeLane({ team: 'qa', seats: [], queue_depth: { total_active: 0 }, stall: { is_stalled: false } })
assert.equal(s3.seatCount, 0)
assert.equal(s3.liveSeatCount, 0)
assert.equal(s3.totalActive, 0)
assert.deepEqual(s3.nonRoutableFamilies, [])

// totally malformed payload must not throw
const s4 = summarizeLane(null)
assert.equal(s4.stalled, false)
assert.equal(s4.failureMode, 'none')
assert.equal(s4.seatCount, 0)

console.log('laneHealth.test.mjs: all assertions passed')
