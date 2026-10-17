import { useState, useMemo, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Plus,
  CheckSquare,
  Calendar,
  Bell,
  Flag,
  Repeat,
  Tag,
  Clock,
  ChevronDown,
  ChevronUp,
  Search,
  MousePointerClick,
  HelpCircle,
  X,
  FolderKanban,
  User,
  Code,
  List,
  LayoutGrid,
  Circle,
  Loader2 as Loader2Icon,
  CheckCircle2,
  Download,
  Upload,
  FileText,
  Package,
  AlertTriangle,
} from 'lucide-react'
import { tasksApi, itemsApi, statusesApi } from '../services/api'
import JQLInput from '../components/JQLInput'
import { useOrganization } from '../contexts/OrganizationContext'
import { useAuth } from '../contexts/AuthContext'
import ItemCard from '../components/ItemCard'
import EmptyState from '../components/EmptyState'
import { PageLoader } from '../components/LoadingSpinner'
import SelectionToolbar from '../components/SelectionToolbar'
import { useMultiSelect } from '../hooks/useMultiSelect'
import SavedFilters from '../components/SavedFilters'
import MultiSelectFilter from '../components/MultiSelectFilter'
import { cn } from '../utils/cn'
import { DEFAULT_TASK_SORT, compareTasksBySort } from '../utils/taskSort'
import { formatOrganizationName } from '../utils/teamLabels'
import { formatApiError, organizationIdForNewTask } from '../utils/createTaskRequest'
import { moveTaskToColumn } from '../utils/moveTaskOnBoard'

const STATUS_FILTERS = [
  { value: '__active__', label: 'Active' },
  { value: '', label: 'All' },
  { value: 'pending', label: 'Pending' },
  { value: 'in_progress', label: 'In Progress' },
  { value: 'completed', label: 'Completed' },
]

const PRIORITY_FILTERS = [
  { value: '', label: 'Any Priority' },
  { value: 'urgent', label: 'Urgent' },
  { value: 'high', label: 'High' },
  { value: 'normal', label: 'Normal' },
  { value: 'low', label: 'Low' },
]

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low' },
  { value: 'normal', label: 'Normal' },
  { value: 'high', label: 'High' },
  { value: 'urgent', label: 'Urgent' },
]

const FREQUENCY_OPTIONS = [
  { value: 'one_time', label: 'Does not repeat' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'yearly', label: 'Yearly' },
]

const SORT_OPTIONS = [
  { value: 'created_at-desc', label: 'Newest first' },
  { value: 'created_at-asc', label: 'Oldest first' },
  { value: 'due_date-asc', label: 'Due date (soonest)' },
  { value: 'due_date-desc', label: 'Due date (latest)' },
  { value: 'priority-desc', label: 'Priority (highest)' },
  { value: 'priority-asc', label: 'Priority (lowest)' },
  { value: 'title-asc', label: 'Title (A-Z)' },
  { value: 'title-desc', label: 'Title (Z-A)' },
]

const initialTaskState = {
  title: '',
  description: '',
  priority: 'normal',
  due_date: '',
  reminder_time: '',
  is_recurring: false,
  frequency: 'one_time',
  tags: '',
  estimated_hours: '',
  project: '',
  assignee_id: '',
  assignee_email: '',
  direct_assignment_reason: '',
}

