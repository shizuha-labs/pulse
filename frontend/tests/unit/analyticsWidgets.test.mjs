import assert from "node:assert/strict";
import {
  ANALYTICS_WIDGET_STORAGE_KEY,
  DEFAULT_ANALYTICS_WIDGET_IDS,
  loadWidgetPreference,
  normalizeWidgetSelection,
  saveWidgetPreference,
  toggleWidgetPreference,
} from "../../src/utils/analyticsWidgets.js";

function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      data.set(key, value);
    },
  };
}

assert.deepEqual(
  normalizeWidgetSelection(["velocity", "velocity", "unknown", "summary"]),
  ["velocity", "summary"],
);
assert.deepEqual(normalizeWidgetSelection([]), DEFAULT_ANALYTICS_WIDGET_IDS);
assert.deepEqual(
  toggleWidgetPreference(DEFAULT_ANALYTICS_WIDGET_IDS, "velocity"),
  [
    "summary",
    "burndown",
    "throughput",
    "cumulativeFlow",
    "cycleTime",
    "cycleDistribution",
    "priority",
  ],
);
assert.deepEqual(toggleWidgetPreference(["velocity"], "velocity"), [
  "velocity",
]);
assert.deepEqual(toggleWidgetPreference(["summary"], "velocity"), [
  "summary",
  "velocity",
]);
assert.deepEqual(toggleWidgetPreference(["summary"], "not-real"), ["summary"]);

const storage = memoryStorage();
saveWidgetPreference(["throughput", "priority"], storage);
assert.equal(
  storage.getItem(ANALYTICS_WIDGET_STORAGE_KEY),
  '["throughput","priority"]',
);
assert.deepEqual(loadWidgetPreference(storage), ["throughput", "priority"]);

const corrupt = memoryStorage({ [ANALYTICS_WIDGET_STORAGE_KEY]: "{bad json" });
assert.deepEqual(loadWidgetPreference(corrupt), DEFAULT_ANALYTICS_WIDGET_IDS);

console.log("analyticsWidgets tests passed");
