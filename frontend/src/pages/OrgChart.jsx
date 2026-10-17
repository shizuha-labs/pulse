import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Network, Users, RefreshCw, AlertCircle,
  ChevronDown, ChevronRight, ChevronUp,
  LayoutGrid, List,
  User, Bot, Filter, X,
} from 'lucide-react'
import { tasksApi } from '../services/api'
import EmptyState from '../components/EmptyState'
import { PageLoader } from '../components/LoadingSpinner'
import AgentAvatar from '../components/shared/AgentAvatar'
import StatusBadge from '../components/shared/StatusBadge'
import AgentTierBadge from '../components/shared/AgentTierBadge'
import { cn } from '../utils/cn'
import { formatTeamName } from '../utils/teamLabels'

// PLAT-358 (F3): People summary nodes — StatusBadge + agent/human indicator +
//   AgentTierBadge on each node; click to expand full card with live workload counts.
// PLAT-360 (F5): Filter bar — alive-only, by-backend, by-availability, department.
// Both source data from /api/admin/org-chart/snapshot (PLAT-320/B1).

const REFRESH_MS = 30_000

// Stable node key: prefer user_id, fall back to email
const nodeKey = (n) => (n.user_id != null ? String(n.user_id) : 'email:' + (n.email || ''))

// Effective tier — falls back to is_agent flag when model_tier absent.
// Must be called BEFORE using the result in any filter (P2 #4).
function tierFor(node) {
  return node.model_tier || (node.is_agent ? 'shizuha' : 'human')
}

// Map snapshot availability_status ('available'|'unavailable'|'unknown') → StatusBadge prop
function toStatus(availability_status) {
  if (availability_status === 'available')   return 'active'
  if (availability_status === 'unavailable') return 'stopped'
  return 'unknown'
}

// Build manager→report tree from a flat list of nodes.
// Nodes whose manager_id is absent or points outside the list become roots.
// TreeNode caps depth to guard against corrupt cycles in data.
function buildForest(nodes) {
  const byId = new Map()
  for (const n of nodes) {
    if (n.user_id != null) byId.set(n.user_id, { ...n, reports: [] })
  }

  // kei P2: include email-only nodes in the nest pass (not unconditional roots) —
  // an email-only node with manager_id pointing at a user_id-bearing node should
  // nest under that node, not float as a root.
  const enriched = nodes.map(n =>
    n.user_id != null ? byId.get(n.user_id) : { ...n, reports: [] }
  )

  const roots = []
  for (const n of enriched) {
    const parent = n.manager_id != null ? byId.get(n.manager_id) : null
    if (parent && parent !== n) {  // guard self-manager
      parent.reports.push(n)
    } else {
      roots.push(n)
    }
  }

  // kei P2: cycle/unreachable fallback — after placing all nodes, walk from roots
  // and promote any unreachable node (cycle participant) to root so the team body
  // is never blank despite having counted members.
  const reachable = new Set()
  const stack = [...roots]
  while (stack.length) {
    const node = stack.pop()
    if (reachable.has(node)) continue
    reachable.add(node)
    node.reports?.forEach(c => stack.push(c))
  }
  for (const n of enriched) {
    if (!reachable.has(n)) roots.push(n)
  }

  return roots
}

// Compact badge row shown on every node card (PLAT-358 compact view)
function NodeBadgeRow({ node }) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap mt-0.5">
      <StatusBadge status={toStatus(node.availability_status)} size="sm" />
      {node.is_agent
        ? <span className="inline-flex items-center gap-0.5 text-[10px] text-blue-600 dark:text-blue-400"><Bot size={11} /> agent</span>
        : <span className="inline-flex items-center gap-0.5 text-[10px] text-amber-600 dark:text-amber-400"><User size={11} /> human</span>
      }
      <AgentTierBadge tier={tierFor(node)} size="sm" />
    </div>
  )
}

