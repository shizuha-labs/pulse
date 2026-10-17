import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, AlertCircle, AlertTriangle, Bot, ChevronDown, ChevronRight,
  ExternalLink, GitPullRequest, RefreshCw, ShieldAlert,
} from 'lucide-react'
import { tasksApi } from '../services/api'
import EmptyState from '../components/EmptyState'
import { PageLoader } from '../components/LoadingSpinner'
import { cn } from '../utils/cn'
import { formatTeamName } from '../utils/teamLabels'

// PLS-305 (PLS-270 Slice B): agent coordination console — a read surface over
// canonical Pulse state (items, links, PR metadata, roster, availability).
// Quick actions deep-link into the existing task/queue UIs, which own the
// assign/comment/link/transition API calls and their workflow permissions —
// this page never mutates state itself.

const REFRESH_MS = 30_000

const AVAILABILITY_STYLES = {
  active: 'bg-green-500',
  inactive: 'bg-gray-400',
  on_leave: 'bg-amber-500',
  disabled: 'bg-red-500',
}

function AvailabilityDot({ availability, stale }) {
  const color = stale ? 'bg-amber-500' : (AVAILABILITY_STYLES[availability.state] || 'bg-gray-400')
  const label = stale ? 'stale' : availability.state
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
      <span className={cn('inline-block w-2 h-2 rounded-full', color)} title={availability.reason || label} />
      {label.replace('_', ' ')}
    </span>
  )
}

