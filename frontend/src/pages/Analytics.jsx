import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  TrendingUp,
  Activity,
  Gauge,
  CheckCircle2,
  ListTodo,
  AlertTriangle,
  Layers,
  SlidersHorizontal,
  Timer,
} from "lucide-react";
import { projectsApi, sprintsApi, itemsApi } from "../services/api";
import StatCard from "../components/StatCard";
import BarChart from "../components/charts/BarChart";
import LineChart from "../components/charts/LineChart";
import ScatterChart from "../components/charts/ScatterChart";
import StackedAreaChart from "../components/charts/StackedAreaChart";
import { SERIES_COLORS, seriesColor } from "../components/charts/chartColors";
import {
  formatDuration,
  histogramBuckets,
  percentileNearestRank,
} from "../utils/chartMath";
import {
  ANALYTICS_WIDGETS,
  DEFAULT_ANALYTICS_WIDGET_IDS,
  loadWidgetPreference,
  saveWidgetPreference,
  toggleWidgetPreference,
} from "../utils/analyticsWidgets";
import { PageLoader } from "../components/LoadingSpinner";

const PRIORITY_ORDER = ["urgent", "high", "normal", "low"];
const WINDOW_OPTIONS = [7, 14, 30];

function unwrap(res) {
  const d = res?.data;
  return d?.results || d || [];
}