// Expanded card content (PLAT-358 full card)
function NodeFullCard({ node, workload }) {
  // Keyed by str(user_id); fall back to email for idless members (P2 #2)
  const wl = (node.user_id != null ? workload[String(node.user_id)] : null)
    || workload[node.email]
    || {}
  const counts = [
    { label: 'open',      val: wl.open ?? 0,        cls: 'bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300' },
    { label: 'in prog',   val: wl.in_progress ?? 0, cls: 'bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-300' },
    { label: 'in review', val: wl.in_review ?? 0,   cls: 'bg-purple-50 dark:bg-purple-900/20 text-purple-700 dark:text-purple-300' },
    { label: 'blocked',   val: wl.blocked ?? 0,     cls: 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300' },
  ]

  return (
    <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-700 space-y-2 text-xs">
      <div className="flex items-center gap-2 flex-wrap">
        <StatusBadge status={toStatus(node.availability_status)} size="sm" show_label />
        <AgentTierBadge tier={tierFor(node)} size="sm" show_label />
      </div>
      <div className="text-gray-400 font-mono text-[10px] truncate">{node.email}</div>
      <div className="grid grid-cols-4 gap-1 text-center">
        {counts.map(({ label, val, cls }) => (
          <div key={label} className={cn('rounded py-1', cls)}>
            <div className="text-sm font-bold leading-none">{val}</div>
            <div className="text-[9px] mt-0.5 opacity-75">{label}</div>
          </div>
        ))}
      </div>
      {/* PLAT-359 (F4): progress at-a-glance — total tasks / in-progress / completed(30d) / overdue */}
      <div className="flex items-center gap-1.5 text-[10px] text-gray-500 dark:text-gray-400 pt-0.5 flex-wrap">
        <span><span className="font-bold text-gray-700 dark:text-gray-300">{(wl.open ?? 0) + (wl.in_progress ?? 0) + (wl.blocked ?? 0) + (wl.in_review ?? 0)}</span> tasks</span>
        <span>·</span><span><span className="font-bold text-amber-600 dark:text-amber-400">{wl.in_progress ?? 0}</span> in prog</span>
        <span>·</span><span><span className="font-bold text-green-600 dark:text-green-400">{wl.completed ?? 0}</span> done·30d</span>
        <span>·</span><span className={cn((wl.overdue ?? 0) > 0 && 'text-red-600 dark:text-red-400 font-semibold')}>{wl.overdue ?? 0} overdue</span>
      </div>
    </div>
  )
}

// Single person node — compact badge row + optional expanded full card
function NodeCard({ node, workload }) {
  const [expanded, setExpanded] = useState(false)
  const name = node.display_name || node.email.split('@')[0]

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-3">
      <button
        type="button"
        className="w-full text-left"
        aria-expanded={expanded}
        onClick={() => setExpanded(e => !e)}
      >
        <div className="flex items-start gap-2">
          <AgentAvatar display_name={name} size="sm" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-1">
              <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{name}</span>
              {expanded
                ? <ChevronUp size={13} className="text-gray-400 shrink-0" />
                : <ChevronDown size={13} className="text-gray-400 shrink-0" />
              }
            </div>
            <NodeBadgeRow node={node} />
          </div>
        </div>
      </button>
      {expanded && <NodeFullCard node={node} workload={workload} />}
    </div>
  )
}

// Recursive tree node — renders NodeCard + indented direct reports (P2 #3)
function TreeNode({ node, workload, depth = 0 }) {
  if (depth > 12) return null  // guard against corrupt manager cycles
  return (
    <div className={cn(depth > 0 && 'ml-4 border-l border-gray-200 dark:border-gray-700 pl-2 mt-2')}>
      <NodeCard node={node} workload={workload} />
      {node.reports?.map(child => (
        <TreeNode key={nodeKey(child)} node={child} workload={workload} depth={depth + 1} />
      ))}
    </div>
  )
}

// Filter bar — all filtering is client-side against the snapshot (PLAT-360)
const BACKEND_OPTIONS = [
  { value: '', label: 'All backends' },
  { value: 'claude_code', label: 'Claude Code' },
  { value: 'codex', label: 'Codex' },
  { value: 'shizuha', label: 'Shizuha' },
]
const AVAIL_OPTIONS = [
  { value: '', label: 'Any status' },
  { value: 'available', label: 'Active' },
  { value: 'unavailable', label: 'Stopped' },
  { value: 'unknown', label: 'Unknown' },
]