function WipPressureBar({ count, cap }) {
  const pct = Math.min(100, Math.round(((count || 0) / (cap || 1)) * 100))
  const tone = pct >= 90 ? 'bg-red-500' : pct >= 60 ? 'bg-amber-500' : 'bg-blue-500'
  return (
    <div className="flex items-center gap-2" title={`${count}/${cap} active items (WIP cap)`}>
      <div className="w-24 h-1.5 rounded bg-gray-200 dark:bg-gray-700 overflow-hidden">
        <div className={cn('h-full rounded', tone)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-500 dark:text-gray-400">{count}/{cap}</span>
    </div>
  )
}

function ItemRow({ item }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5 border-t border-gray-100 dark:border-gray-700/60 text-sm">
      <Link
        to={`/tasks/${item.key}`}
        className="font-mono text-xs text-blue-600 dark:text-blue-400 hover:underline shrink-0"
      >
        {item.key}
      </Link>
      <span className="truncate flex-1 min-w-[10rem] text-gray-700 dark:text-gray-200">{item.title}</span>
      <span
        className={cn(
          'px-1.5 py-0.5 rounded text-xs shrink-0',
          item.waiting
            ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
            : 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
        )}
      >
        {item.status}
      </span>
      {item.blockers.map((b) => (
        <Link
          key={b.key}
          to={`/tasks/${b.key}`}
          className="inline-flex items-center gap-1 text-xs text-red-600 dark:text-red-400 hover:underline shrink-0"
          title={`Blocked by ${b.key} (${b.status})`}
        >
          <ShieldAlert className="w-3.5 h-3.5" />
          {b.key}
        </Link>
      ))}
      {item.pull_requests.map((pr) => (
        <a
          key={pr.url}
          href={pr.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-xs text-purple-600 dark:text-purple-400 hover:underline shrink-0"
          title={`PR ${pr.status}`}
        >
          <GitPullRequest className="w-3.5 h-3.5" />
          {pr.status}
        </a>
      ))}
      {item.next_transitions.length > 0 && (
        <span
          className="text-xs text-gray-400 dark:text-gray-500 shrink-0"
          title={`Available transitions: ${item.next_transitions.join(', ')}`}
        >
          next: {item.next_transitions.slice(0, 2).join(' · ')}
          {item.next_transitions.length > 2 ? ' …' : ''}
        </span>
      )}
      <Link
        to={`/tasks/${item.key}`}
        className="ml-auto inline-flex items-center gap-1 text-xs text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 shrink-0"
        title="Open task to assign / comment / transition (existing Pulse actions)"
      >
        <ExternalLink className="w-3.5 h-3.5" />
        open
      </Link>
    </div>
  )
}

function AgentCard({ agent }) {
  const [open, setOpen] = useState(agent.wip_count > 0 || agent.waiting_count > 0)
  const name = agent.email.split('@')[0]
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-3 text-left"
      >
        {open ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />}
        <Bot className="w-4 h-4 text-gray-400 shrink-0" />
        <span className="font-medium text-gray-900 dark:text-gray-100">{name}</span>
        <AvailabilityDot availability={agent.availability} stale={agent.stale} />
        {agent.stale && (
          <span
            className="inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400"
            title={`Holds WIP with no item activity in the stale window (last: ${agent.last_item_activity_at || 'never'})`}
          >
            <AlertTriangle className="w-3.5 h-3.5" />
            no recent activity
          </span>
        )}
        <span className="ml-auto flex items-center gap-3">
          {agent.waiting_count > 0 && (
            <span className="text-xs text-amber-600 dark:text-amber-400">{agent.waiting_count} waiting</span>
          )}
          <WipPressureBar count={agent.wip_count} cap={agent.wip_cap} />
        </span>
      </button>
      {open && (
        <div className="mt-2 pl-7">
          {agent.items.length === 0 ? (
            <p className="text-xs text-gray-400 dark:text-gray-500 py-1">No active items.</p>
          ) : (
            agent.items.map((item) => <ItemRow key={item.key} item={item} />)
          )}
          {agent.items_truncated > 0 && (
            <p className="text-xs text-gray-400 dark:text-gray-500 pt-1">
              +{agent.items_truncated} more —{' '}
              <Link className="text-blue-600 dark:text-blue-400 hover:underline" to={`/team?assignee=${agent.email}`}>
                view full queue
              </Link>
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export default function AgentConsole() {
  const [searchParams, setSearchParams] = useSearchParams()
  const team = searchParams.get('team') || ''

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['agent-activity', team],
    queryFn: () => tasksApi.agentActivity(team ? { team } : {}).then((r) => r.data),
    refetchInterval: REFRESH_MS,
  })

  if (isLoading) return <PageLoader />

  if (isError) {
    return (
      <div className="p-6">
        <div className="flex items-center gap-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 text-sm text-red-700 dark:text-red-300">
          <AlertCircle className="w-5 h-5 shrink-0" />
          <span>Failed to load agent activity{error?.message ? `: ${error.message}` : ''}.</span>
          <button
            type="button"
            onClick={() => refetch()}
            className="ml-auto inline-flex items-center gap-1 px-2 py-1 rounded border border-red-300 dark:border-red-700 hover:bg-red-100 dark:hover:bg-red-900/40"
          >
            <RefreshCw className="w-3.5 h-3.5" /> Retry
          </button>
        </div>
      </div>
    )
  }

  const teams = Object.keys(data?.teams || {}).sort()
  const agents = data?.agents || []
  const byTeam = teams.map((t) => ({
    team: t,
    agents: agents.filter((a) => a.teams.includes(t)),
  })).filter((g) => g.agents.length > 0)

  return (
    <div className="p-4 sm:p-6 space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Activity className="w-5 h-5 text-gray-400" />
        <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Agent Console</h1>
        <select
          value={team}
          onChange={(e) => setSearchParams(e.target.value ? { team: e.target.value } : {})}
          className="ml-auto text-sm rounded border-gray-300 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
          aria-label="Filter by team"
        >
          <option value="">All teams</option>
          {teams.map((t) => (
            <option key={t} value={t}>{formatTeamName(t)}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => refetch()}
          className="inline-flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400 hover:text-blue-600"
          title="Refresh"
        >
          <RefreshCw className={cn('w-4 h-4', isFetching && 'animate-spin')} />
        </button>
      </div>

      {(data?.lanes?.review?.length > 0 || data?.lanes?.merge?.length > 0) && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Review lane: {data.lanes.review.map((e) => e.split('@')[0]).join(', ') || '—'}
          {' · '}
          Merge lane: {data.lanes.merge.map((e) => e.split('@')[0]).join(', ') || '—'}
        </p>
      )}

      {byTeam.length === 0 ? (
        <EmptyState
          icon={Bot}
          title="No agents found"
          description="No teams match this filter, or the selected organization has no teams."
        />
      ) : (
        byTeam.map(({ team: t, agents: teamAgents }) => (
          <section key={t}>
            <h2 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2">
              {formatTeamName(t)}
              <span className="ml-2 text-xs text-gray-400">{teamAgents.length} agent(s)</span>
            </h2>
            <div className="space-y-2">
              {teamAgents.map((agent) => (
                <AgentCard key={`${t}:${agent.email}`} agent={agent} />
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  )
}
