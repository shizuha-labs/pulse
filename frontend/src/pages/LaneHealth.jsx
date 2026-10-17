import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  CircleSlash,
  Gauge,
  HeartPulse,
  Inbox,
  RefreshCw,
  Users,
} from "lucide-react";
import { laneHealthApi } from "../services/api";
import { formatAge, reasonFamily, summarizeLane } from "../utils/laneHealth";

// PLS-1036 — Lane-health dashboard (PLS-1033 epic S2).
// Builds against contract v9 §3.1 as-served field names ONLY; the planned
// v2/v3/v4 increments (§3.2) are intentionally not rendered. Pure display
// logic (reason families, age buckets, lane summary) lives in
// src/utils/laneHealth.js and is unit-tested in tests/unit/laneHealth.test.mjs.

const FAILURE_MODE_META = {
  authority_drift: {
    label: "authority drift",
    hint: "Routing authority moved (roster version advanced) — seats may be fine.",
    classes: "bg-purple-100 text-purple-800 border-purple-300",
  },
  seat_not_live: {
    label: "seat not live",
    hint: "Zero routable seats; every non-routable verdict is liveness-family.",
    classes: "bg-red-100 text-red-800 border-red-300",
  },
  over_capacity: {
    label: "over capacity",
    hint: "Zero routable seats; every non-routable verdict is capacity-family (WIP accounting).",
    classes: "bg-amber-100 text-amber-800 border-amber-300",
  },
  mixed: {
    label: "mixed",
    hint: "Multiple reason families contribute to zero-routable.",
    classes: "bg-orange-100 text-orange-800 border-orange-300",
  },
  none: { label: "healthy", hint: "", classes: "bg-green-100 text-green-800 border-green-300" },
};

const FAMILY_STYLES = {
  liveness: "bg-red-50 text-red-700 border-red-200",
  status_role: "bg-slate-100 text-slate-600 border-slate-300",
  capacity: "bg-amber-50 text-amber-700 border-amber-200",
  staffing: "bg-blue-50 text-blue-700 border-blue-200",
  capability: "bg-violet-50 text-violet-700 border-violet-200",
  other: "bg-gray-100 text-gray-600 border-gray-200",
};

