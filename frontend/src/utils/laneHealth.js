// PLS-1036 — pure helpers for the lane-health dashboard.
// Kept framework-free so tests/unit can exercise them with node:assert
// (repo unit-test convention). Contract pin: v9 §3.1/§4.2 as-served names.

// Anti-conflation (contract §4.2): status_role_ineligible is NOT a liveness
// verdict; capacity comes from seats[].capacity.over_capacity, never from
// reasons[]. Family mapping follows the §4.2 reason-family table.
const LIVENESS_FAMILY = new Set(["seat_not_live"]);
const STAFFING_FAMILY = new Set([
  "team_unstaffed",
  "seat_not_team_member",
  "team_unknown",
  "org_roster_mismatch",
]);
const CAPABILITY_FAMILY = new Set(["seat_incapable", "seat_incapable_substrate"]);

export function reasonFamily(reason) {
  if (LIVENESS_FAMILY.has(reason)) return "liveness";
  if (reason === "status_role_ineligible") return "status_role";
  if (reason === "capacity_limited") return "capacity";
  if (STAFFING_FAMILY.has(reason)) return "staffing";
  if (CAPABILITY_FAMILY.has(reason)) return "capability";
  return "other";
}

export function formatAge(iso, now = Date.now()) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const mins = Math.max(0, Math.round((now - then) / 60000));
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// Per-lane display summary: everything a card renders, derived once so the
// component stays declarative and the classification logic is testable.
export function summarizeLane(lane) {
  const seats = Array.isArray(lane?.seats) ? lane.seats : [];
  const stalled = lane?.stall?.is_stalled === true;
  const failureMode = lane?.stall?.failure_mode || "none";
  const liveSeats = seats.filter((s) => s?.live === true);
  const overCapacitySeats = seats.filter((s) => s?.capacity?.over_capacity === true);
  const nonRoutableFamilies = [
    ...new Set(
      seats
        .flatMap((s) => s?.reasons || [])
        .map(reasonFamily)
        .filter((f) => f !== "other")
    ),
  ];
  return {
    team: lane?.team,
    organization: lane?.organization,
    stalled,
    failureMode: stalled ? failureMode : "none",
    rosterVersion: lane?.roster?.version ?? null,
    seatCount: seats.length,
    liveSeatCount: liveSeats.length,
    routableSeats: lane?.resolver?.routable_seats ?? 0,
    totalActive: lane?.queue_depth?.total_active ?? 0,
    queueByStatus: lane?.queue_depth?.by_status || {},
    lastAssignmentAt: lane?.last_assignment?.at || null,
    mergeSodCount: lane?.merge_sod?.count ?? 0,
    overCapacitySeatCount: overCapacitySeats.length,
    nonRoutableFamilies,
  };
}
