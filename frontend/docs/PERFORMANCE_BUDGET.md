# Sellable-shell frontend performance budget (PLAT-875)

Shared performance budget for **customer-facing sellable product shells** (Pulse,
Connect, Books — and the standard to adopt for HIVE/Cortex). The first impression,
mobile/low-bandwidth users, and the unauth/login flow must not pay for the full
authenticated app bundle before any value is visible.

## Budget (initial route — what an unauthenticated visitor downloads first)

| Metric | Target (warn) | Hard ceiling (fail) |
|---|---|---|
| Initial JS (the entry + its eager imports), **gzip** | ≤ 250 KB | ≤ 400 KB |
| Initial CSS, gzip | ≤ 50 KB | ≤ 80 KB |
| Any single emitted chunk (uncompressed) | ≤ 600 KB | — (vite `chunkSizeWarningLimit`) |
| Largest Contentful Paint (if measured, mid-tier mobile / 4G) | ≤ 2.5 s | ≤ 4 s |
| Total Blocking Time (if measured) | ≤ 200 ms | ≤ 600 ms |

"Initial" = the entry chunk + the vendor chunks + anything imported **eagerly**
(the shell: router, auth/provider context, Layout, ProtectedRoute). Route pages and
heavy widgets must be lazy so they are **not** in the initial download.

Baseline that motivated this (2026-06-26, uncompressed initial JS): Pulse ~1.8 MB,
Connect ~1.0 MB, Books ~643 KB — all shipped the full app up front.

## How we meet it

1. **Route-level code splitting** — every route page is `React.lazy(() => import(...))`
   behind a `<Suspense>` boundary; only the shell is eager. Heavy always-mounted
   widgets (e.g. the chat widget) are lazy too (fallback `null`). Admin/rarely-used
   areas are *always* lazy. (Pulse: `frontend/src/App.jsx`.)
2. **Vendor chunking** — `build.rollupOptions.output.manualChunks` splits
   react/react-dom/react-router and react-query into long-cacheable vendor chunks so
   the app chunk is small and vendor isn't re-downloaded on every deploy.
3. **Regression visibility** — `build.chunkSizeWarningLimit` (600 KB) makes vite warn
   at build (CI-visible) when a chunk regresses past budget. CI should surface the
   per-chunk size table from `vite build` output; a chunk over the hard ceiling fails.

## Applying to a new shell

- Set the same `build.chunkSizeWarningLimit` + `manualChunks` in the service's
  `vite.config.js` (merged on top of the shared `shizuha-ui` config).
- Convert route components to `React.lazy` + a `<Suspense>` boundary; keep providers,
  router, Layout, and the auth-gate eager.
- Verify the unauth/login route renders without pulling authenticated-only chunks
  (DevTools Network → first navigation should not fetch dashboard/admin chunks).

Cross-product home: also mirrored in the Engineering wiki (frontend standards) so
HIVE/Cortex adopt the same budget.
