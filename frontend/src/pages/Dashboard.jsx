import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  CheckSquare,
  Zap,
  AlertTriangle,
  Clock,
  TrendingUp,
  List,
  LayoutGrid,
  Circle,
  Loader2,
  CheckCircle2,
  ChevronDown,
  RefreshCw,
} from 'lucide-react'
import { itemsApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { useOrganization } from '../contexts/OrganizationContext'
import StatCard from '../components/StatCard'
import ItemCard from '../components/ItemCard'
import AlertDetailModal from '../components/AlertDetailModal'
import EmptyState from '../components/EmptyState'
import QuickCreateTask from '../components/QuickCreateTask'
import { cn } from '../utils/cn'
import { getEntityBadge, getEntityBadgeTitle } from '../utils/entityKey'
import { formatApiError } from '../utils/createTaskRequest'
import { moveTaskToColumn } from '../utils/moveTaskOnBoard'

// Kanban columns map to Jira-style status categories ('todo' / 'in_progress' / 'done'),
// NOT to literal status slugs. This is the only correct approach when the
// underlying schema has many workflow-specific slugs (`done`, `applied`,
// `awaiting_merge`, `in_review`, etc.) that all roll up to one of these three.
// Default-drop target slugs are used only for drag-and-drop: dragging a card
// to the "To Do" column PATCHes status=pending (the canonical todo slug), etc.
const KANBAN_COLUMNS = [
  { id: 'todo',        label: 'To Do',       dropSlug: 'pending',     icon: Circle,       color: 'text-gray-500', bg: 'bg-gray-100 dark:bg-gray-800/50' },
  { id: 'in_progress', label: 'In Progress', dropSlug: 'in_progress', icon: Loader2,      color: 'text-blue-500', bg: 'bg-blue-50 dark:bg-blue-900/20' },
  { id: 'done',        label: 'Done',        dropSlug: 'completed',   icon: CheckCircle2, color: 'text-green-500', bg: 'bg-green-50 dark:bg-green-900/20' },
]

const TASK_FILTERS = [
  { value: 'active', label: 'Active' },
  { value: 'all', label: 'All' },
  { value: 'completed', label: 'Completed' },
]

function getPriorityBorder(priority) {
  switch (priority) {
    case 'urgent': return 'border-l-red-500'
    case 'high': return 'border-l-orange-500'
    case 'normal': return 'border-l-blue-400'
    case 'low': return 'border-l-gray-300'
    default: return 'border-l-gray-300'
  }
}

function EntityBadgePill({ item }) {
  const entityBadge = getEntityBadge(item)
  if (!entityBadge) return null
  return (
    <span
      className="inline-flex h-5 min-w-[1.75rem] flex-shrink-0 items-center justify-center rounded border border-brand-200 bg-brand-50 px-1.5 text-[10px] font-bold leading-none text-brand-700 dark:border-brand-800 dark:bg-brand-950/60 dark:text-brand-300"
      title={getEntityBadgeTitle(item, entityBadge)}
    >
      {entityBadge.key}
    </span>
  )
}

function KanbanTaskCard({ item, isDragging, onDragStart, onDragEnd, onClick }) {
  return (
    <div
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onClick={onClick}
      className={cn(
        'bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-3',
        'border-l-4 cursor-pointer hover:shadow-md transition-shadow',
        getPriorityBorder(item.priority),
        isDragging && 'opacity-50'
      )}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="flex min-w-0 items-center gap-1.5">
          <EntityBadgePill item={item} />
          <span className="truncate text-[11px] font-medium text-gray-500 dark:text-gray-400">
            {item.item_key || `#${item.id}`}
          </span>
        </div>
        {item.assignee_email && (
          <div
            className="w-5 h-5 flex-shrink-0 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center"
            title={item.assignee_name || item.assignee_email}
          >
            <span className="text-[10px] text-brand-600 dark:text-brand-400 font-medium">
              {(item.assignee_name || item.assignee_email)[0].toUpperCase()}
            </span>
          </div>
        )}
      </div>
      <p className="text-sm text-gray-900 dark:text-white line-clamp-2">
        {item.title}
      </p>
      {item.project_key && (
        <span className="text-[10px] text-gray-400 mt-1 block">
          {item.project_key}
        </span>
      )}
    </div>
  )
}

export default function Dashboard() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { user } = useAuth()
  const { currentOrganization, isOrganizationResolved } = useOrganization()
  const scope = [user?.id, currentOrganization?.id ?? 'all']
  const orgParams = currentOrganization ? { organization: currentOrganization.id } : {}
  const refreshOptions = {
    enabled: !!user?.id && isOrganizationResolved,
    staleTime: 30000,
    refetchInterval: 30000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  }
  const visibleData = query => [401, 403].includes(query.error?.response?.status) ? undefined : query.data
  const [selectedAlert, setSelectedAlert] = useState(null)
  const [silenceModal, setSilenceModal] = useState(null)
  const [viewMode, setViewMode] = useState('list') // 'list' or 'kanban'
  const [taskFilter, setTaskFilter] = useState('active')
  const [taskPage, setTaskPage] = useState(1)

  // Fetch statistics
  const statsQuery = useQuery({
    ...refreshOptions,
    queryKey: ['statistics', ...scope],
    queryFn: ({ signal }) => itemsApi.getStatistics({ params: orgParams, signal }).then((res) => res.data),
  })

  // Fetch pending items (alerts + actions)
  const pendingQuery = useQuery({
    ...refreshOptions,
    queryKey: ['pending', ...scope],
    queryFn: ({ signal }) => itemsApi.getPending({ params: orgParams, signal }).then((res) => res.data),
  })

  // Fetch overdue items
  const overdueQuery = useQuery({
    ...refreshOptions,
    queryKey: ['overdue', ...scope],
    queryFn: ({ signal }) => itemsApi.getOverdue({ params: orgParams, signal }).then((res) => res.data),
  })

  // My Tasks — paginated, uses assigned_to_me to match both ID and email
  const taskQueryParams = useMemo(() => {
    const params = { ...(currentOrganization ? { organization: currentOrganization.id } : {}), page_size: 50, page: taskPage, mode: 'task', assigned_to_me: 'true' }
    // Active = non-terminal AND not a future-scheduled recurring task.
    // Recurring tasks with due_date >24h out (e.g. weekly scan just completed)
    // are hidden until they approach their next occurrence.
    if (taskFilter === 'active') params.is_active = 'true'
    else if (taskFilter === 'completed') params.status = 'completed'
    // 'all' — no status filter
    // Board view: cap Done column to last 14 days so it doesn't balloon
    // with historical items once the org has shipped hundreds of tasks.
    if (viewMode === 'kanban') {
      params.done_recent_days = '14'
    }
    return params
  }, [taskFilter, taskPage, viewMode, currentOrganization])

  const tasksQuery = useQuery({
    ...refreshOptions,
    queryKey: ['my-dashboard-tasks', ...scope, taskQueryParams],
    queryFn: ({ signal }) => itemsApi.list(taskQueryParams, { signal }).then((res) => res.data),
  })

  const stats = visibleData(statsQuery)
  const pending = visibleData(pendingQuery)
  const overdue = visibleData(overdueQuery)
  const myTasksData = visibleData(tasksQuery)
  const statsLoading = statsQuery.isLoading
  const pendingLoading = pendingQuery.isLoading
  const overdueLoading = overdueQuery.isLoading
  const tasksLoading = tasksQuery.isLoading
  const queries = [statsQuery, pendingQuery, overdueQuery, tasksQuery]
  const updatesFailed = queries.some(query => query.isError)
  const refreshing = queries.some(query => query.isFetching)
  const updatedAt = Math.min(...queries.map(query => query.dataUpdatedAt).filter(Boolean))
  const refresh = () => queries.forEach(query => query.refetch({ cancelRefetch: false }))
  const overdueCount = overdue?.count ?? overdue?.results?.length ?? overdue?.items?.length ?? (Array.isArray(overdue) ? overdue.length : undefined)
  const myTasks = myTasksData?.results || []
  const myTasksCount = myTasksData?.count
  const hasMoreTasks = !!myTasksData?.next

  // Helper to invalidate all dashboard queries
  const invalidateDashboardQueries = () => {
    queryClient.invalidateQueries({ queryKey: ['pending'] })
    queryClient.invalidateQueries({ queryKey: ['statistics'] })
    queryClient.invalidateQueries({ queryKey: ['overdue'] })
    queryClient.invalidateQueries({ queryKey: ['my-dashboard-tasks'] })
  }

  // Mutations
  const completeMutation = useMutation({
    mutationFn: (item) => item.status === 'completed'
      ? itemsApi.reopen(item.id)
      : itemsApi.complete(item.id),
    onSuccess: invalidateDashboardQueries,
  })

  const approveMutation = useMutation({
    mutationFn: (item) => itemsApi.approve(item.id),
    onSuccess: invalidateDashboardQueries,
  })

  const acknowledgeMutation = useMutation({
    mutationFn: (item) => itemsApi.acknowledge(item.id),
    onSuccess: invalidateDashboardQueries,
  })

  const resolveMutation = useMutation({
    mutationFn: (item) => itemsApi.resolve(item.id),
    onSuccess: invalidateDashboardQueries,
  })

  // Drag and drop for kanban
  const [draggedItem, setDraggedItem] = useState(null)
  const [boardMoveError, setBoardMoveError] = useState('')

  const handleDragStart = (e, item) => {
    setDraggedItem(item)
    e.dataTransfer.effectAllowed = 'move'
  }
  const handleDragOver = (e) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
  }
  const handleDrop = async (e, columnId) => {
    e.preventDefault()
    const item = draggedItem
    setDraggedItem(null)
    if (!item) return
    setBoardMoveError('')
    try {
      const result = await moveTaskToColumn(item, columnId, itemsApi)
      if (result.moved) invalidateDashboardQueries()
    } catch (err) {
      setBoardMoveError(formatApiError(err, 'This task could not move.'))
    }
  }

  // Group tasks by status_category for kanban view. Uses the serializer's
  // `status_category` field (resolved from the Status table) so every workflow
  // slug lands in the right column regardless of naming: done/completed/closed
  // all go to Done; awaiting_merge/in_review/in_progress all go to In Progress;
  // pending/open/todo/needs_triage all go to To Do.
  // If `status_category` is unexpectedly missing (task has no Status row), we
  // derive a category from the slug via a small hardcoded map as a safety net,
  // NOT via a "dump everything into To Do" fallback (which is what caused
  // Done tasks to appear in the TODO column).
  const tasksByStatus = useMemo(() => {
    const grouped = {}
    KANBAN_COLUMNS.forEach(col => { grouped[col.id] = [] })
    const SLUG_TO_CATEGORY = {
      completed: 'done', done: 'done', closed: 'done', cancelled: 'done',
      canceled: 'done', resolved: 'done', rejected: 'done', duplicate: 'done',
      wont_fix: 'done', merged: 'done', applied: 'done', risk_accepted: 'done',
      change_rejected: 'done',
      in_progress: 'in_progress', in_review: 'in_progress', awaiting_merge: 'in_progress',
      approved: 'in_progress', applying: 'in_progress', blocked: 'in_progress',
      design: 'in_progress', in_qa: 'in_progress', under_review: 'in_progress',
      verification: 'in_progress', escalated: 'in_progress', awaiting_triage: 'in_progress',
      pending: 'todo', open: 'todo', todo: 'todo', backlog: 'todo', proposed: 'todo',
      found: 'todo', needs_triage: 'todo', reopened: 'todo', apply_failed: 'todo',
      // Scheduled maps to its own category; the Dashboard doesn't render
      // a Scheduled column by default (it's a gap state, not active work),
      // so these tasks naturally drop from the board via the unmatched
      // category path below.
      scheduled: 'scheduled',
    }
    myTasks.forEach(task => {
      const cat = task.status_category || SLUG_TO_CATEGORY[task.status]
      if (cat && grouped[cat]) {
        grouped[cat].push(task)
      }
      // Tasks with an unmappable status are dropped from the board rather
      // than silently pooled into the wrong column — the old behaviour hid
      // the real state of the task.
    })
    return grouped
  }, [myTasks])

  // Async-first: render the dashboard shell immediately, then hydrate
  // secondary stats/pending data in-place so slow summary calls do not
  // block first useful render of the user's task list.
  const pendingActions = pending?.actions || []
  const firingAlerts = pending?.alerts || []

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Dashboard</h1>
        <p className="text-gray-500 dark:text-gray-400">Overview of your tasks, actions, and alerts</p>
      </div>

      <div className="flex items-center justify-between gap-3 text-sm">
        <p role="status" className={updatesFailed ? 'text-amber-600 dark:text-amber-400' : 'text-gray-500'}>
          {updatesFailed
            ? (stats || pending || overdue || myTasksData ? 'Updates unavailable; showing the last successful observation where available.' : 'Dashboard metrics unavailable.')
            : Number.isFinite(updatedAt) ? `Updated ${new Date(updatedAt).toLocaleTimeString()}` : 'Loading dashboard metrics…'}
        </p>
        <button type="button" onClick={refresh} disabled={refreshing || !user?.id || !isOrganizationResolved} className="btn-secondary inline-flex items-center gap-1.5 disabled:opacity-50">
          <RefreshCw size={16} className={refreshing ? 'animate-spin' : ''} />
          {updatesFailed ? 'Retry' : 'Refresh'}
        </button>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          title="Pending Tasks"
          value={stats?.tasks?.pending ?? '—'}
          icon={CheckSquare}
          color="indigo"
          subtitle={statsLoading ? 'Loading…' : statsQuery.isError ? (stats ? 'Last observation' : 'Unavailable') : undefined}
        />
        <StatCard
          title="Pending Actions"
          value={stats?.actions?.pending ?? '—'}
          icon={Zap}
          color="yellow"
          subtitle={statsLoading ? 'Loading…' : statsQuery.isError ? (stats ? 'Last observation' : 'Unavailable') : undefined}
        />
        <StatCard
          title="Firing Alerts"
          value={stats?.alerts?.firing ?? '—'}
          icon={AlertTriangle}
          color="red"
          subtitle={statsLoading ? 'Loading…' : statsQuery.isError ? (stats ? 'Last observation' : 'Unavailable') : undefined}
        />
        <StatCard
          title="Overdue"
          value={overdueCount ?? '—'}
          icon={Clock}
          color="orange"
          subtitle={overdueLoading ? 'Loading…' : overdueQuery.isError ? (overdue ? 'Last observation' : 'Unavailable') : undefined}
        />
      </div>

      {/* Pending summary hydration */}
      {pendingLoading && (
        <section className="card p-4">
          <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
            <Loader2 className="animate-spin" size={16} />
            Checking actions and alerts…
          </div>
        </section>
      )}

      {/* Firing Alerts Section */}
      {firingAlerts.length > 0 && (
        <section>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
              <AlertTriangle className="text-red-500" size={20} />
              Firing Alerts
              <span className="badge badge-critical">{firingAlerts.length}</span>
            </h2>
          </div>
          <div className="space-y-3">
            {firingAlerts.map((item) => (
              <ItemCard
                key={item.id}
                item={item}
                onClick={(item) => setSelectedAlert(item)}
                onAcknowledge={(item) => acknowledgeMutation.mutate(item)}
                onResolve={(item) => resolveMutation.mutate(item)}
              />
            ))}
          </div>
        </section>
      )}

      {/* Pending Actions Section */}
      {pendingActions.length > 0 && (
        <section>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
              <Zap className="text-amber-500" size={20} />
              Pending Actions
              <span className="badge badge-warning">{pendingActions.length}</span>
            </h2>
          </div>
          <div className="space-y-3">
            {pendingActions.map((item) => (
              <ItemCard
                key={item.id}
                item={item}
                onApprove={(item) => approveMutation.mutate(item)}
                navigateOnClick
              />
            ))}
          </div>
        </section>
      )}

      {/* My Tasks Section */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <CheckSquare className="text-brand-500" size={20} />
            My Tasks
            <span className="text-sm font-normal text-gray-500 dark:text-gray-400">
              {myTasksCount ?? '—'}
            </span>
          </h2>

          <div className="flex items-center gap-2">
            {/* Filter */}
            <select
              value={taskFilter}
              onChange={(e) => { setTaskFilter(e.target.value); setTaskPage(1) }}
              className="input text-sm py-1 w-auto"
            >
              {TASK_FILTERS.map(f => (
                <option key={f.value} value={f.value}>{f.label}</option>
              ))}
            </select>

            {/* View toggle — hidden on phones because the kanban view (288px-wide
                columns) can't render meaningfully on a narrow screen and the
                28px tap targets were easy to mis-tap on mobile WebViews. */}
            <div className="hidden sm:flex items-center bg-gray-100 dark:bg-gray-800 rounded-lg p-0.5">
              <button
                onClick={() => setViewMode('list')}
                className={cn(
                  'p-2 rounded-md transition-colors min-w-[40px] min-h-[40px] flex items-center justify-center',
                  viewMode === 'list'
                    ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white'
                    : 'text-gray-500 hover:text-gray-700 dark:text-gray-400'
                )}
                title="List view"
              >
                <List size={16} />
              </button>
              <button
                onClick={() => { setViewMode('kanban') }}
                className={cn(
                  'p-2 rounded-md transition-colors min-w-[40px] min-h-[40px] flex items-center justify-center',
                  viewMode === 'kanban'
                    ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white'
                    : 'text-gray-500 hover:text-gray-700 dark:text-gray-400'
                )}
                title="Board view"
              >
                <LayoutGrid size={16} />
              </button>
            </div>
          </div>
        </div>

        {boardMoveError && (
          <p role="alert" className="mb-3 text-sm text-red-600 dark:text-red-400">
            {boardMoveError}
          </p>
        )}

        {tasksLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="animate-spin text-gray-400" size={24} />
          </div>
        ) : !myTasksData ? (
          <p role="status" className="py-8 text-sm text-gray-500">Task list unavailable. Retry to load your tasks.</p>
        ) : myTasks.length === 0 ? (
          <EmptyState
            icon={CheckSquare}
            title="No tasks"
            description={taskFilter === 'active' ? 'No active tasks assigned to you.' : 'No tasks found.'}
          />
        ) : viewMode === 'list' ? (
          /* List View */
          <div>
            <div className="space-y-2">
              {myTasks.map((item) => (
                <ItemCard
                  key={item.id}
                  item={item}
                  onComplete={(item) => completeMutation.mutate(item)}
                  navigateOnClick
                />
              ))}
            </div>

            {/* Pagination */}
            {(hasMoreTasks || taskPage > 1) && (
              <div className="flex items-center justify-center gap-3 mt-4">
                {taskPage > 1 && (
                  <button
                    onClick={() => setTaskPage(p => p - 1)}
                    className="btn-secondary btn-sm"
                  >
                    Previous
                  </button>
                )}
                <span className="text-sm text-gray-500 dark:text-gray-400">
                  Page {taskPage}
                </span>
                {hasMoreTasks && (
                  <button
                    onClick={() => setTaskPage(p => p + 1)}
                    className="btn-secondary btn-sm"
                  >
                    Next
                  </button>
                )}
              </div>
            )}
          </div>
        ) : (
          /* Kanban View */
          <div className="overflow-x-auto -mx-4 px-4">
            <div className="flex gap-4 min-w-max">
              {KANBAN_COLUMNS.map((column) => {
                const columnItems = tasksByStatus[column.id] || []
                const Icon = column.icon

                return (
                  <div
                    key={column.id}
                    className={cn('w-72 flex flex-col rounded-lg min-h-[200px]', column.bg)}
                    onDragOver={handleDragOver}
                    onDrop={(e) => handleDrop(e, column.id)}
                  >
                    {/* Column Header */}
                    <div className="flex items-center gap-2 p-3 border-b border-gray-200/50 dark:border-gray-700/50">
                      <Icon size={16} className={column.color} />
                      <span className="font-medium text-sm text-gray-900 dark:text-white">
                        {column.label}
                      </span>
                      <span className="text-xs text-gray-500 dark:text-gray-400 bg-white/60 dark:bg-gray-700/60 px-1.5 py-0.5 rounded-full">
                        {columnItems.length}
                      </span>
                    </div>

                    {/* Column Cards */}
                    <div className="flex-1 overflow-y-auto p-2 space-y-2">
                      {columnItems.map((item) => (
                        <KanbanTaskCard
                          key={item.id}
                          item={item}
                          isDragging={draggedItem?.id === item.id}
                          onDragStart={(e) => handleDragStart(e, item)}
                          onDragEnd={() => setDraggedItem(null)}
                          onClick={() => navigate(`/tasks/${item.item_key || item.id}`)}
                        />
                      ))}
                      {columnItems.length === 0 && (
                        <p className="text-xs text-gray-400 dark:text-gray-500 text-center py-6">
                          No tasks
                        </p>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </section>

      {/* Empty state — only when nothing at all */}
      {pending && myTasksData && stats && !updatesFailed &&
        stats.tasks?.pending === 0 && stats.actions?.pending === 0 && stats.alerts?.firing === 0 && !pendingLoading &&
        !tasksLoading &&
        !statsLoading &&
        firingAlerts.length === 0 &&
        pendingActions.length === 0 &&
        myTasksCount === 0 && (
          <EmptyState
            icon={TrendingUp}
            title="All caught up!"
            description="No pending tasks, actions, or alerts right now."
          />
        )}

      {/* Alert Detail Modal */}
      {selectedAlert && (
        <AlertDetailModal
          alert={selectedAlert}
          onClose={() => setSelectedAlert(null)}
          onAcknowledge={
            selectedAlert.status === 'firing'
              ? (item) => acknowledgeMutation.mutate(item)
              : undefined
          }
          onResolve={
            selectedAlert.status === 'firing' || selectedAlert.status === 'acknowledged'
              ? (item) => resolveMutation.mutate(item)
              : undefined
          }
          onSilence={
            selectedAlert.status === 'firing'
              ? (item) => setSilenceModal(item)
              : undefined
          }
        />
      )}

      {/* Floating quick-create button */}
      <QuickCreateTask />
    </div>
  )
}