export default function Tasks() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const {
    organizations,
    projects,
    currentOrganization,
    switchOrganization,
    refreshOrganizations,
    refreshProjects,
    isOrganizationResolved,
    organizationResolutionError,
  } = useOrganization()
  const { user } = useAuth()
  const organizationById = useMemo(
    () => new Map(organizations.map(organization => [String(organization.id), organization])),
    [organizations]
  )
  const organizationLabelForItem = (item) => {
    const organization = organizationById.get(String(item.organization_id))
    return formatOrganizationName({
      organization_name: item.organization_name || organization?.name,
      organization_slug: item.organization_slug || organization?.slug,
    })
  }

  // Filter & sort state. All multi-select arrays.
  //
  //  progressFilter  — category buckets: 'todo' / 'in_progress' / 'done'.
  //                    Primary day-to-day filter (Jira "Status Category" pattern).
  //                    Empty = "Open" default (is_actionable=true, every
  //                    Done-category status hidden) — the manager's glance view,
  //                    matching GitHub/Jira's "Open" convention.
  //  statusFilter    — specific workflow status slugs (awaiting_merge, in_review, etc.).
  //                    Precision drill-down. ANDed with progressFilter when both set.
  //  priorityFilter  — urgent / high / normal / low. Empty = any.
  //
  // This matches the industry convention (Jira / Linear / ClickUp all expose
  // category and specific status as peer filters, not a hierarchy).
  const [progressFilter, setProgressFilter] = useState([])
  const [statusFilter, setStatusFilter] = useState([])
  const [priorityFilter, setPriorityFilter] = useState([])
  const [projectFilter, setProjectFilter] = useState([])
  const [assigneeFilter, setAssigneeFilter] = useState([])
  const [reporterFilter, setReporterFilter] = useState([])
  const [searchQuery, setSearchQuery] = useState('')
  const [sortOption, setSortOption] = useState(DEFAULT_TASK_SORT)

  // JQL mode
  const [jqlMode, setJqlMode] = useState(false)
  const [jqlQuery, setJqlQuery] = useState('')
  const [activeJql, setActiveJql] = useState('')

  // UI state
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [newTask, setNewTask] = useState(initialTaskState)
  const [createOrgId, setCreateOrgId] = useState('')
  const [createError, setCreateError] = useState('')
  const [boardMoveError, setBoardMoveError] = useState('')
  const [selectionMode, setSelectionMode] = useState(false)
  const [focusedIndex, setFocusedIndex] = useState(-1)
  const [showKeyboardHelp, setShowKeyboardHelp] = useState(false)
  // Default to 'kanban' (board) — managers glance at a Kanban columns view
  // more naturally than a flat list. The user's choice persists via
  // localStorage so we only default on first visit.
  const [viewMode, setViewMode] = useState(() => {
    try {
      const saved = localStorage.getItem('pulse.tasks.viewMode')
      if (saved === 'list' || saved === 'kanban') return saved
    } catch { /* localStorage may be unavailable */ }
    return 'kanban'
  })
  useEffect(() => {
    try { localStorage.setItem('pulse.tasks.viewMode', viewMode) } catch { /* ignore */ }
  }, [viewMode])
  const [showExportMenu, setShowExportMenu] = useState(false)
  const [showImportModal, setShowImportModal] = useState(false)
  const [importFile, setImportFile] = useState(null)
  const [importResult, setImportResult] = useState(null)
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState(null)

  const taskListRef = useRef(null)

  // Pagination state
  const [page, setPage] = useState(1)
  const [allTasks, setAllTasks] = useState([])
  const taskScopeKeyRef = useRef(null)
  const [hasMore, setHasMore] = useState(true)
  const [isLoadingMore, setIsLoadingMore] = useState(false)

  // Build query params
  const queryParams = useMemo(() => {
    const params = { page_size: 100 } // Request larger page size
    if (currentOrganization?.id) {
      params.organization = currentOrganization.id
    }
    if (jqlMode && activeJql) {
      params.jql = activeJql
    } else {
      // Progress (category) + Status (specific) are peer filters, ANDed when both set.
      // Default (both empty) = "Open" — hide every Done-category status via
      // is_actionable=true. Manager sees remaining work at a glance.
      if (progressFilter && progressFilter.length > 0) {
        params.status_category = progressFilter.join(',')
      }
      if (statusFilter && statusFilter.length > 0) {
        params.status = statusFilter.join(',')
      }
      if ((!progressFilter || progressFilter.length === 0) &&
          (!statusFilter || statusFilter.length === 0)) {
        // Neither progress nor status picked → default active view (exclude terminal)
        params.is_active = 'true'
      }
      // Priority: empty = all, otherwise comma-separated list.
      if (priorityFilter && priorityFilter.length > 0) {
        params.priority = priorityFilter.join(',')
      }
      // Project: empty = all accessible projects + personal items, otherwise
      // comma-separated project IDs. Backend resolves via id__in.
      if (projectFilter && projectFilter.length > 0) {
        params.project = projectFilter.join(',')
      }
      // Board view: cap Done column to recent (last 14 days, Jira default).
      // List view shows everything if user filters for Done explicitly.
      if (viewMode === 'kanban') {
        params.done_recent_days = '14'
      }
      if (assigneeFilter && assigneeFilter.length > 0) {
        const ids = assigneeFilter.filter(v => !v.startsWith('email:'))
        const emails = assigneeFilter.filter(v => v.startsWith('email:')).map(v => v.slice(6))
        if (ids.length) params.assignee_id = ids.join(',')
        if (emails.length) params.assignee_email = emails.join(',')
      }
      if (reporterFilter && reporterFilter.length > 0) {
        const ids = reporterFilter.filter(v => !v.startsWith('email:'))
        const emails = reporterFilter.filter(v => v.startsWith('email:')).map(v => v.slice(6))
        if (ids.length) params.reporter_id = ids.join(',')
        if (emails.length) params.reporter_email = emails.join(',')
      }
    }
    return params
  }, [jqlMode, activeJql, progressFilter, statusFilter, priorityFilter, projectFilter, assigneeFilter, reporterFilter, viewMode, currentOrganization?.id])

  // Keep the accumulated rows bound to the exact organization/filter scope
  // that produced them. The ref changes together with the row state after a
  // response, while a scope switch changes `queryScopeKey` during render. That
  // makes prior-scope rows ineligible immediately, before an effect or the new
  // network response can run.
  const queryScopeKey = isOrganizationResolved
    ? JSON.stringify(queryParams)
    : '__organization_pending__'

  // Fetch the full Status catalog once per session for the status filter.
  // Cached forever (staleTime: Infinity) — statuses rarely change.
  const { data: statusCatalog = [] } = useQuery({
    queryKey: ['statuses'],
    queryFn: () => statusesApi.list().then(r => r.data?.results || r.data || []),
    staleTime: Infinity,
  })

  // Status options for the multi-select, grouped visually by category.
  // Category order: in_progress → todo → done (what's "active" first, done last).
  const statusFilterOptions = useMemo(() => {
    const order = { in_progress: 0, todo: 1, done: 2, scheduled: 3 }
    const sorted = [...statusCatalog].sort((a, b) => {
      const ca = order[a.category] ?? 99
      const cb = order[b.category] ?? 99
      if (ca !== cb) return ca - cb
      return (a.position ?? 0) - (b.position ?? 0)
    })
    return sorted.map(s => ({
      value: s.slug,
      label: s.name,
      sublabel: s.category === 'in_progress' ? 'In progress'
        : s.category === 'todo' ? 'To Do'
        : s.category === 'done' ? 'Done'
        : s.category === 'scheduled' ? 'Scheduled (gap state)'
        : s.category,
    }))
  }, [statusCatalog])

  const priorityFilterOptions = useMemo(() => ([
    { value: 'urgent', label: 'Urgent' },
    { value: 'high',   label: 'High' },
    { value: 'normal', label: 'Normal' },
    { value: 'low',    label: 'Low' },
  ]), [])

  // Fetch tasks with pagination support
  const { data, isLoading, error: fetchError, isFetching } = useQuery({
    queryKey: ['tasks', queryParams, page],
    queryFn: () => tasksApi.list({ ...queryParams, page }).then((res) => res.data),
    enabled: isOrganizationResolved,
  })

  // Handle paginated data accumulation
  useEffect(() => {
    if (data) {
      const results = data.results || data
      setAllTasks(prev => (
        page === 1 || taskScopeKeyRef.current !== queryScopeKey
          ? results
          : [...prev, ...results]
      ))
      taskScopeKeyRef.current = queryScopeKey
      // Check if there's more data
      setHasMore(data.next !== null && data.next !== undefined)
      setIsLoadingMore(false)
    }
  }, [data, page, queryScopeKey])

  // Reset pagination when filters change. Rendering is already fail-closed by
  // `queryScopeKey`; retaining the hidden rows until the replacement response
  // also lets React Query satisfy a revisited scope from cache without a blank
  // overwrite race between these effects.
  useEffect(() => {
    setPage(1)
    setHasMore(false) // Will be set correctly when new data arrives
  }, [queryScopeKey])

  // Handle JQL search
  const handleJqlSearch = (jql) => {
    setActiveJql(jql)
    setPage(1)
    setAllTasks([])
  }

  // Toggle between JQL and standard filter modes; clear the other mode's state.
  const toggleJqlMode = () => {
    setJqlMode((prev) => {
      const next = !prev
      if (next) {
        // Entering JQL mode → clear standard filters
        setStatusFilter([])
        setPriorityFilter([])
        setProjectFilter([])
        setAssigneeFilter([])
        setReporterFilter([])
      } else {
        // Returning to standard mode → clear JQL
        setJqlQuery('')
        setActiveJql('')
      }
      return next
    })
  }

  // Load more handler
  const loadMore = () => {
    if (hasMore && !isFetching && !isLoadingMore) {
      setIsLoadingMore(true)
      setPage(prev => prev + 1)
    }
  }

  // Fetch assignable users for filter dropdown
  const { data: assignableUsersData } = useQuery({
    queryKey: ['assignableUsers', currentOrganization?.id],
    queryFn: () => tasksApi.assignableUsers({
      organization: currentOrganization?.id
    }).then((res) => res.data),
    enabled: isOrganizationResolved,
  })

  const assignableUsers = useMemo(() => {
    const users = assignableUsersData?.users || []
    // Deduplicate by id (preferred) or email (fallback for email-only assignments)
    const seen = new Set()
    return users.filter(u => {
      const key = u.user_id ? `id:${u.user_id}` : (u.email ? `email:${u.email}` : null)
      if (!key || seen.has(key)) return false
      seen.add(key)
      return true
    })
  }, [assignableUsersData])

  // Use accumulated tasks from pagination
  const rawTasks = (
    isOrganizationResolved && taskScopeKeyRef.current === queryScopeKey
      ? allTasks
      : []
  )

  // Filter by search query (client-side)
  const filteredTasks = useMemo(() => {
    if (!searchQuery.trim()) return rawTasks

    const query = searchQuery.toLowerCase()
    return rawTasks.filter(
      (task) =>
        task.title.toLowerCase().includes(query) ||
        task.description?.toLowerCase().includes(query) ||
        task.tags?.toLowerCase().includes(query)
    )
  }, [rawTasks, searchQuery])

  // Sort tasks (client-side)
  const sortedTasks = useMemo(() => {
    return [...filteredTasks].sort(compareTasksBySort(sortOption))
  }, [filteredTasks, sortOption])

  // Multi-select hook
  const {
    toggle,
    toggleWithShift,
    selectAll,
    deselectAll,
    isSelected,
    selectedIds,
    selectedCount,
    allSelected,
    someSelected,
  } = useMultiSelect(sortedTasks)

  // Reset pagination and refetch helper
  const resetAndRefetch = () => {
    setPage(1)
    setAllTasks([])
    setHasMore(false) // Will be set correctly when new data arrives
    queryClient.invalidateQueries({ queryKey: ['tasks'] })
    queryClient.invalidateQueries({ queryKey: ['statistics'] })
  }

  // === Export/Import handlers ===
  // Trigger a browser download of the exported file.
  const triggerDownload = (blob, filename) => {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  const handleExport = async (format) => {
    // Send the current filter params so the export matches what the user sees.
    try {
      const res = format === 'csv'
        ? await itemsApi.exportCsv(queryParams)
        : await itemsApi.exportBundle(queryParams)
      const ext = format === 'csv' ? 'csv' : 'zip'
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
      triggerDownload(res.data, `pulse-tasks-${stamp}.${ext}`)
    } catch (err) {
      alert(`Export failed: ${err.message}`)
    } finally {
      setShowExportMenu(false)
    }
  }

  const handleImport = async () => {
    if (!importFile) return
    setImporting(true)
    setImportError(null)
    setImportResult(null)
    try {
      const fd = new FormData()
      fd.append('file', importFile)
      const isCSV = importFile.name.toLowerCase().endsWith('.csv')
      const res = isCSV ? await itemsApi.importCsv(fd) : await itemsApi.importBundle(fd)
      setImportResult(res.data)
      resetAndRefetch()
    } catch (err) {
      setImportError(err.response?.data?.error || err.message || 'Import failed')
    } finally {
      setImporting(false)
    }
  }

  // Toggle complete/reopen mutation (single)
  const completeMutation = useMutation({
    mutationFn: (item) => item.status === 'completed'
      ? itemsApi.reopen(item.id)
      : itemsApi.complete(item.id),
    onSuccess: () => {
      resetAndRefetch()
    },
  })

  // Create mutation
  const createMutation = useMutation({
    mutationFn: ({ organizationId, task }) => tasksApi.create(task, organizationId),
    onSuccess: () => {
      resetAndRefetch()
      refreshProjects()
      setShowCreateModal(false)
      setNewTask(initialTaskState)
      setShowAdvanced(false)
      setCreateError('')
    },
    onError: (err) => {
      setCreateError(formatApiError(err, 'The task could not be created.'))
    },
  })

  // Bulk complete mutation
  const bulkCompleteMutation = useMutation({
    mutationFn: (ids) => tasksApi.bulkComplete(ids),
    onSuccess: () => {
      resetAndRefetch()
      deselectAll()
    },
  })

  // Bulk update mutation
  const bulkUpdateMutation = useMutation({
    mutationFn: ({ ids, ...updates }) => tasksApi.bulkUpdate(ids, updates),
    onSuccess: () => {
      resetAndRefetch()
      deselectAll()
    },
  })

  // Bulk delete mutation
  const bulkDeleteMutation = useMutation({
    mutationFn: (ids) => tasksApi.bulkDelete(ids),
    onSuccess: () => {
      resetAndRefetch()
      deselectAll()
    },
  })

  const isAnyBulkLoading =
    bulkCompleteMutation.isPending ||
    bulkUpdateMutation.isPending ||
    bulkDeleteMutation.isPending

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e) => {
      // Skip if in input/textarea
      if (
        e.target.tagName === 'INPUT' ||
        e.target.tagName === 'TEXTAREA' ||
        e.target.isContentEditable
      ) {
        return
      }

      // Skip if modal is open
      if (showCreateModal || showKeyboardHelp) {
        if (e.key === 'Escape') {
          setShowCreateModal(false)
          setShowKeyboardHelp(false)
        }
        return
      }

      switch (e.key.toLowerCase()) {
        case 'j': // Next task
          e.preventDefault()
          setFocusedIndex((prev) =>
            prev < sortedTasks.length - 1 ? prev + 1 : prev
          )
          break
        case 'k': // Previous task
          e.preventDefault()
          setFocusedIndex((prev) => (prev > 0 ? prev - 1 : 0))
          break
        case 'x': // Toggle selection
          e.preventDefault()
          if (focusedIndex >= 0 && focusedIndex < sortedTasks.length) {
            if (!selectionMode) setSelectionMode(true)
            toggle(sortedTasks[focusedIndex].id)
          }
          break
        case 'c': // Complete focused/selected
          e.preventDefault()
          if (selectedCount > 0) {
            bulkCompleteMutation.mutate(selectedIds)
          } else if (focusedIndex >= 0 && focusedIndex < sortedTasks.length) {
            const task = sortedTasks[focusedIndex]
            if (task.status !== 'completed') {
              completeMutation.mutate(task)
            }
          }
          break
        case 'n': // New task
          e.preventDefault()
          openCreateModal()
          break
        case 's': // Toggle selection mode
          e.preventDefault()
          setSelectionMode((prev) => !prev)
          if (selectionMode) deselectAll()
          break
        case 'a': // Select all (with Ctrl/Cmd)
          if (e.ctrlKey || e.metaKey) {
            e.preventDefault()
            if (!selectionMode) setSelectionMode(true)
            selectAll()
          }
          break
        case 'escape':
          e.preventDefault()
          if (selectedCount > 0) {
            deselectAll()
          } else if (selectionMode) {
            setSelectionMode(false)
          } else {
            setFocusedIndex(-1)
          }
          break
        case 'enter':
          e.preventDefault()
          if (focusedIndex >= 0 && focusedIndex < sortedTasks.length) {
            if (selectionMode) {
              toggle(sortedTasks[focusedIndex].id)
            } else {
              // Navigate to task detail page
              const task = sortedTasks[focusedIndex]
              const taskKey = task.item_key || task.id
              navigate(`/tasks/${taskKey}`)
            }
          }
          break
        case '?':
          e.preventDefault()
          setShowKeyboardHelp(true)
          break
        default:
          break
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [
    sortedTasks,
    focusedIndex,
    selectionMode,
    selectedCount,
    selectedIds,
    showCreateModal,
    showKeyboardHelp,
    toggle,
    selectAll,
    deselectAll,
    completeMutation,
    bulkCompleteMutation,
    navigate,
  ])

  // Reset focus when tasks change
  useEffect(() => {
    setFocusedIndex(-1)
  }, [progressFilter, statusFilter, priorityFilter, assigneeFilter, reporterFilter, searchQuery, sortOption])

  // Clear selection when leaving selection mode
  useEffect(() => {
    if (!selectionMode) {
      deselectAll()
    }
  }, [selectionMode, deselectAll])

  const handleCreateTask = (e) => {
    e.preventDefault()
    if (!newTask.title.trim()) return
    setCreateError('')

    const organizationId = createOrgId || organizationIdForNewTask(currentOrganization, organizations)
    if (!organizationId) {
      setCreateError('Choose a workspace first. A task has to belong to one.')
      return
    }

    const assigningSomeoneElse = Boolean(
      newTask.assignee_id && user?.id && String(newTask.assignee_id) !== String(user.id)
    )
    if (assigningSomeoneElse && !newTask.direct_assignment_reason.trim()) {
      setCreateError('Say why this task goes to that person. Direct assignment needs a reason.')
      return
    }

    // ADR-0050: a no-project task is the default-project onboarding path.
    // "Myself" is the form's UX default, not an explicit direct handoff;
    // omitting it lets the backend attach the creator on the org's project.
    const useDefaultProjectSelfAssignment = Boolean(
      !newTask.project &&
      user?.id &&
      String(newTask.assignee_id) === String(user.id)
    )

    const taskData = {
      mode: 'task',
      item_type: 'task.generic',
      title: newTask.title.trim(),
      description: newTask.description.trim(),
      priority: newTask.priority,
      due_date: newTask.due_date ? new Date(newTask.due_date).toISOString() : null,
      reminder_time: newTask.reminder_time
        ? new Date(newTask.reminder_time).toISOString()
        : null,
      is_recurring: newTask.is_recurring,
      frequency: newTask.frequency,
      tags: newTask.tags.trim(),
      estimated_hours: newTask.estimated_hours
        ? parseFloat(newTask.estimated_hours)
        : null,
      project: newTask.project ? parseInt(newTask.project, 10) : null,
      assignee_id: useDefaultProjectSelfAssignment ? null : (newTask.assignee_id || null),
      assignee_email: useDefaultProjectSelfAssignment ? null : (newTask.assignee_email || null),
      ...(assigningSomeoneElse
        ? { direct_assignment_reason: newTask.direct_assignment_reason.trim() }
        : {}),
    }

    createMutation.mutate({ organizationId, task: taskData })
  }

  const handleTaskClick = (task) => {
    if (!selectionMode) {
      // Navigate to task detail page
      const taskKey = task.item_key || task.id
      navigate(`/tasks/${taskKey}`)
    }
  }

  const openCreateModal = () => {
    const organizationId = organizationIdForNewTask(currentOrganization, organizations)
    setCreateOrgId(organizationId ? String(organizationId) : '')
    setCreateError('')
    setNewTask({
      ...initialTaskState,
      assignee_id: user?.id ? String(user.id) : '',
      assignee_email: user?.email || '',
    })
    setShowCreateModal(true)
  }

  const closeCreateModal = () => {
    setShowCreateModal(false)
    setNewTask(initialTaskState)
    setShowAdvanced(false)
    setCreateError('')
  }

  // Orgs can arrive after the dialog opens. Fill the workspace once so
  // Create is not left on a blank required field.
  useEffect(() => {
    if (!showCreateModal || createOrgId) return
    const organizationId = organizationIdForNewTask(currentOrganization, organizations)
    if (organizationId) setCreateOrgId(String(organizationId))
  }, [showCreateModal, createOrgId, currentOrganization, organizations])

  // Only show full page loader on initial load (page 1 with no data yet)
  if (organizationResolutionError) {
    const useGlobalScope = () => {
      const url = new URL(window.location.href)
      url.searchParams.delete('org')
      url.searchParams.delete('organization')
      url.searchParams.delete('organization_id')
      window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
      switchOrganization(null)
    }

    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50 p-6 text-amber-950 shadow-sm dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600 dark:text-amber-400" />
          <div>
            <h1 className="text-lg font-semibold">Organization scope unavailable</h1>
            <p className="mt-1 text-sm">{organizationResolutionError}</p>
            <div className="mt-4 flex flex-wrap gap-2">
              <button type="button" onClick={refreshOrganizations} className="btn-primary">
                Retry
              </button>
              {organizations.length > 0 && (
                <button type="button" onClick={useGlobalScope} className="btn-secondary">
                  View all organizations
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    )
  }

  if ((!isOrganizationResolved || isLoading) && page === 1 && rawTasks.length === 0) {
    return <PageLoader />
  }

  return (
    <div className="space-y-4 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Tasks</h1>
          <p className="text-gray-500 dark:text-gray-400">
            Manage your personal tasks and to-dos
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowKeyboardHelp(true)}
            className="btn-icon p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
            title="Keyboard shortcuts (?)"
          >
            <HelpCircle size={20} />
          </button>

          {/* Export dropdown */}
          <div className="relative" data-dropdown>
            <button
              onClick={() => { setShowExportMenu(v => !v); }}
              className="btn-secondary flex items-center gap-1.5"
              title="Export tasks"
            >
              <Download size={16} />
              <span className="hidden sm:inline">Export</span>
              <ChevronDown size={14} />
            </button>
            {showExportMenu && (
              <div
                className="absolute right-0 z-40 mt-1 w-56 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg py-1"
                onMouseLeave={() => setShowExportMenu(false)}
              >
                <button
                  onClick={() => handleExport('bundle')}
                  className="w-full flex items-start gap-2.5 px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/50 text-left"
                >
                  <Package size={16} className="text-brand-500 mt-0.5 flex-shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-gray-900 dark:text-white">Bundle (.zip)</div>
                    <div className="text-[11px] text-gray-500 dark:text-gray-400">
                      Full fidelity — tasks, comments, links
                    </div>
                  </div>
                </button>
                <button
                  onClick={() => handleExport('csv')}
                  className="w-full flex items-start gap-2.5 px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/50 text-left"
                >
                  <FileText size={16} className="text-gray-500 mt-0.5 flex-shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-gray-900 dark:text-white">CSV (.csv)</div>
                    <div className="text-[11px] text-gray-500 dark:text-gray-400">
                      Excel-editable, tasks only
                    </div>
                  </div>
                </button>
              </div>
            )}
          </div>

          {/* Import button */}
          <button
            onClick={() => { setShowImportModal(true); setImportFile(null); setImportResult(null); setImportError(null) }}
            className="btn-secondary flex items-center gap-1.5"
            title="Import tasks"
          >
            <Upload size={16} />
            <span className="hidden sm:inline">Import</span>
          </button>

          <button
            onClick={openCreateModal}
            className="btn-primary flex items-center gap-2"
          >
            <Plus size={20} />
            <span className="hidden sm:inline">New Task</span>
          </button>
        </div>
      </div>

      {/* Filters & Controls.
          Single wrapping row: filter pills on the left, right-edge cluster
          (Sort · SavedFilters · View · JQL · Select) pinned with ml-auto
          so that when content wraps, these controls stay together instead
          of orphaning onto their own line. */}
      <div className="space-y-3">
        {jqlMode ? (
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex-1 min-w-[260px]">
              <JQLInput
                value={jqlQuery}
                onChange={setJqlQuery}
                onSearch={handleJqlSearch}
                placeholder="Enter JQL query (e.g., status = 'pending' AND priority = 'high')"
              />
              {fetchError && (
                <p className="text-sm text-red-600 dark:text-red-400 mt-1">
                  {fetchError?.response?.data?.detail || 'Invalid JQL query'}
                </p>
              )}
              {activeJql && (
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Query active
                </p>
              )}
            </div>

            {/* Right-edge toolbar (mirrors standard-mode cluster) */}
            <div className="flex items-center gap-1 ml-auto">
              <div className="flex items-center bg-gray-100 dark:bg-gray-800 rounded-lg p-0.5">
                <button
                  onClick={() => setViewMode('list')}
                  className={cn(
                    'p-1.5 rounded-md transition-colors',
                    viewMode === 'list'
                      ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white'
                      : 'text-gray-500 hover:text-gray-700 dark:text-gray-400'
                  )}
                  title="List view"
                >
                  <List size={16} />
                </button>
                <button
                  onClick={() => setViewMode('kanban')}
                  className={cn(
                    'p-1.5 rounded-md transition-colors',
                    viewMode === 'kanban'
                      ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white'
                      : 'text-gray-500 hover:text-gray-700 dark:text-gray-400'
                  )}
                  title="Board view"
                >
                  <LayoutGrid size={16} />
                </button>
              </div>
              <button
                onClick={toggleJqlMode}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-2 rounded-lg border transition-colors',
                  'bg-brand-100 dark:bg-brand-900 border-brand-300 dark:border-brand-700 text-brand-700 dark:text-brand-300'
                )}
                title="Exit JQL mode"
              >
                <Code size={16} />
                <span className="hidden md:inline">JQL</span>
              </button>
              <button
                onClick={() => setSelectionMode(!selectionMode)}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-2 rounded-lg border transition-colors',
                  selectionMode
                    ? 'bg-brand-100 dark:bg-brand-900 border-brand-300 dark:border-brand-700 text-brand-700 dark:text-brand-300'
                    : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'
                )}
                title={selectionMode ? 'Exit selection mode' : 'Selection mode'}
              >
                <MousePointerClick size={16} />
                <span className="hidden md:inline">
                  {selectionMode ? 'Exit' : 'Select'}
                </span>
              </button>
            </div>
          </div>
        ) : (
          /* Standard Filters (when not in JQL mode) */
          <div className="flex flex-wrap items-center gap-3">
            {/* Search */}
            <div className="relative flex-1 min-w-[200px] max-w-md">
              <Search
                size={16}
                className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
              />
              <input
                type="text"
                placeholder="Search tasks..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="input pl-9 w-full"
              />
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery('')}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                >
                  <X size={16} />
                </button>
              )}
            </div>

            {/* Progress Filter — 3 category buckets (Jira "Status Category" pattern).
                Primary day-to-day filter.
                Default (no selection): label reads "Open" — the cross-industry
                convention (GitHub, Jira) for "not closed/done". Backend uses
                `is_actionable=true` to hide every Done-category status so the
                manager sees all remaining work at a glance with zero clicks.
                To include Done tasks, the user explicitly picks "Done" (or any
                combo) from the dropdown; the filter switches to an explicit
                `status_category=...` query and respects whatever is selected. */}
            <MultiSelectFilter
              label="Open"
              searchPlaceholder="Search progress..."
              selectedValues={progressFilter}
              onChange={setProgressFilter}
              options={[
                // Labels match Jira's "Status Category" convention, plus
                // Pulse's 'scheduled' gap state for recurring tasks.
                { value: 'todo',        label: 'To Do',        sublabel: 'Not started' },
                { value: 'in_progress', label: 'In Progress',  sublabel: 'Underway' },
                { value: 'done',        label: 'Done',         sublabel: 'Terminal states' },
                { value: 'scheduled',   label: 'Scheduled',    sublabel: 'Recurring — waiting for next cycle' },
              ]}
            />

            {/* Status Filter — specific workflow status slugs for drill-down.
                Peer to Progress, ANDed when both set. */}
            <MultiSelectFilter
              label="Any Status"
              searchPlaceholder="Search statuses..."
              selectedValues={statusFilter}
              onChange={setStatusFilter}
              options={statusFilterOptions}
            />

            {/* Priority Filter — searchable multi-select. Empty = any priority. */}
            <MultiSelectFilter
              label="Any Priority"
              searchPlaceholder="Search priorities..."
              selectedValues={priorityFilter}
              onChange={setPriorityFilter}
              options={priorityFilterOptions}
            />

            {/* Project Filter — searchable multi-select. Empty = all accessible
                (personal + member projects). Backend resolves comma-separated
                `project` param via id__in over accessible projects. */}
            <MultiSelectFilter
              label="Any Project"
              searchPlaceholder="Search projects..."
              selectedValues={projectFilter}
              onChange={setProjectFilter}
              options={(projects || []).map((p) => ({
                value: String(p.id),
                label: p.name,
                sublabel: p.key,
                avatar: (p.key || p.name || '?')[0].toUpperCase(),
              }))}
            />

            {/* Assignee Filter — searchable multi-select */}
            <MultiSelectFilter
              label="All Assignees"
              searchPlaceholder="Search assignees..."
              selectedValues={assigneeFilter}
              onChange={setAssigneeFilter}
              options={[
                ...(user?.id ? [{
                  value: String(user.id),
                  label: 'My Tasks',
                  sublabel: user.email,
                  avatar: (user.email || '?')[0].toUpperCase(),
                }] : []),
                ...assignableUsers
                  .filter((u) => (u.user_id || u.email) && String(u.user_id ?? '') !== String(user?.id ?? ''))
                  .map((u) => ({
                    value: u.user_id ? String(u.user_id) : `email:${u.email}`,
                    label: u.user_name || u.name || u.email?.split('@')[0] || u.email,
                    sublabel: u.email,
                    avatar: ((u.user_name || u.name || u.email || '?')[0] || '?').toUpperCase(),
                  })),
              ]}
            />

            {/* Reporter Filter — searchable multi-select */}
            <MultiSelectFilter
              label="All Reporters"
              searchPlaceholder="Search reporters..."
              selectedValues={reporterFilter}
              onChange={setReporterFilter}
              options={[
                ...(user?.id ? [{
                  value: String(user.id),
                  label: 'Created by me',
                  sublabel: user.email,
                  avatar: (user.email || '?')[0].toUpperCase(),
                }] : []),
                ...assignableUsers
                  .filter((u) => (u.user_id || u.email) && String(u.user_id ?? '') !== String(user?.id ?? ''))
                  .map((u) => ({
                    value: u.user_id ? String(u.user_id) : `email:${u.email}`,
                    label: u.user_name || u.name || u.email?.split('@')[0] || u.email,
                    sublabel: u.email,
                    avatar: ((u.user_name || u.name || u.email || '?')[0] || '?').toUpperCase(),
                  })),
              ]}
            />

            {/* Sort */}
            <select
              value={sortOption}
              onChange={(e) => setSortOption(e.target.value)}
              className="input w-auto"
            >
              {SORT_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>

            {/* Right-edge toolbar — SavedFilters, View toggle, JQL toggle,
                Selection mode. Grouped behind ml-auto so they stay clustered
                on the trailing edge and wrap together rather than one-per-line. */}
            <div className="flex items-center gap-1 ml-auto">
              <SavedFilters
                currentFilters={{
                  progress: progressFilter,
                  status: statusFilter,
                  priority: priorityFilter,
                  project: projectFilter,
                  assignee: assigneeFilter,
                  reporter: reporterFilter,
                  sort: sortOption,
                }}
                onApplyFilter={(filters) => {
                  // All filters are now arrays. Normalize legacy saved filters
                  // that may still carry a single string, plus drop the old
                  // '__active__' sentinel (empty array now = "Active").
                  const normalizeList = (v) => {
                    if (Array.isArray(v)) return v
                    if (!v || v === '__active__') return []
                    return [v]
                  }
                  setProgressFilter(normalizeList(filters.progress))
                  setStatusFilter(normalizeList(filters.status))
                  setPriorityFilter(normalizeList(filters.priority))
                  setProjectFilter(normalizeList(filters.project))
                  setAssigneeFilter(normalizeList(filters.assignee))
                  setReporterFilter(normalizeList(filters.reporter))
                  setSortOption(filters.sort)
                }}
              />

              <div className="flex items-center bg-gray-100 dark:bg-gray-800 rounded-lg p-0.5">
                <button
                  onClick={() => setViewMode('list')}
                  className={cn(
                    'p-1.5 rounded-md transition-colors',
                    viewMode === 'list'
                      ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white'
                      : 'text-gray-500 hover:text-gray-700 dark:text-gray-400'
                  )}
                  title="List view"
                >
                  <List size={16} />
                </button>
                <button
                  onClick={() => setViewMode('kanban')}
                  className={cn(
                    'p-1.5 rounded-md transition-colors',
                    viewMode === 'kanban'
                      ? 'bg-white dark:bg-gray-700 shadow-sm text-gray-900 dark:text-white'
                      : 'text-gray-500 hover:text-gray-700 dark:text-gray-400'
                  )}
                  title="Board view"
                >
                  <LayoutGrid size={16} />
                </button>
              </div>

              <button
                onClick={toggleJqlMode}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-2 rounded-lg border transition-colors',
                  'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'
                )}
                title="Switch to JQL query mode"
              >
                <Code size={16} />
                <span className="hidden md:inline">JQL</span>
              </button>

              <button
                onClick={() => setSelectionMode(!selectionMode)}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-2 rounded-lg border transition-colors',
                  selectionMode
                    ? 'bg-brand-100 dark:bg-brand-900 border-brand-300 dark:border-brand-700 text-brand-700 dark:text-brand-300'
                    : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'
                )}
                title={selectionMode ? 'Exit selection mode' : 'Selection mode'}
              >
                <MousePointerClick size={16} />
                <span className="hidden md:inline">
                  {selectionMode ? 'Exit' : 'Select'}
                </span>
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Selection Toolbar */}
      {selectionMode && (
        <SelectionToolbar
          selectedCount={selectedCount}
          totalCount={sortedTasks.length}
          allSelected={allSelected}
          someSelected={someSelected}
          onSelectAll={selectAll}
          onDeselectAll={deselectAll}
          onComplete={() => bulkCompleteMutation.mutate(selectedIds)}
          onDelete={() => bulkDeleteMutation.mutate(selectedIds)}
          onSetStatus={(status) =>
            bulkUpdateMutation.mutate({ ids: selectedIds, status })
          }
          onSetPriority={(priority) =>
            bulkUpdateMutation.mutate({ ids: selectedIds, priority })
          }
          isLoading={isAnyBulkLoading}
        />
      )}

      {boardMoveError && (
        <p role="alert" className="mb-3 text-sm text-red-600 dark:text-red-400">
          {boardMoveError}
        </p>
      )}

      {/* Task List / Kanban */}
      {viewMode === 'list' ? (
        <>
          {sortedTasks.length > 0 ? (
            <div ref={taskListRef} className="space-y-3">
              {sortedTasks.map((task, index) => (
                <ItemCard
                  key={task.id}
                  item={task}
                  onComplete={(item) => completeMutation.mutate(item)}
                  onClick={handleTaskClick}
                  selectable={selectionMode}
                  isSelected={isSelected(task.id)}
                  onToggleSelect={toggleWithShift}
                  isFocused={index === focusedIndex}
                  navigateOnClick={!selectionMode}
                />
              ))}

              {/* Load More Button */}
              {hasMore && !isLoading && allTasks.length > 0 && (
                <div className="flex justify-center pt-4">
                  <button
                    onClick={loadMore}
                    disabled={isLoadingMore || isFetching}
                    className="btn-secondary px-6"
                  >
                    {isLoadingMore || isFetching ? 'Loading...' : 'Load More Tasks'}
                  </button>
                </div>
              )}
            </div>
          ) : !isLoading && !isFetching ? (
            <EmptyState
              icon={CheckSquare}
              title="No tasks found"
              description={
                searchQuery
                  ? `No tasks match "${searchQuery}"`
                  : (progressFilter.length > 0 || statusFilter.length > 0 || priorityFilter.length > 0 || projectFilter.length > 0)
                  ? 'Try adjusting your filters'
                  : 'Create your first task to get started'
              }
              action={
                progressFilter.length === 0 && statusFilter.length === 0 && priorityFilter.length === 0 && projectFilter.length === 0 && !searchQuery && (
                  <button onClick={openCreateModal} className="btn-primary">
                    Create Task
                  </button>
                )
              }
            />
          ) : null}

          {/* Results count */}
          {sortedTasks.length > 0 && (
            <div className="text-sm text-gray-500 dark:text-gray-400 text-center">
              {jqlMode && activeJql
                ? `${sortedTasks.length} tasks matching JQL query${hasMore ? ' (more available)' : ''}`
                : (searchQuery || progressFilter.length > 0 || statusFilter.length > 0 || priorityFilter.length > 0 || projectFilter.length > 0)
                  ? `Showing ${sortedTasks.length} tasks${hasMore ? ' (more available)' : ''}`
                  : `${sortedTasks.length} tasks${hasMore ? ' (more available)' : ''}`}
            </div>
          )}
        </>
      ) : (
        /* Kanban View */
        <div className="overflow-x-auto -mx-4 px-4">
          <div className="flex gap-4 min-w-max">
            {(() => {
              // Same category-based columns as Dashboard — see Dashboard.jsx
              // KANBAN_COLUMNS for the rationale. Uses `status_category` so
              // every workflow slug lands in the right column; literal slug
              // match dropped tasks with workflow-specific statuses.
              // The "Scheduled" column only shows when the user has
              // explicitly filtered for it (or when tasks in that state
              // exist in the current result set) — it's a gap state, not
              // an active work column.
              const baseColumns = [
                { id: 'todo',        label: 'To Do',       dropSlug: 'pending',     icon: Circle,       color: 'text-gray-500',   bg: 'bg-gray-100 dark:bg-gray-800/50' },
                { id: 'in_progress', label: 'In Progress', dropSlug: 'in_progress', icon: Loader2Icon,  color: 'text-blue-500',   bg: 'bg-blue-50 dark:bg-blue-900/20' },
                { id: 'done',        label: 'Done',        dropSlug: 'completed',   icon: CheckCircle2, color: 'text-green-500',  bg: 'bg-green-50 dark:bg-green-900/20' },
              ]
              const hasScheduled = progressFilter.includes('scheduled')
                || sortedTasks.some(t => t.status_category === 'scheduled')
              if (hasScheduled) {
                baseColumns.push({ id: 'scheduled', label: 'Scheduled', dropSlug: 'scheduled', icon: Repeat, color: 'text-violet-500', bg: 'bg-violet-50 dark:bg-violet-900/20' })
              }
              return baseColumns
            })().map((column) => {
              const SLUG_TO_CATEGORY = {
                completed:'done',done:'done',closed:'done',cancelled:'done',canceled:'done',
                resolved:'done',rejected:'done',duplicate:'done',wont_fix:'done',merged:'done',
                applied:'done',risk_accepted:'done',change_rejected:'done',
                in_progress:'in_progress',in_review:'in_progress',awaiting_merge:'in_progress',
                approved:'in_progress',applying:'in_progress',blocked:'in_progress',
                design:'in_progress',in_qa:'in_progress',under_review:'in_progress',
                verification:'in_progress',escalated:'in_progress',awaiting_triage:'in_progress',
                pending:'todo',open:'todo',todo:'todo',backlog:'todo',proposed:'todo',
                found:'todo',needs_triage:'todo',reopened:'todo',apply_failed:'todo',
                scheduled:'scheduled',
              }
              const columnItems = sortedTasks.filter(t => (t.status_category || SLUG_TO_CATEGORY[t.status]) === column.id)
              const Icon = column.icon

              return (
                <div
                  key={column.id}
                  className={cn('w-80 flex flex-col rounded-lg min-h-[400px]', column.bg)}
                  onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}
                  onDrop={async (e) => {
                    e.preventDefault()
                    const taskId = e.dataTransfer.getData('text/plain')
                    const item = sortedTasks.find((task) => String(task.id) === String(taskId))
                    if (!item) return
                    setBoardMoveError('')
                    try {
                      const result = await moveTaskToColumn(item, column.id, itemsApi)
                      if (result.moved) resetAndRefetch()
                    } catch (err) {
                      setBoardMoveError(formatApiError(err, 'This task could not move.'))
                    }
                  }}
                >
                  {/* Column Header */}
                  <div className="flex items-center gap-2 p-3 border-b border-gray-200/50 dark:border-gray-700/50">
                    <Icon size={16} className={column.color} />
                    <span className="font-medium text-sm text-gray-900 dark:text-white">
                      {column.label}
                    </span>
                    <span className="text-xs text-gray-500 bg-white/60 dark:bg-gray-700/60 px-1.5 py-0.5 rounded-full">
                      {columnItems.length}
                    </span>
                  </div>

                  {/* Column Cards */}
                  <div className="flex-1 overflow-y-auto p-2 space-y-2">
                    {columnItems.map((item) => (
                      <div
                        key={item.id}
                        draggable
                        onDragStart={(e) => { e.dataTransfer.setData('text/plain', String(item.id)); e.dataTransfer.effectAllowed = 'move' }}
                        onClick={() => navigate(`/tasks/${item.item_key || item.id}`)}
                        className={cn(
                          'bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-3',
                          'border-l-4 cursor-pointer hover:shadow-md transition-shadow',
                          item.priority === 'urgent' ? 'border-l-red-500' :
                          item.priority === 'high' ? 'border-l-orange-500' :
                          item.priority === 'normal' ? 'border-l-blue-400' : 'border-l-gray-300',
                        )}
                      >
                        <div className="flex items-center justify-between mb-1">
                          <span className="text-[11px] font-medium text-gray-500 dark:text-gray-400">
                            {item.item_key || `#${item.id}`}
                          </span>
                          {item.assignee_email && (
                            <div
                              className="w-5 h-5 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center"
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
                        {(item.project_key || (!currentOrganization && item.organization_id)) && (
                          <span className="text-[10px] text-gray-400 mt-1 block">
                            {[
                              !currentOrganization ? organizationLabelForItem(item) : '',
                              item.project_key,
                            ].filter(Boolean).join(' › ')}
                          </span>
                        )}
                      </div>
                    ))}
                    {columnItems.length === 0 && (
                      <p className="text-xs text-gray-400 dark:text-gray-500 text-center py-8">
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

      {/* Import Modal */}
      {showImportModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={(e) => { if (e.target === e.currentTarget && !importing) setShowImportModal(false) }}
        >
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-lg w-full mx-4 p-5">
            <div className="flex items-center gap-2 mb-3">
              <Upload size={18} className="text-brand-500" />
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Import Tasks</h3>
            </div>

            {!importResult && !importError && (
              <>
                <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
                  Upload a Pulse bundle (.zip) or CSV (.csv) file. Existing tasks (matched by
                  item_key) are skipped — imports are non-destructive.
                </p>

                <div className="border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-6 text-center">
                  <input
                    type="file"
                    id="import-file"
                    accept=".zip,.csv"
                    onChange={(e) => setImportFile(e.target.files?.[0] || null)}
                    className="hidden"
                  />
                  <label htmlFor="import-file" className="cursor-pointer">
                    {importFile ? (
                      <div className="flex flex-col items-center gap-1">
                        {importFile.name.endsWith('.zip')
                          ? <Package size={24} className="text-brand-500" />
                          : <FileText size={24} className="text-gray-500" />
                        }
                        <div className="text-sm font-medium text-gray-900 dark:text-white">
                          {importFile.name}
                        </div>
                        <div className="text-xs text-gray-500">
                          {(importFile.size / 1024).toFixed(1)} KB
                        </div>
                        <div className="text-xs text-brand-600 dark:text-brand-400 mt-1">
                          Click to change
                        </div>
                      </div>
                    ) : (
                      <div className="flex flex-col items-center gap-2">
                        <Upload size={32} className="text-gray-400" />
                        <div className="text-sm text-gray-500 dark:text-gray-400">
                          Click to choose file (.zip or .csv)
                        </div>
                      </div>
                    )}
                  </label>
                </div>

                <div className="flex items-center justify-end gap-2 mt-4">
                  <button
                    type="button"
                    onClick={() => setShowImportModal(false)}
                    disabled={importing}
                    className="btn-secondary"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleImport}
                    disabled={!importFile || importing}
                    className="btn-primary"
                  >
                    {importing ? 'Importing...' : 'Import'}
                  </button>
                </div>
              </>
            )}

            {importError && (
              <>
                <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800/50 rounded-lg p-3 mb-4">
                  <div className="text-sm font-medium text-red-800 dark:text-red-300 mb-1">
                    Import failed
                  </div>
                  <div className="text-sm text-red-700 dark:text-red-400">{importError}</div>
                </div>
                <div className="flex items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => { setImportError(null); setImportFile(null) }}
                    className="btn-secondary"
                  >
                    Try again
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowImportModal(false)}
                    className="btn-primary"
                  >
                    Close
                  </button>
                </div>
              </>
            )}

            {importResult && (
              <>
                <div className="bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800/50 rounded-lg p-3 mb-4">
                  <div className="text-sm font-medium text-green-800 dark:text-green-300 mb-2">
                    Import complete
                  </div>
                  <div className="text-sm text-green-700 dark:text-green-400 space-y-0.5">
                    <div>Tasks: <strong>{importResult.tasks_created}</strong> created, <strong>{importResult.tasks_skipped}</strong> skipped</div>
                    {importResult.comments_created > 0 && (
                      <div>Comments: <strong>{importResult.comments_created}</strong> created</div>
                    )}
                    {importResult.links_created > 0 && (
                      <div>Links: <strong>{importResult.links_created}</strong> created</div>
                    )}
                    {importResult.tasks_failed > 0 && (
                      <div className="text-red-600 dark:text-red-400">
                        Failed: <strong>{importResult.tasks_failed}</strong>
                      </div>
                    )}
                  </div>
                </div>
                {importResult.warnings?.length > 0 && (
                  <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800/50 rounded-lg p-3 mb-4">
                    <div className="text-xs font-medium text-amber-800 dark:text-amber-300 mb-1">
                      {importResult.warnings.length} warning{importResult.warnings.length !== 1 ? 's' : ''}:
                    </div>
                    <ul className="text-xs text-amber-700 dark:text-amber-400 list-disc list-inside space-y-0.5 max-h-24 overflow-y-auto">
                      {importResult.warnings.slice(0, 10).map((w, i) => <li key={i}>{w}</li>)}
                      {importResult.warnings.length > 10 && (
                        <li>... and {importResult.warnings.length - 10} more</li>
                      )}
                    </ul>
                  </div>
                )}
                <div className="flex items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setShowImportModal(false)
                      setImportFile(null)
                      setImportResult(null)
                    }}
                    className="btn-primary"
                  >
                    Done
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Create Task Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/50" onClick={closeCreateModal} />
          <div className="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-hidden flex flex-col">
            {/* Header */}
            <div className="shrink-0 p-4 border-b border-gray-200 dark:border-gray-700">
              <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
                Create New Task
              </h2>
            </div>

            {/* Form */}
            <form id="create-new-task-form" onSubmit={handleCreateTask} className="flex-1 min-h-0 overflow-y-auto p-4">
              <div className="space-y-4">
                {!currentOrganization && organizations.length > 1 && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Workspace *
                    </label>
                    <select
                      value={createOrgId}
                      onChange={(e) => setCreateOrgId(e.target.value)}
                      className="input"
                    >
                      <option value="">Choose a workspace</option>
                      {organizations.map((org) => (
                        <option key={org.id} value={String(org.id)}>
                          {org.name || org.slug}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                {/* Title */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Title *
                  </label>
                  <input
                    type="text"
                    value={newTask.title}
                    onChange={(e) => setNewTask({ ...newTask, title: e.target.value })}
                    className="input"
                    placeholder="What needs to be done?"
                    autoFocus
                    required
                  />
                </div>

                {/* Description/Notes */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Notes
                  </label>
                  <textarea
                    value={newTask.description}
                    onChange={(e) =>
                      setNewTask({ ...newTask, description: e.target.value })
                    }
                    className="input min-h-[80px]"
                    placeholder="Add details, links, or notes..."
                  />
                </div>

                {/* Due Date & Time */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    <Calendar size={14} className="inline mr-1" />
                    Due Date & Time
                  </label>
                  <input
                    type="datetime-local"
                    value={newTask.due_date}
                    onChange={(e) => setNewTask({ ...newTask, due_date: e.target.value })}
                    className="input"
                  />
                </div>

                {/* Reminder */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    <Bell size={14} className="inline mr-1" />
                    Reminder
                  </label>
                  <input
                    type="datetime-local"
                    value={newTask.reminder_time}
                    onChange={(e) =>
                      setNewTask({ ...newTask, reminder_time: e.target.value })
                    }
                    className="input"
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Get notified before the task is due
                  </p>
                </div>

                {/* Priority */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    <Flag size={14} className="inline mr-1" />
                    Priority
                  </label>
                  <select
                    value={newTask.priority}
                    onChange={(e) => setNewTask({ ...newTask, priority: e.target.value })}
                    className="input"
                  >
                    {PRIORITY_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </div>

                {/* Assignee */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    <User size={14} className="inline mr-1" />
                    Assignee
                  </label>
                  <select
                    value={newTask.assignee_id}
                    onChange={(e) => {
                      const selectedId = e.target.value
                      const selectedUser = assignableUsers.find(
                        (u) => String(u.user_id) === String(selectedId)
                      )
                      const isSelf = user?.id && String(selectedId) === String(user.id)
                      setNewTask({
                        ...newTask,
                        assignee_id: selectedId || '',
                        assignee_email: isSelf
                          ? (user.email || '')
                          : (selectedUser?.email || ''),
                        direct_assignment_reason: isSelf ? '' : newTask.direct_assignment_reason,
                      })
                    }}
                    className="input"
                  >
                    <option value="">Unassigned</option>
                    {user?.id && (
                      <option value={String(user.id)}>
                        Myself ({user.email?.split('@')[0]})
                      </option>
                    )}
                    {assignableUsers
                      .filter((u) => u.user_id && String(u.user_id) !== String(user?.id))
                      .map((u) => (
                        <option key={u.user_id} value={String(u.user_id)}>
                          {u.email}
                        </option>
                      ))}
                  </select>
                  {newTask.assignee_id && user?.id && String(newTask.assignee_id) !== String(user.id) && (
                    <div className="mt-2">
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                        Why this person *
                      </label>
                      <input
                        type="text"
                        value={newTask.direct_assignment_reason}
                        onChange={(e) => setNewTask({ ...newTask, direct_assignment_reason: e.target.value })}
                        className="input"
                        placeholder="What should they do with this task?"
                      />
                    </div>
                  )}
                </div>

                {/* Advanced Options Toggle */}
                <button
                  type="button"
                  onClick={() => setShowAdvanced(!showAdvanced)}
                  className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
                >
                  {showAdvanced ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                  Advanced options
                </button>

                {showAdvanced && (
                  <div className="space-y-4 pl-4 border-l-2 border-gray-200 dark:border-gray-700">
                    {/* Project */}
                    {projects && projects.length > 0 && (
                      <div>
                        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                          <FolderKanban size={14} className="inline mr-1" />
                          Project
                        </label>
                        <select
                          value={newTask.project}
                          onChange={(e) => setNewTask({ ...newTask, project: e.target.value })}
                          className="input"
                        >
                          <option value="">Personal (no project)</option>
                          {projects.map((project) => (
                            <option key={project.id} value={project.id}>
                              {project.key} - {project.name}
                            </option>
                          ))}
                        </select>
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                          Assign to a project to share with team members
                        </p>
                      </div>
                    )}

                    {/* Recurring */}
                    <div>
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                        <Repeat size={14} className="inline mr-1" />
                        Repeat
                      </label>
                      <select
                        value={newTask.frequency}
                        onChange={(e) =>
                          setNewTask({
                            ...newTask,
                            frequency: e.target.value,
                            is_recurring: e.target.value !== 'one_time',
                          })
                        }
                        className="input"
                      >
                        {FREQUENCY_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>
                            {opt.label}
                          </option>
                        ))}
                      </select>
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                        Recurring tasks auto-create the next instance when completed
                      </p>
                    </div>

                    {/* Tags */}
                    <div>
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                        <Tag size={14} className="inline mr-1" />
                        Tags
                      </label>
                      <input
                        type="text"
                        value={newTask.tags}
                        onChange={(e) => setNewTask({ ...newTask, tags: e.target.value })}
                        className="input"
                        placeholder="work, personal, urgent (comma-separated)"
                      />
                    </div>

                    {/* Estimated Time */}
                    <div>
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                        <Clock size={14} className="inline mr-1" />
                        Estimated Time (hours)
                      </label>
                      <input
                        type="number"
                        step="0.5"
                        min="0"
                        value={newTask.estimated_hours}
                        onChange={(e) =>
                          setNewTask({ ...newTask, estimated_hours: e.target.value })
                        }
                        className="input"
                        placeholder="e.g., 2.5"
                      />
                    </div>
                  </div>
                )}
              </div>
            </form>

            {/* Footer */}
            <div className="shrink-0 p-4 border-t border-gray-200 dark:border-gray-700">
              {createError && (
                <p role="alert" className="mb-3 text-sm text-red-600 dark:text-red-400">
                  {createError}
                </p>
              )}
              <div className="flex gap-3">
                <button type="button" onClick={closeCreateModal} className="btn-secondary flex-1">
                  Cancel
                </button>
                <button
                  type="submit"
                  form="create-new-task-form"
                  disabled={!newTask.title.trim() || createMutation.isPending}
                  className="btn-primary flex-1"
                >
                  {createMutation.isPending ? 'Creating...' : 'Create Task'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Keyboard Shortcuts Help Modal */}
      {showKeyboardHelp && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => setShowKeyboardHelp(false)}
          />
          <div className="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-md p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                Keyboard Shortcuts
              </h2>
              <button
                onClick={() => setShowKeyboardHelp(false)}
                className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                <X size={20} />
              </button>
            </div>

            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                <div className="text-gray-500 dark:text-gray-400">Navigation</div>
                <div></div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">j</kbd> / <kbd className="kbd">k</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">Next / Previous task</div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">Enter</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">Open task details</div>

                <div className="mt-2 text-gray-500 dark:text-gray-400">Selection</div>
                <div></div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">s</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">Toggle selection mode</div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">x</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">Toggle selection</div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">Ctrl</kbd> + <kbd className="kbd">a</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">Select all</div>

                <div className="mt-2 text-gray-500 dark:text-gray-400">Actions</div>
                <div></div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">c</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">Complete task(s)</div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">n</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">New task</div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">Esc</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">Clear / Cancel</div>

                <div className="flex items-center gap-2">
                  <kbd className="kbd">?</kbd>
                </div>
                <div className="text-gray-700 dark:text-gray-300">Show this help</div>
              </div>
            </div>

            <div className="mt-6 pt-4 border-t border-gray-200 dark:border-gray-700">
              <p className="text-xs text-gray-500 dark:text-gray-400 text-center">
                Hold <kbd className="kbd text-xs">Shift</kbd> while clicking to select a range
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