function SeatRow({ seat }) {
  const live = seat.live === true;
  const overCap = seat?.capacity?.over_capacity === true;
  return (
    <div className="flex items-start justify-between gap-3 border-t border-gray-100 py-2 first:border-t-0">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span
            className={`inline-block h-2 w-2 shrink-0 rounded-full ${live ? "bg-green-500" : "bg-red-500"}`}
            title={live ? "live" : "not live"}
          />
          <span className="truncate text-sm font-medium text-gray-800">{seat.email}</span>
          {overCap && (
            <span className="shrink-0 rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700">
              over capacity
            </span>
          )}
        </div>
        {(seat.reasons || []).length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1 pl-4">
            {(seat.reasons || []).map((r) => (
              <span
                key={r}
                className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${FAMILY_STYLES[reasonFamily(r)] || FAMILY_STYLES.other}`}
                title={`reason family: ${reasonFamily(r)}`}
              >
                {r}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="shrink-0 text-right text-[11px] leading-4 text-gray-500">
        <div>
          active {seat?.capacity?.active_load ?? 0}/{seat?.capacity?.active_wip_cap ?? "—"}
        </div>
        <div>
          ready {seat?.capacity?.ready_load ?? 0}/{seat?.capacity?.ready_wip_cap ?? "—"}
        </div>
      </div>
    </div>
  );
}

function LaneCard({ lane }) {
  const [open, setOpen] = useState(false);
  const stalled = lane?.stall?.is_stalled === true;
  const mode = lane?.stall?.failure_mode || "none";
  const meta = FAILURE_MODE_META[mode] || FAILURE_MODE_META.none;
  const roster = lane?.roster || {};
  const resolver = lane?.resolver || {};
  const queue = lane?.queue_depth || {};
  const lastAssignment = lane?.last_assignment || {};
  const seats = lane?.seats || [];
  const mergeSod = lane?.merge_sod;

  return (
    <div
      className={`rounded-lg border bg-white shadow-sm transition-shadow hover:shadow-md ${
        stalled ? "border-red-300 ring-1 ring-red-200" : "border-gray-200"
      }`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
        aria-expanded={open}
      >
        <div className="flex min-w-0 items-center gap-3">
          {open ? (
            <ChevronDown className="h-4 w-4 shrink-0 text-gray-400" />
          ) : (
            <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" />
          )}
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="truncate text-sm font-semibold text-gray-900">{lane.team}</span>
              <span className="text-[11px] text-gray-400">roster v{roster.version ?? "—"}</span>
              {stalled && (
                <span
                  className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${meta.classes}`}
                  title={meta.hint}
                >
                  <AlertTriangle className="h-3 w-3" />
                  stalled: {meta.label}
                </span>
              )}
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-gray-500">
              <span className="inline-flex items-center gap-1">
                <Users className="h-3 w-3" />
                {resolver.routable_seats ?? 0}/{resolver.seat_count ?? seats.length} routable
              </span>
              <span className="inline-flex items-center gap-1">
                <Inbox className="h-3 w-3" />
                {queue.total_active ?? 0} active
              </span>
              <span className="inline-flex items-center gap-1">
                <Activity className="h-3 w-3" />
                last assign {formatAge(lastAssignment.at)}
              </span>
            </div>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div
            className={`text-lg font-bold leading-5 ${stalled ? "text-red-600" : "text-gray-800"}`}
          >
            {queue.total_active ?? 0}
          </div>
          <div className="text-[10px] uppercase tracking-wide text-gray-400">queued</div>
        </div>
      </button>

      {stalled && (
        <div className="border-t border-red-200 bg-red-50 px-4 py-2">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-red-700">
            {meta.label}
          </div>
          {lane?.stall?.evidence && (
            <p className="mt-0.5 text-xs text-red-800">{lane.stall.evidence}</p>
          )}
        </div>
      )}

      {mergeSod && (mergeSod.count ?? 0) > 0 && (
        <div className="border-t border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">
          <span className="font-semibold">{mergeSod.count}</span> merge SoD hold
          {mergeSod.count === 1 ? "" : "s"} (no distinct available merger) — surfaced separately
          from liveness/capacity.
        </div>
      )}

      {open && (
        <div className="border-t border-gray-100 px-4 py-3">
          <div className="mb-2 grid grid-cols-2 gap-3 text-[11px] text-gray-500 sm:grid-cols-4">
            <div>
              <div className="font-semibold text-gray-700">Roster</div>
              v{roster.version ?? "—"} · {roster.member_count ?? seats.length} seats
              {roster.active === false && " · inactive"}
              {roster.wip_enforced ? ` · caps ${roster.ready_wip_cap ?? "—"}/${roster.active_wip_cap ?? "—"}` : " · caps not enforced"}
            </div>
            <div>
              <div className="font-semibold text-gray-700">Resolver</div>
              {resolver.staffed ? "staffed" : "unstaffed"} · {resolver.routable_seats ?? 0} routable ·{" "}
              {resolver.unavailable_seats ?? 0} unavailable
            </div>
            <div>
              <div className="font-semibold text-gray-700">Queue by status</div>
              {Object.entries(queue.by_status || {})
                .map(([k, v]) => `${k}: ${v}`)
                .join(" · ") || "—"}
              <div className="text-gray-400">total all-time: {queue.total_all ?? "—"}</div>
            </div>
            <div>
              <div className="font-semibold text-gray-700">Last assignment</div>
              {lastAssignment.at ? (
                <>
                  {formatAge(lastAssignment.at)}
                  <div className="truncate text-gray-400">
                    {lastAssignment.assignee} · {lastAssignment.item_key}
                  </div>
                </>
              ) : (
                "—"
              )}
            </div>
          </div>
          <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
            Seats ({seats.length})
          </div>
          <div>
            {seats.length === 0 ? (
              <div className="flex items-center gap-2 py-2 text-xs text-gray-400">
                <CircleSlash className="h-3.5 w-3.5" /> No seats on this roster.
              </div>
            ) : (
              seats.map((s) => <SeatRow key={s.email} seat={s} />)
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function LaneHealth() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["laneHealth"],
    queryFn: () => laneHealthApi.list().then((r) => r.data),
    refetchInterval: 60_000,
  });

  const lanes = Array.isArray(data?.lanes) ? data.lanes : [];
  const stalledLanes = lanes.filter((l) => l?.stall?.is_stalled === true);

  return (
    <div className="mx-auto max-w-6xl px-4 py-6">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <HeartPulse className="h-5 w-5 text-red-500" />
          <h1 className="text-xl font-bold text-gray-900">Lane Health</h1>
          {stalledLanes.length > 0 && (
            <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-semibold text-red-700">
              {stalledLanes.length} stalled
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          className="inline-flex items-center gap-1.5 rounded border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          disabled={isFetching}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {isLoading && (
        <div className="flex items-center gap-2 rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-500">
          <Gauge className="h-4 w-4 animate-pulse" /> Loading lane health…
        </div>
      )}

      {isError && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-6">
          <div className="flex items-center gap-2 text-sm font-semibold text-red-700">
            <AlertTriangle className="h-4 w-4" /> Failed to load lane health
          </div>
          <p className="mt-1 text-xs text-red-600">
            {error?.response?.status
              ? `API returned ${error.response.status}.`
              : "Network error reaching the lane-health API."}{" "}
            The surface is read-only and safe to retry.
          </p>
          <button
            type="button"
            onClick={() => refetch()}
            className="mt-3 inline-flex items-center gap-1.5 rounded border border-red-300 bg-white px-2.5 py-1.5 text-xs font-medium text-red-700 hover:bg-red-100"
          >
            <RefreshCw className="h-3.5 w-3.5" /> Retry
          </button>
        </div>
      )}

      {!isLoading && !isError && lanes.length === 0 && (
        <div className="rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-500">
          No lanes reported. The lane-health resolver returned an empty set — check the Pulse
          backend roster projection.
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {lanes.map((lane) => (
          <LaneCard key={`${lane.organization ?? "org"}:${lane.team}`} lane={lane} />
        ))}
      </div>
    </div>
  );
}
