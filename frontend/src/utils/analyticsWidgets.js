export const ANALYTICS_WIDGETS = [
  {
    id: "summary",
    label: "Summary cards",
    description: "Total, active, completed and overdue task counts.",
  },
  {
    id: "velocity",
    label: "Velocity",
    description: "Committed vs completed story points per sprint.",
  },
  {
    id: "burndown",
    label: "Sprint burndown",
    description: "Ideal vs remaining sprint work over time.",
  },
  {
    id: "throughput",
    label: "Team throughput",
    description: "Transitions out per day and p90 dwell by team.",
  },
  {
    id: "cumulativeFlow",
    label: "Cumulative flow",
    description: "End-of-day item counts by workflow status.",
  },
  {
    id: "cycleTime",
    label: "Cycle time",
    description: "Scatterplot of completed item cycle times.",
  },
  {
    id: "cycleDistribution",
    label: "Cycle-time distribution",
    description: "Histogram of completed item cycle times.",
  },
  {
    id: "priority",
    label: "Open items by priority",
    description: "Open task distribution by priority.",
  },
];

export const ANALYTICS_WIDGET_STORAGE_KEY = "pulse.analytics.visibleWidgets.v1";

export const DEFAULT_ANALYTICS_WIDGET_IDS = ANALYTICS_WIDGETS.map(
  (widget) => widget.id,
);

const VALID_WIDGET_IDS = new Set(DEFAULT_ANALYTICS_WIDGET_IDS);

export function normalizeWidgetSelection(ids) {
  if (!Array.isArray(ids)) return [...DEFAULT_ANALYTICS_WIDGET_IDS];
  const selected = [];
  for (const id of ids) {
    if (VALID_WIDGET_IDS.has(id) && !selected.includes(id)) selected.push(id);
  }
  return selected.length > 0 ? selected : [...DEFAULT_ANALYTICS_WIDGET_IDS];
}

export function loadWidgetPreference(storage = globalThis?.localStorage) {
  try {
    const raw = storage?.getItem?.(ANALYTICS_WIDGET_STORAGE_KEY);
    if (!raw) return [...DEFAULT_ANALYTICS_WIDGET_IDS];
    return normalizeWidgetSelection(JSON.parse(raw));
  } catch (_err) {
    return [...DEFAULT_ANALYTICS_WIDGET_IDS];
  }
}

export function saveWidgetPreference(ids, storage = globalThis?.localStorage) {
  const normalized = normalizeWidgetSelection(ids);
  try {
    storage?.setItem?.(
      ANALYTICS_WIDGET_STORAGE_KEY,
      JSON.stringify(normalized),
    );
  } catch (_err) {
    // Storage may be unavailable in private-mode/browser-policy contexts; the
    // dashboard still works for the current render with in-memory state.
  }
  return normalized;
}

export function toggleWidgetPreference(ids, widgetId) {
  const current = normalizeWidgetSelection(ids);
  if (!VALID_WIDGET_IDS.has(widgetId)) return current;
  if (current.includes(widgetId)) {
    return current.length === 1
      ? current
      : current.filter((id) => id !== widgetId);
  }
  const next = new Set(current);
  next.add(widgetId);
  return DEFAULT_ANALYTICS_WIDGET_IDS.filter((id) => next.has(id));
}