function FilterBar({ params, setParam, clearAll, departments }) {
  const alive   = params.get('alive') === '1'
  const backend = params.get('backend') || ''
  const avail   = params.get('avail') || ''
  const dept    = params.get('dept') || ''
  const hasFilter = alive || backend || avail || dept

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 px-3 py-2">
      <Filter size={14} className="text-gray-400 shrink-0" aria-label="Filters" />

      <button
        onClick={() => setParam('alive', alive ? '' : '1')}
        className={cn(
          'text-xs px-2 py-1 rounded-full border transition-colors',
          alive
            ? 'border-green-400 bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300'
            : 'border-gray-200 dark:border-gray-700 text-gray-500 hover:border-gray-300',
        )}
      >
        Active only
      </button>

      <select
        value={backend}
        onChange={e => setParam('backend', e.target.value)}
        className="text-xs rounded border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 px-2 py-1"
        aria-label="Filter by backend"
      >
        {BACKEND_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>

      <select
        value={avail}
        onChange={e => setParam('avail', e.target.value)}
        className="text-xs rounded border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 px-2 py-1"
        aria-label="Filter by availability"
      >
        {AVAIL_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>

      {departments.length > 0 && (
        <select
          value={dept}
          onChange={e => setParam('dept', e.target.value)}
          className="text-xs rounded border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 px-2 py-1"
          aria-label="Filter by department"
        >
          <option value="">All departments</option>
          {departments.map(d => (
            <option key={d.id} value={String(d.id)}>{d.name}</option>
          ))}
        </select>
      )}

      {hasFilter && (
        <button
          onClick={clearAll}
          className="ml-auto flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
        >
          <X size={12} /> Clear filters
        </button>
      )}
    </div>
  )
}

export default function OrgChart() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [collapsed, setCollapsed] = useState({})
  // PLAT-339 (F2): persist the individual <-> team-clustered choice for the session.
  const [viewMode, setViewMode] = useState(() => {
    try { return localStorage.getItem('orgChartViewMode') === 'team' ? 'team' : 'individual' } catch { return 'individual' }
  })
  const setView = (mode) => {
    setViewMode(mode)
    try { localStorage.setItem('orgChartViewMode', mode) } catch { /* localStorage unavailable — non-fatal */ }
  }

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['orgChartSnapshot'],
    queryFn: () => tasksApi.orgChartSnapshot().then(r => r.data),
    refetchInterval: REFRESH_MS,
  })

  const setParam = (key, value) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value); else next.delete(key)
    setSearchParams(next, { replace: true })
  }
  const clearAll = () => setSearchParams({}, { replace: true })

  if (isLoading) return <PageLoader />

  const nodes       = data?.nodes ?? []
  const teams       = data?.teams ?? []
  const departments = data?.departments ?? []
  const workload    = data?.workload ?? {}
  const teamWorkload = data?.team_workload ?? {}

  // Client-side filter application (PLAT-360)
  const filterAlive   = searchParams.get('alive') === '1'
  const filterBackend = searchParams.get('backend') || ''
  const filterAvail   = searchParams.get('avail') || ''
  const filterDept    = searchParams.get('dept') || ''

  const teamDeptMap = {}
  teams.forEach(t => { if (t.department_id) teamDeptMap[String(t.id)] = String(t.department_id) })

  const visibleNodes = nodes.filter(n => {
    const tier = tierFor(n)  // compute BEFORE filter — model_tier may be absent (P2 #4)
    if (filterAlive   && n.availability_status !== 'available') return false
    if (filterBackend && tier !== filterBackend)                return false
    if (filterAvail   && n.availability_status !== filterAvail) return false
    // kei P2: filterDept is NOT applied here — it operates at the team level (below)
    // so secondary-team members (primary team_id in a different dept) remain in
    // nodesById and rosterFor() can still resolve them via member_ids.
    return true
  })

  // nodesById: keyed by nodeKey(n) — handles both user_id-bearing and idless
  // (email-only) nodes.  member_ids from the backend uses the same stable-key
  // contract: integer for users with an id, 'email:<addr>' for idless members.
  const nodesById = new Map(visibleNodes.map(n => [nodeKey(n), n]))

  // nodesByTeam: fallback for teams that don't supply member_ids
  const nodesByTeam = new Map()
  visibleNodes.forEach(n => {
    const tid = String(n.team_id)
    if (!nodesByTeam.has(tid)) nodesByTeam.set(tid, [])
    nodesByTeam.get(tid).push(n)
  })

  // Resolve team roster via member_ids first, then team_id grouping.
  // member_ids entries are integers (numeric user_id) or 'email:...' strings;
  // nodeKey() uses String(user_id) so normalise integers before lookup.
  const rosterFor = (team) =>
    Array.isArray(team.member_ids) && team.member_ids.length
      ? team.member_ids.map(id => nodesById.get(typeof id === 'number' ? String(id) : id)).filter(Boolean)
      : (nodesByTeam.get(String(team.id)) || [])

  // kei P2: department filter applied here so all nodes stay in nodesById for
  // rosterFor() member_ids lookups; only teams in the selected dept are shown.
  const visibleTeams = teams.filter(t => {
    if (filterDept && teamDeptMap[String(t.id)] !== filterDept) return false
    return rosterFor(t).length > 0
  })
  const totalActive  = visibleNodes.filter(n => n.availability_status === 'available').length

  const toggle = tid => setCollapsed(p => ({ ...p, [tid]: !p[tid] }))

  return (
    <div className="space-y-4 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <Network size={24} className="text-blue-500" /> Org Chart
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {visibleNodes.length} member{visibleNodes.length !== 1 ? 's' : ''} · {totalActive} active
            {data?.generated_at && <> · updated {new Date(data.generated_at).toLocaleTimeString()}</>}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden" role="group" aria-label="Org chart view mode">
            <button type="button" onClick={() => setView('individual')} aria-pressed={viewMode === 'individual'}
              className={cn('flex items-center gap-1.5 px-3 py-1.5 text-xs', viewMode === 'individual' ? 'bg-blue-500 text-white' : 'text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800')}>
              <List size={14} /> Individual
            </button>
            <button type="button" onClick={() => setView('team')} aria-pressed={viewMode === 'team'}
              className={cn('flex items-center gap-1.5 px-3 py-1.5 text-xs', viewMode === 'team' ? 'bg-blue-500 text-white' : 'text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800')}>
              <LayoutGrid size={14} /> Teams
            </button>
          </div>
          <button onClick={() => refetch()} className="btn-icon" aria-label="Refresh" title="Refresh">
            <RefreshCw size={18} className={cn(isFetching && 'animate-spin')} />
          </button>
        </div>
      </div>

      {/* PLAT-360: Filter bar */}
      <FilterBar
        params={searchParams}
        setParam={setParam}
        clearAll={clearAll}
        departments={departments}
      />

      {/* Team groups with hierarchical node cards */}
      {isError ? (
        <EmptyState
          icon={AlertCircle}
          title="Couldn't load org chart"
          description="The snapshot endpoint did not respond. Staff/admin access is required."
        />
      ) : nodes.length === 0 ? (
        <EmptyState icon={Users} title="No org data" description="No agents or teams in this organization yet." />
      ) : viewMode === 'team' ? (
        // PLAT-339 (F2): team-clustered view — each team as a bubble (name, lead, member
        // count) from the same snapshot data (no extra API call). Filters via rosterFor()
        // which resolves from visibleNodes, so client-side filters apply here too.
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {teams.map((team) => {
            const teamNodes = rosterFor(team)
            if (teamNodes.length === 0) return null
            // Resolve the team lead, robust to email-only and multi-team rosters:
            //  1. lead_id (id-based leads).
            //  2. lead_email -> the 'email:'+email node key (aoi P2): an email-only
            //     lead has lead_id=null, and if their node was de-duped under an
            //     earlier team its role reflects THAT team — so a teamNodes role==='lead'
            //     scan misses them. The email key resolves the de-duped node directly.
            //  3. role==='lead' member in this team (single-team email-only fallback).
            const lead =
              (team.lead_id != null ? nodesById.get(team.lead_id) : null)
              || (team.lead_email ? nodesById.get('email:' + team.lead_email) : null)
              || teamNodes.find((n) => n.role === 'lead')
              || null
            return (
              <div key={team.id} className="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-900/40 p-4">
                <div className="flex items-center gap-2 mb-3">
                  <Users size={18} className="text-gray-400" />
                  <span className="font-semibold text-gray-900 dark:text-gray-100">{formatTeamName(team.name)}</span>
                  <span className="ml-auto text-xs text-gray-400">{teamNodes.length} member{teamNodes.length === 1 ? '' : 's'}</span>
                </div>
                {lead ? (
                  <div className="flex items-center gap-2">
                    <AgentAvatar display_name={lead.display_name} size="sm" />
                    <div className="min-w-0">
                      <div className="text-sm text-gray-900 dark:text-gray-100 truncate">{lead.display_name}</div>
                      <div className="text-[10px] uppercase tracking-wide text-indigo-500">lead</div>
                    </div>
                  </div>
                ) : (
                  <div className="text-xs text-gray-400">No lead assigned</div>
                )}
                {/* PLAT-359 (F4): per-team progress rollup (tasks / in-progress / completed(30d) / overdue) */}
                {(() => {
                  const tw = teamWorkload[String(team.id)] || {}
                  const total = (tw.open ?? 0) + (tw.in_progress ?? 0) + (tw.blocked ?? 0) + (tw.in_review ?? 0)
                  return (
                    <div className="flex items-center gap-1.5 text-[10px] text-gray-500 dark:text-gray-400 mt-3 pt-2 border-t border-gray-100 dark:border-gray-700 flex-wrap">
                      <span><span className="font-bold text-gray-700 dark:text-gray-300">{total}</span> tasks</span>
                      <span>·</span><span><span className="font-bold text-amber-600 dark:text-amber-400">{tw.in_progress ?? 0}</span> in prog</span>
                      <span>·</span><span><span className="font-bold text-green-600 dark:text-green-400">{tw.completed ?? 0}</span> done·30d</span>
                      <span>·</span><span className={cn((tw.overdue ?? 0) > 0 && 'text-red-600 dark:text-red-400 font-semibold')}>{tw.overdue ?? 0} overdue</span>
                    </div>
                  )
                })()}
              </div>
            )
          })}
        </div>
      ) : visibleTeams.length === 0 ? (
        <EmptyState icon={Users} title="No members found" description="No members match the current filters." />
      ) : (
        <div className="space-y-3">
          {visibleTeams.map(team => {
            const tid     = String(team.id)
            const members = rosterFor(team)
            const active  = members.filter(n => n.availability_status === 'available').length
            const forest  = buildForest(members)
            const isCollapsed = collapsed[tid]

            return (
              <div key={tid} className="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-900/40">
                <button
                  type="button"
                  onClick={() => toggle(tid)}
                  className="w-full flex items-center justify-between px-4 py-3"
                  aria-expanded={!isCollapsed}
                >
                  <div className="flex items-center gap-2">
                    {isCollapsed ? <ChevronRight size={18} /> : <ChevronDown size={18} />}
                    <Users size={18} className="text-gray-400" />
                    <span className="font-semibold text-gray-900 dark:text-gray-100">{formatTeamName(team.name)}</span>
                    <span className="text-xs text-gray-400">{members.length} member{members.length !== 1 ? 's' : ''}</span>
                  </div>
                  <span className="text-xs text-gray-500">{active} active</span>
                </button>
                {!isCollapsed && (
                  <div className="px-4 pb-4 space-y-2">
                    {forest.map(root => (
                      <TreeNode key={nodeKey(root)} node={root} workload={workload} />
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
