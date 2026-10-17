/**
 * Categorical palette for the analytics charts (PLS-274).
 *
 * Explicit hex values (not Tailwind `fill-*` utilities) because SVG presentation
 * attributes set from dynamic props would be purged by Tailwind's JIT scanner.
 * Chosen for adequate contrast in BOTH light and dark cards and to stay legible
 * for the common deuteranopia/protanopia cases (blue + amber + teal, not
 * red/green pairs). Muted grid/axis tokens read on either theme.
 */
export const SERIES_COLORS = {
  committed: '#94a3b8', // slate-400 — planned/target
  completed: '#4f46e5', // brand-600 — actual delivered
  ideal: '#94a3b8', // slate-400 — ideal burndown guide
  remaining: '#4f46e5', // brand-600 — actual remaining
  throughput: '#0d9488', // teal-600
  dwell: '#d97706', // amber-600
}

// Ordered palette for arbitrary N-series (e.g. per-team) charts.
export const CATEGORICAL = [
  '#4f46e5', // indigo
  '#0d9488', // teal
  '#d97706', // amber
  '#db2777', // pink
  '#65a30d', // lime
  '#0891b2', // cyan
  '#7c3aed', // violet
  '#ca8a04', // yellow
]

export function seriesColor(index) {
  return CATEGORICAL[index % CATEGORICAL.length]
}
