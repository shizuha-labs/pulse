import assert from 'node:assert/strict'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
const { MemoryRouter } = createRequire(import.meta.url)('react-router-dom')
const { QueryClient, QueryClientProvider } = createRequire(import.meta.url)('@tanstack/react-query')
import { Module, createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../', import.meta.url))
const result = await build({ stdin: { contents: "export { default as Dashboard } from './src/pages/Dashboard.jsx'; export { default as Context } from './src/contexts/AuthContext.jsx';export { default as OrgContext } from './src/contexts/OrganizationContext.jsx';", resolveDir: root }, bundle: true, platform: 'node', format: 'cjs', write: false, jsx: 'automatic', external: ['react', 'react/*', 'react-dom/*', 'react-router-dom', '@tanstack/react-query'], define: { 'import.meta.env': '{}' } })
const compiled = new Module(root + 'dashboard-test.cjs')
compiled.require = createRequire(import.meta.url)
compiled._compile(result.outputFiles[0].text, root + 'dashboard-test.cjs')
const { Dashboard, Context, OrgContext } = compiled.exports
const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false, staleTime: Infinity, gcTime: Infinity } } })
const auth = { user: { id: 3 }, isAuthenticated: true, currentOrganization: { id: 1, slug: 'shizuha', name: 'Shizuha' }, loading: false }
function render(overrides = {}) {
  const context = { ...auth, ...overrides }
  let tree = React.createElement(Context.Provider, { value: context }, React.createElement(Dashboard))
  if (OrgContext) tree = React.createElement(OrgContext.Provider, { value: { currentOrganization: context.currentOrganization, isOrganizationResolved: true, organizations: [] } }, tree)
  return renderToStaticMarkup(React.createElement(QueryClientProvider, { client }, React.createElement(MemoryRouter, null, tree)))
    .replace(/<[^>]*>/g, '').replace(/\s+/g, ' ')
}
async function fail(queryKey, status = 503) {
  await client.fetchQuery({ queryKey, staleTime: 0, queryFn: () => Promise.reject(Object.assign(new Error('unavailable'), { response: { status } })), retry: false }).catch(() => {})
}
const loading = render()
assert.match(loading, /Loading/i)
assert.doesNotMatch(loading, /Total Employees0|Pending Tasks0|All caught up/)
const keys = client.getQueryCache().getAll().map(q => q.queryKey)
for (const key of keys) await fail(key)
let text = render()
assert.match(text, /unavailable/i)
assert.doesNotMatch(text, /Pending Tasks0|Pending Actions0|Firing Alerts0|Overdue0|All caught up|No active tasks assigned|No tasks found/)
const statsKey = keys.find(k => k[0] === 'statistics')
const pendingKey = keys.find(k => k[0] === 'pending')
const overdueKey = keys.find(k => k[0] === 'overdue')
const tasksKey = keys.find(k => k[0] === 'my-dashboard-tasks')
client.setQueryData(statsKey, { tasks: { pending: 0 }, actions: { pending: 0 }, alerts: { firing: 0 } })
client.setQueryData(pendingKey, { actions: [], alerts: [] })
client.setQueryData(overdueKey, { count: 7, items: [] })
client.setQueryData(tasksKey, { results: [], count: 0 })
text = render()
assert.match(text, /Pending Tasks0/)
assert.match(text, /Overdue7/)
client.setQueryData(overdueKey, { count: 0, items: [] })
assert.match(text, /All caught up/)
client.setQueryData(statsKey, { tasks: { pending: 12 }, actions: { pending: 0 }, alerts: { firing: 0 } })
await fail(statsKey)
text = render()
assert.match(text, /Pending Tasks12/)
assert.match(text, /last (?:successful )?observation/i)
await fail(statsKey, 401)
text = render()
assert.doesNotMatch(text, /Pending Tasks12/)
assert.doesNotMatch(render({ user: { id: 99 } }), /Pending Tasks12/)
assert.doesNotMatch(render({ currentOrganization: { id: 2, slug: 'other' } }), /Pending Tasks12/)
const query = client.getQueryCache().find({ queryKey: statsKey })
assert.equal(query.options.refetchInterval, 30000)
assert.equal(query.options.refetchIntervalInBackground, false)
assert.equal(query.options.refetchOnWindowFocus, true)
await client.fetchQuery({ queryKey: statsKey, staleTime: 0, queryFn: async () => ({ tasks: { pending: 23 }, actions: { pending: 0 }, alerts: { firing: 0 } }) })
assert.match(render(), /Pending Tasks23/)
client.clear()
console.log('actual dashboard: initial failure, measured zero, retained observation, permission failure, actor/tenant isolation and refresh contract PASS')