function humanize(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function Analytics() {
  const [projectId, setProjectId] = useState(null);
  const [sprintId, setSprintId] = useState(null);
  const [windowDays, setWindowDays] = useState(14);
  const [showWidgetPicker, setShowWidgetPicker] = useState(false);
  const [visibleWidgetIds, setVisibleWidgetIds] = useState(() =>
    loadWidgetPreference(),
  );

  useEffect(() => {
    saveWidgetPreference(visibleWidgetIds);
  }, [visibleWidgetIds]);

  const visibleWidgets = useMemo(
    () => new Set(visibleWidgetIds),
    [visibleWidgetIds],
  );

  // Projects for the selector
  const { data: projects = [], isLoading: projectsLoading } = useQuery({
    queryKey: ["analytics", "projects"],
    queryFn: async () => unwrap(await projectsApi.list()),
  });

  const activeProjectId = projectId ?? projects[0]?.id ?? null;
  const activeProject = projects.find((p) => p.id === activeProjectId);

  // Org-wide statistics (headline cards + priority mix)
  const { data: stats } = useQuery({
    queryKey: ["analytics", "statistics"],
    queryFn: async () => (await itemsApi.getStatistics()).data,
  });

  // Velocity across completed sprints for the selected project
  const { data: velocity } = useQuery({
    queryKey: ["analytics", "velocity", activeProjectId],
    queryFn: async () =>
      (await sprintsApi.velocity({ project: activeProjectId })).data,
    enabled: !!activeProjectId,
  });

  // Sprints for the burndown selector
  const { data: sprints = [] } = useQuery({
    queryKey: ["analytics", "sprints", activeProjectId],
    queryFn: async () =>
      unwrap(await sprintsApi.list({ project: activeProjectId })),
    enabled: !!activeProjectId,
  });

  // Default the burndown sprint to the active one, else the most recent.
  const effectiveSprintId = useMemo(() => {
    if (sprintId && sprints.some((s) => s.id === sprintId)) return sprintId;
    const active = sprints.find((s) => s.status === "active");
    if (active) return active.id;
    return sprints[0]?.id ?? null;
  }, [sprintId, sprints]);

  const { data: burndown } = useQuery({
    queryKey: ["analytics", "burndown", effectiveSprintId],
    queryFn: async () => (await sprintsApi.burndown(effectiveSprintId)).data,
    enabled: !!effectiveSprintId,
  });

  // Team throughput / stage dwell
  const { data: throughput } = useQuery({
    queryKey: ["analytics", "throughput", activeProject?.key, windowDays],
    queryFn: async () =>
      (
        await itemsApi.teamThroughput({
          days: windowDays,
          ...(activeProject?.key ? { project: activeProject.key } : {}),
        })
      ).data,
  });

  // PLS-362: per-item cycle times for the scatterplot/histogram
  const { data: cycleData } = useQuery({
    queryKey: ["analytics", "cycle-times", activeProject?.key, windowDays],
    queryFn: async () =>
      (
        await itemsApi.cycleTimes({
          days: windowDays,
          ...(activeProject?.key ? { project: activeProject.key } : {}),
        })
      ).data,
  });

  // PLS-361: end-of-day status counts for the cumulative-flow diagram
  const { data: cfdData } = useQuery({
    queryKey: ["analytics", "cumulative-flow", activeProject?.key, windowDays],
    queryFn: async () =>
      (
        await itemsApi.cumulativeFlow({
          days: windowDays,
          ...(activeProject?.key ? { project: activeProject.key } : {}),
        })
      ).data,
  });

  if (projectsLoading) return <PageLoader />;

  // ---- Velocity chart data ----
  const velocityGroups = (velocity?.sprints || []).map((s) => ({
    label: s.sprint_name,
    values: {
      committed: s.committed_points || 0,
      completed: s.completed_points || 0,
    },
  }));
  const velocitySeries = [
    { key: "committed", label: "Committed", color: SERIES_COLORS.committed },
    { key: "completed", label: "Completed", color: SERIES_COLORS.completed },
  ];

  // ---- Burndown chart data ----
  const burndownPts = burndown?.burndown || [];
  const burndownLabels = burndownPts.map((p) => (p.date || "").slice(5)); // MM-DD
  const burndownLines = [
    {
      key: "ideal",
      label: "Ideal",
      color: SERIES_COLORS.ideal,
      dashed: true,
      points: burndownPts.map((p) => p.ideal),
    },
    {
      key: "remaining",
      label: "Remaining",
      color: SERIES_COLORS.remaining,
      points: burndownPts.map((p) => p.remaining),
    },
  ];

  // ---- Team throughput: aggregate per team across statuses ----
  const throughputRows = throughput?.rows || [];
  const teamAgg = {};
  for (const r of throughputRows) {
    const t = r.team || "unassigned";
    if (!teamAgg[t])
      teamAgg[t] = { team: t, throughput: 0, maxP90: 0, samples: 0 };
    teamAgg[t].throughput += r.throughput_out_per_day || 0;
    teamAgg[t].maxP90 = Math.max(teamAgg[t].maxP90, r.p90_dwell_seconds || 0);
    teamAgg[t].samples += r.sample_count || 0;
  }
  const teams = Object.values(teamAgg).sort(
    (a, b) => b.throughput - a.throughput,
  );
  const throughputGroups = teams.map((t) => ({
    label: t.team,
    values: { throughput: Math.round(t.throughput * 100) / 100 },
  }));
  const throughputSeries = [
    {
      key: "throughput",
      label: "Transitions out / day",
      color: SERIES_COLORS.throughput,
    },
  ];

  // ---- Priority distribution (org-wide) ----
  const byPriority = stats?.by_priority || {};
  const priorityGroups = PRIORITY_ORDER.filter((p) => p in byPriority).map(
    (p) => ({
      label: p[0].toUpperCase() + p.slice(1),
      values: { count: byPriority[p] || 0 },
    }),
  );
  const prioritySeries = [
    { key: "count", label: "Items", color: SERIES_COLORS.dwell },
  ];

  const g = stats?.global_tasks || {};
  const toggleWidget = (widgetId) => {
    setVisibleWidgetIds((current) => toggleWidgetPreference(current, widgetId));
  };

  // ---- Cycle time (PLS-362) ----
  const cycleRows = cycleData?.rows || [];
  const cyclePoints = cycleRows.map((r) => ({
    x: Date.parse(r.completed_at),
    y: r.cycle_seconds,
    label: r.item_key,
  }));
  const cycleValues = cycleRows.map((r) => r.cycle_seconds);
  const cycleSummary = cycleData?.summary || {};
  const cycleP50 =
    cycleSummary.p50_seconds ?? percentileNearestRank(cycleValues, 50);
  const cycleP85 =
    cycleSummary.p85_seconds ?? percentileNearestRank(cycleValues, 85);
  const cycleRefLines = [
    { value: cycleP50, label: "p50", color: SERIES_COLORS.throughput },
    { value: cycleP85, label: "p85", color: SERIES_COLORS.dwell },
  ].filter((r) => Number.isFinite(r.value));
  const LOW_SAMPLE_THRESHOLD = 5;
  const cycleLowSample =
    cycleRows.length > 0 && cycleRows.length < LOW_SAMPLE_THRESHOLD;
  const cycleHistGroups = histogramBuckets(cycleValues, 8).map((b) => ({
    label: `≤${formatDuration(b.max)}`,
    values: { count: b.count },
  }));
  const cycleHistSeries = [
    { key: "count", label: "Completed items", color: SERIES_COLORS.completed },
  ];

  // ---- Cumulative flow (PLS-361) ----
  const cfdDays = cfdData?.days || [];
  const cfdSeries = (cfdData?.statuses || []).map((st, i) => ({
    key: st,
    label: humanize(st),
    color: seriesColor(i),
    points: cfdData?.series?.[st] || [],
  }));

  return (
    <div className="p-4 md:p-6 max-w-7xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <BarChart3 size={24} className="text-brand-600" />
            Analytics
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            Burndown, velocity, throughput and team insights across your work.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn-secondary inline-flex items-center gap-2"
            onClick={() => setShowWidgetPicker((open) => !open)}
            aria-expanded={showWidgetPicker}
            aria-controls="analytics-widget-picker"
          >
            <SlidersHorizontal size={16} />
            Widgets
          </button>
          {projects.length > 0 && (
            <div className="flex items-center gap-2">
              <label
                htmlFor="analytics-project"
                className="text-sm text-gray-500 dark:text-gray-400"
              >
                Project
              </label>
              <select
                id="analytics-project"
                className="input py-1.5 text-sm w-48"
                value={activeProjectId ?? ""}
                onChange={(e) => {
                  setProjectId(Number(e.target.value));
                  setSprintId(null);
                }}
              >
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      </div>

      {showWidgetPicker && (
        <section
          id="analytics-widget-picker"
          className="card p-4"
          aria-label="Customize analytics widgets"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="font-semibold text-gray-900 dark:text-gray-100">
                Customize dashboard widgets
              </h2>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                Choose which analytics widgets are visible. Your selection is
                saved in this browser.
              </p>
            </div>
            <button
              type="button"
              className="btn-secondary text-sm"
              onClick={() =>
                setVisibleWidgetIds([...DEFAULT_ANALYTICS_WIDGET_IDS])
              }
            >
              Reset defaults
            </button>
          </div>
          <fieldset className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <legend className="sr-only">Visible analytics widgets</legend>
            {ANALYTICS_WIDGETS.map((widget) => {
              const checked = visibleWidgets.has(widget.id);
              const isLastChecked = checked && visibleWidgetIds.length === 1;
              return (
                <label
                  key={widget.id}
                  className="flex gap-3 rounded-lg border border-gray-200 bg-white p-3 text-sm dark:border-gray-700 dark:bg-gray-900"
                >
                  <input
                    type="checkbox"
                    className="mt-1 h-4 w-4 rounded border-gray-300 text-brand-600 focus:ring-brand-500"
                    checked={checked}
                    disabled={isLastChecked}
                    onChange={() => toggleWidget(widget.id)}
                  />
                  <span>
                    <span className="font-medium text-gray-900 dark:text-gray-100">
                      {widget.label}
                    </span>
                    <span className="block text-gray-500 dark:text-gray-400">
                      {widget.description}
                    </span>
                    {isLastChecked && (
                      <span className="block text-xs text-gray-400 dark:text-gray-500 mt-1">
                        At least one widget must remain visible.
                      </span>
                    )}
                  </span>
                </label>
              );
            })}
          </fieldset>
        </section>
      )}

      {/* Summary cards */}
      {visibleWidgets.has("summary") && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            title="Total tasks"
            value={g.total ?? "—"}
            icon={ListTodo}
            color="indigo"
          />
          <StatCard
            title="Active"
            value={g.pending ?? "—"}
            icon={Activity}
            color="blue"
          />
          <StatCard
            title="Completed (24h)"
            value={
              g.completed_last_24h ?? stats?.tasks_completed_last_24h ?? "—"
            }
            icon={CheckCircle2}
            color="green"
          />
          <StatCard
            title="Overdue"
            value={stats?.overdue ?? "—"}
            icon={AlertTriangle}
            color="red"
          />
        </div>
      )}

      {/* Charts grid */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {/* Velocity */}
        {visibleWidgets.has("velocity") && (
          <section className="card p-4">
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                <TrendingUp size={18} className="text-brand-600" /> Velocity
              </h2>
              {velocity?.average_velocity != null && (
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  avg {velocity.average_velocity} pts ·{" "}
                  {velocity.sprints_completed || 0} sprints
                </span>
              )}
            </div>
            <BarChart
              groups={velocityGroups}
              series={velocitySeries}
              formatValue={(n) => `${n}`}
              unit="pts"
              ariaLabel="Committed vs completed story points per completed sprint"
            />
          </section>
        )}

        {/* Burndown */}
        {visibleWidgets.has("burndown") && (
          <section className="card p-4">
            <div className="flex items-center justify-between mb-3 gap-2">
              <h2 className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                <Gauge size={18} className="text-brand-600" /> Sprint burndown
              </h2>
              {sprints.length > 0 && (
                <select
                  className="input py-1 text-xs w-40"
                  value={effectiveSprintId ?? ""}
                  onChange={(e) => setSprintId(Number(e.target.value))}
                  aria-label="Select sprint for burndown"
                >
                  {sprints.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                      {s.status === "active" ? " (active)" : ""}
                    </option>
                  ))}
                </select>
              )}
            </div>
            <LineChart
              xLabels={burndownLabels}
              lines={burndownLines}
              formatValue={(n) => `${n}`}
              ariaLabel="Ideal vs remaining story points over the sprint"
            />
          </section>
        )}

        {/* Team throughput */}
        {visibleWidgets.has("throughput") && (
          <section className="card p-4">
            <div className="flex items-center justify-between mb-3 gap-2">
              <h2 className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                <Activity size={18} className="text-brand-600" /> Team
                throughput
              </h2>
              <select
                className="input py-1 text-xs w-28"
                value={windowDays}
                onChange={(e) => setWindowDays(Number(e.target.value))}
                aria-label="Throughput time window"
              >
                {WINDOW_OPTIONS.map((d) => (
                  <option key={d} value={d}>
                    {d} days
                  </option>
                ))}
              </select>
            </div>
            <BarChart
              groups={throughputGroups}
              series={throughputSeries}
              formatValue={(n) => `${n}`}
              ariaLabel={`Transitions out per day per team over ${windowDays} days`}
            />
            {teams.length > 0 && (
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-gray-400 dark:text-gray-500 text-left">
                      <th className="font-medium py-1">Team</th>
                      <th className="font-medium py-1 text-right">Out/day</th>
                      <th className="font-medium py-1 text-right">p90 dwell</th>
                      <th className="font-medium py-1 text-right">Samples</th>
                    </tr>
                  </thead>
                  <tbody>
                    {teams.map((t) => (
                      <tr
                        key={t.team}
                        className="border-t border-gray-100 dark:border-gray-800"
                      >
                        <td className="py-1 text-gray-700 dark:text-gray-300">
                          {t.team}
                        </td>
                        <td className="py-1 text-right text-gray-700 dark:text-gray-300">
                          {Math.round(t.throughput * 100) / 100}
                        </td>
                        <td className="py-1 text-right text-gray-700 dark:text-gray-300">
                          {formatDuration(t.maxP90)}
                        </td>
                        <td className="py-1 text-right text-gray-500 dark:text-gray-400">
                          {t.samples}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}

        {/* Cumulative flow (PLS-361) */}
        {visibleWidgets.has("cumulativeFlow") && (
          <section className="card p-4 xl:col-span-2">
            <div className="flex items-center justify-between mb-3 gap-2">
              <h2 className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                <Layers size={18} className="text-brand-600" /> Cumulative
                flow
              </h2>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {windowDays}d · end-of-day counts
              </span>
            </div>
            <StackedAreaChart
              xLabels={cfdDays}
              series={cfdSeries}
              ariaLabel={`Cumulative flow: items per status at end of each of the last ${windowDays} days`}
            />
            <p className="mt-2 text-[11px] text-gray-400 dark:text-gray-500">
              Items that reached a terminal status before this window are
              excluded, so the terminal band shows work completed within the
              window rather than all-time history.
            </p>
          </section>
        )}

        {/* Cycle time scatterplot (PLS-362) */}
        {visibleWidgets.has("cycleTime") && (
          <section className="card p-4">
            <div className="flex items-center justify-between mb-3 gap-2">
              <h2 className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                <Timer size={18} className="text-brand-600" /> Cycle time
              </h2>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {cycleRows.length} completed · {windowDays}d
                {cycleP50 != null
                  ? ` · p50 ${formatDuration(cycleP50)}`
                  : ""}
                {cycleP85 != null
                  ? ` · p85 ${formatDuration(cycleP85)}`
                  : ""}
              </span>
            </div>
            {cycleLowSample && (
              <p
                className="mb-2 text-xs text-amber-600 dark:text-amber-400"
                role="note"
              >
                Low sample size ({cycleRows.length} item
                {cycleRows.length === 1 ? "" : "s"}) — percentiles are
                indicative only.
              </p>
            )}
            <ScatterChart
              points={cyclePoints}
              refLines={cycleRefLines}
              ariaLabel={`Cycle time of each item completed in the last ${windowDays} days (first status move to completion)`}
            />
            <p className="mt-2 text-[11px] text-gray-400 dark:text-gray-500">
              Cycle time = first status movement → completion. Items completed
              without recorded transitions count their full age.
            </p>
          </section>
        )}

        {/* Cycle time histogram (PLS-362) */}
        {visibleWidgets.has("cycleDistribution") && (
          <section className="card p-4">
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                <Timer size={18} className="text-brand-600" /> Cycle-time
                distribution
              </h2>
            </div>
            <BarChart
              groups={cycleHistGroups}
              series={cycleHistSeries}
              formatValue={(n) => `${n}`}
              ariaLabel={`Histogram of cycle times for items completed in the last ${windowDays} days`}
            />
          </section>
        )}

        {/* Priority distribution */}
        {visibleWidgets.has("priority") && (
          <section className="card p-4">
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                <Layers size={18} className="text-brand-600" /> Open items by
                priority
              </h2>
            </div>
            <BarChart
              groups={priorityGroups}
              series={prioritySeries}
              formatValue={(n) => `${n}`}
              ariaLabel="Item count by priority"
            />
          </section>
        )}
      </div>
    </div>
  );
}
