import { useState, useEffect, useMemo } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Edit3,
  Trash2,
  CheckCircle,
  Circle,
  Calendar,
  Clock,
  Flag,
  Tag,
  User,
  UserPlus,
  Eye,
  EyeOff,
  FolderKanban,
  Link as LinkIcon,
  Paperclip,
  MessageSquare,
  Activity,
  MoreHorizontal,
  Share2,
  ExternalLink,
  ChevronDown,
  ChevronUp,
  X,
  ListTree,
  PlayCircle,
  GitBranch,
  Copy,
  Repeat,
} from 'lucide-react'
import { itemsApi, projectsApi, commentsApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { useOrganization } from '../contexts/OrganizationContext'
import { cn } from '../utils/cn'
import { formatSmartDate, formatRelativeTime, getPriorityClasses, formatRecurrence } from '../utils/format'
import { formatOrganizationName, formatScopedTeamName, formatTeamName } from '../utils/teamLabels'
import { assigneeDisplayName, formatAssigneeRosterTeams, isAssigneeOutsideAssignmentGroup } from '../utils/assigneeDisplay'
import RecurrenceEditor from '../components/RecurrenceEditor'
import SnoozeMenu from '../components/SnoozeMenu'
import { PageLoader } from '../components/LoadingSpinner'
import CommentsSection from '../components/CommentsSection'
import AttachmentSection from '../components/AttachmentSection'
import ActivityTimeline from '../components/ActivityTimeline'
import LinkedIssues, { InlineLinkedIssues } from '../components/LinkedIssues'
import DescriptionEditor from '../components/DescriptionEditor'
import SubtasksSection from '../components/SubtasksSection'
import UserSearchInput from '../components/UserSearchInput'
import SprintSelector from '../components/SprintSelector'
import ComponentSelector from '../components/ComponentSelector'
import VersionSelector from '../components/VersionSelector'
import WorkflowStatusBar from '../components/WorkflowStatusBar'
import { useToast } from '../components/Toast'
import TimeTrackingDisplay from '../components/TimeTrackingDisplay'
import WorkLogModal from '../components/WorkLogModal'
import WorkLogList from '../components/WorkLogList'
import PullRequests from '../components/PullRequests'
import ProgressSlider from '../components/ProgressSlider'
import ItemPicker from '../components/ItemPicker'

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', dot: 'bg-blue-400', text: 'text-blue-600 dark:text-blue-400', icon: '↓' },
  { value: 'normal', label: 'Normal', dot: 'bg-green-500', text: 'text-green-600 dark:text-green-400', icon: '═' },
  { value: 'high', label: 'High', dot: 'bg-orange-500', text: 'text-orange-600 dark:text-orange-400', icon: '↑' },
  { value: 'urgent', label: 'Urgent', dot: 'bg-red-500', text: 'text-red-600 dark:text-red-400', icon: '⇑' },
]

const STATUS_OPTIONS = [
  { value: 'pending', label: 'Pending', bg: 'bg-gray-100 dark:bg-gray-700', text: 'text-gray-700 dark:text-gray-300' },
  { value: 'in_progress', label: 'In Progress', bg: 'bg-blue-100 dark:bg-blue-900/50', text: 'text-blue-700 dark:text-blue-300' },
  { value: 'completed', label: 'Completed', bg: 'bg-green-100 dark:bg-green-900/50', text: 'text-green-700 dark:text-green-300' },
  { value: 'blocked', label: 'Blocked', bg: 'bg-red-100 dark:bg-red-900/50', text: 'text-red-700 dark:text-red-300' },
]

export default function TaskDetail() {
  const { taskKey } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const { organizations, projects } = useOrganization()
  const toast = useToast()

  const [isEditing, setIsEditing] = useState(false)
  const [isEditingTitle, setIsEditingTitle] = useState(false)
  const [isEditingDueDate, setIsEditingDueDate] = useState(false)
  const [activeTab, setActiveTab] = useState('comments')
  const [showProjectDropdown, setShowProjectDropdown] = useState(false)
  const [showStatusMenu, setShowStatusMenu] = useState(false)
  const [showPriorityMenu, setShowPriorityMenu] = useState(false)
  const [editData, setEditData] = useState({})
  const [showWorkLogModal, setShowWorkLogModal] = useState(false)
  const [showDuplicateModal, setShowDuplicateModal] = useState(false)
  const [duplicateTarget, setDuplicateTarget] = useState(null)
  const [duplicateComment, setDuplicateComment] = useState('')

  // Fetch task by key (PROJ-123) or by ID (numeric)
  const {
    data: task,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['task', taskKey],
    queryFn: async () => {
      // Check if taskKey is a numeric ID
      const isNumericId = /^\d+$/.test(taskKey)
      if (isNumericId) {
        const res = await itemsApi.get(taskKey)
        return res.data
      }
      // Otherwise try to fetch by item_key (e.g., PROJ-123)
      const res = await itemsApi.getByKey(taskKey)
      return res.data
    },
    enabled: !!taskKey,
  })

  // Fetch project members if task has a project (project-scoped UI).
  // Mentions intentionally use the all-accessible-org endpoint below so @
  // suggestions are not limited to the task's/current org (PLS-99).
  const { data: projectMembers = [] } = useQuery({
    queryKey: ['project-members', task?.project],
    queryFn: async () => {
      if (!task?.project) return []
      const res = await projectsApi.members(task.project)
      return res.data.members || []
    },
    enabled: !!task?.project,
  })

  const { data: allOrgMembers = [] } = useQuery({
    queryKey: ['mention-members', 'all-accessible-orgs'],
    queryFn: async () => {
      const res = await itemsApi.searchOrgMembers()
      return res.data.members || []
    },
    staleTime: 5 * 60 * 1000,
  })

  const { data: assigneeRosterData = null } = useQuery({
    queryKey: ['team-roster-member', task?.project, task?.assignee_email],
    queryFn: async () => {
      try {
        const res = await itemsApi.teamRosterMember(task.assignee_email, task.project)
        return res.data
      } catch {
        return null
      }
    },
    enabled: !!task?.assignee_email && !!task?.project,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })

  const assigneeRosterTeamLabels = useMemo(
    () => formatAssigneeRosterTeams(assigneeRosterData),
    [assigneeRosterData]
  )
  const assigneeGroupMismatch = useMemo(
    () => isAssigneeOutsideAssignmentGroup(task, assigneeRosterData),
    [task, assigneeRosterData]
  )

  const taskProject = useMemo(
    () => projects.find(project => String(project.id) === String(task?.project)) || null,
    [projects, task?.project]
  )
  const taskOrganization = useMemo(() => {
    const organizationId = task?.organization_id
      ?? taskProject?.organization
      ?? taskProject?.org_id
      ?? null
    const organizationSlug = task?.organization_slug
      || taskProject?.organization_slug
      || ''
    const organization = organizations.find(candidate => (
      (organizationId !== null && String(candidate.id) === String(organizationId))
      || (organizationSlug && candidate.slug === organizationSlug)
    ))
    return {
      organization_id: organizationId ?? organization?.id ?? null,
      organization_slug: organizationSlug || organization?.slug || '',
      organization_name: task?.organization_name
        || taskProject?.organization_name
        || organization?.name
        || '',
    }
  }, [organizations, task?.organization_id, task?.organization_name, task?.organization_slug, taskProject])
  const taskHasOrganization = taskOrganization.organization_id !== null
  const taskOrganizationLabel = formatOrganizationName(taskOrganization)
    || 'No organization (legacy)'
  const { data: taskOrganizationProjects = [] } = useQuery({
    queryKey: ['task-organization-projects', taskOrganization.organization_id],
    queryFn: () => projectsApi.listAll({
      organization: taskOrganization.organization_id,
    }),
    enabled: taskOrganization.organization_id !== null,
    staleTime: 5 * 60 * 1000,
  })
  const sameOrganizationProjects = useMemo(() => {
    if (taskOrganization.organization_id === null) return []
    return taskOrganizationProjects.filter(project => {
      const projectOrganizationId = project.organization ?? project.org_id
      return String(projectOrganizationId) === String(taskOrganization.organization_id)
    })
  }, [taskOrganizationProjects, taskOrganization.organization_id])
  const assignmentTeam = task?.assignment_team || {
    team_key: task?.assignment_group,
    display_name: task?.assignment_group,
  }
  const assignmentTeamLabel = formatScopedTeamName(assignmentTeam, taskOrganization)

  // Fetch watchers
  const { data: watchersData } = useQuery({
    queryKey: ['task-watchers', task?.id],
    queryFn: async () => {
      const res = await itemsApi.getWatchers(task.id)
      return res.data
    },
    enabled: !!task?.id,
  })

  const isWatching = watchersData?.watchers?.some(w => w.user_id === user?.id)

  // Transform all accessible org members into mentionable users format.
  const mentionableUsers = useMemo(() => {
    return allOrgMembers.map(member => ({
      id: member.user_id,
      label: member.user_name || member.user_email?.split('@')[0] || 'Unknown',
      email: member.user_email,
    }))
  }, [allOrgMembers])

  // Helper to extract a human-readable error message from API errors
  const getErrorMessage = (err) => {
    const data = err?.response?.data
    if (!data) return err?.message || 'Something went wrong'
    if (typeof data === 'string') return data
    // API returns { field: "message" } or { error: "message" } or { status: "message" }
    if (data.error) return data.error
    if (data.detail) return data.detail
    const firstValue = Object.values(data)[0]
    if (typeof firstValue === 'string') return firstValue
    if (Array.isArray(firstValue)) return firstValue[0]
    return err?.message || 'Something went wrong'
  }

  // Mutations
  const updateMutation = useMutation({
    mutationFn: (data) => itemsApi.update(task.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      setIsEditing(false)
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  // Mutation for status changes — uses workflow transitions when available
  const statusTransitionMutation = useMutation({
    mutationFn: (newStatus) => {
      if (task.workflow_name) {
        return itemsApi.executeTransition(task.id, { to_status: newStatus })
      }
      return itemsApi.update(task.id, { status: newStatus })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['available-transitions', task.id] })
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  // Snooze — manually park a recurring task in Scheduled until a chosen time.
  const snoozeMutation = useMutation({
    mutationFn: (payload) => itemsApi.snooze(task.id, payload),
    onSuccess: (res) => {
      const next = res?.data?.next_fire_at
      toast.success(
        next
          ? `Snoozed until ${new Date(next).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`
          : 'Snoozed'
      )
      queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['available-transitions', task.id] })
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  // Run Now — manually fire the next recurring cycle from Scheduled/Done.
  const runNowMutation = useMutation({
    mutationFn: () => {
      const cycle = task.metadata?.recurring_cycle || {}
      return itemsApi.runNow(task.id, {
        expected_cycle_revision: cycle.cycle_revision ?? 0,
        expected_generation: cycle.active_generation ?? 0,
        expected_due_at: task.due_date,
        expected_completion_watermark: cycle.last_terminal_completion_at ?? '',
        recurring_event_id: cycle.last_event_id || undefined,
      })
    },
    onSuccess: () => {
      toast.success('Cycle started')
      queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['available-transitions', task.id] })
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  const completeMutation = useMutation({
    mutationFn: () => itemsApi.complete(task.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  const deleteMutation = useMutation({
    mutationFn: () => itemsApi.delete(task.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      navigate(-1)
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  const assignMutation = useMutation({
    mutationFn: (data) => itemsApi.assign(task.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  const watchMutation = useMutation({
    mutationFn: () => isWatching
      ? itemsApi.unwatch(task.id)
      : itemsApi.watch(task.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task-watchers', task.id] })
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  const markDuplicateMutation = useMutation({
    mutationFn: ({ target, comment }) => itemsApi.markDuplicate(task.id, {
      target_item_id: target.id,
      target_item_key: target.item_key,
      comment,
    }),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
      queryClient.invalidateQueries({ queryKey: ['task-links', task.id] })
      queryClient.invalidateQueries({ queryKey: ['task-comments', task.id] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      const target = res?.data?.target
      toast.success(`Marked as duplicate of ${target?.item_key || target?.id || 'target task'}`)
      setShowDuplicateModal(false)
      setDuplicateTarget(null)
      setDuplicateComment('')
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  const unmarkDuplicateMutation = useMutation({
    mutationFn: () => itemsApi.unmarkDuplicate(task.id),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
      queryClient.invalidateQueries({ queryKey: ['task-links', task.id] })
      queryClient.invalidateQueries({ queryKey: ['task-comments', task.id] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      toast.success(`Restored to ${res?.data?.restored_status || 'previous status'}`)
    },
    onError: (err) => toast.error(getErrorMessage(err)),
  })

  // Initialize edit data when task loads
  useEffect(() => {
    if (task) {
      setEditData({
        title: task.title || '',
        description: task.description || '',
        priority: task.priority || 'normal',
        status: task.status || 'pending',
        due_date: task.due_date ? task.due_date.slice(0, 16) : '',
        tags: task.tags || '',
        estimated_hours: task.estimated_hours || '',
      })
    }
  }, [task])

  // Close dropdowns when clicking outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (!e.target.closest('[data-dropdown]')) {
        setShowProjectDropdown(false)
        setShowStatusMenu(false)
        setShowPriorityMenu(false)
      }
    }
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [])

  const handleSave = () => {
    // Only save title-related edits (description is handled separately by DescriptionEditor)
    const data = {
      title: editData.title,
      priority: editData.priority,
      status: editData.status,
      tags: editData.tags,
      due_date: editData.due_date ? new Date(editData.due_date).toISOString() : null,
      estimated_hours: editData.estimated_hours ? parseFloat(editData.estimated_hours) : null,
    }
    updateMutation.mutate(data)
  }

  const handleFieldChange = (field, value) => {
    // Quick update for status/priority without entering edit mode
    if (field === 'status') {
      statusTransitionMutation.mutate(value)
    } else {
      updateMutation.mutate({ [field]: value })
    }
  }

  if (isLoading) {
    return <PageLoader />
  }

  if (error || !task) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px] text-center">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
          Task not found
        </h2>
        <p className="text-gray-500 dark:text-gray-400 mt-2">
          The task "{taskKey}" could not be found.
        </p>
        <button
          onClick={() => navigate(-1)}
          className="btn-primary mt-4"
        >
          Go Back
        </button>
      </div>
    )
  }

  const priorityClasses = getPriorityClasses(task.priority)
  const isOverdue = task.due_date && new Date(task.due_date) < new Date() && task.status !== 'completed'

  return (
    <div className="max-w-6xl mx-auto">
      {/* Header / Breadcrumb */}
      <div className="flex items-center flex-wrap gap-3 mb-4">
        <button
          onClick={() => navigate(-1)}
          className="btn-icon"
          title="Go back"
        >
          <ArrowLeft size={20} />
        </button>
        <nav
          aria-label="Task location"
          className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400"
        >
          <>
            <span
              data-testid="task-owning-organization"
              className={cn(
                'font-medium',
                taskHasOrganization
                  ? 'text-gray-700 dark:text-gray-300'
                  : 'text-amber-700 dark:text-amber-300'
              )}
            >
              {taskOrganizationLabel}
            </span>
            <span aria-hidden="true">›</span>
          </>
          {task.project_key && (
            <>
              <Link
                to={`/projects/${task.project}`}
                className="hover:text-brand-600 dark:hover:text-brand-400"
              >
                {task.project_name || task.project_key}
              </Link>
              <span aria-hidden="true">›</span>
            </>
          )}
          <span className="font-medium text-gray-900 dark:text-gray-100">
            {task.item_key || `#${task.id}`}
          </span>
        </nav>

        <div className="flex-1" />

        {/* Action buttons */}
        <div className="flex items-center gap-2">
          <button
            onClick={() => watchMutation.mutate()}
            disabled={watchMutation.isPending}
            className={cn(
              'btn-icon',
              isWatching && 'text-brand-600 dark:text-brand-400'
            )}
            title={isWatching ? 'Stop watching' : 'Watch this task'}
          >
            {isWatching ? <EyeOff size={18} /> : <Eye size={18} />}
          </button>
          <button
            onClick={() => navigator.clipboard.writeText(window.location.href)}
            className="btn-icon"
            title="Copy link"
          >
            <Share2 size={18} />
          </button>
          {!isEditing && (
            <>
              <button
                onClick={() => setIsEditing(true)}
                className="btn-icon"
                title="Edit"
              >
                <Edit3 size={18} />
              </button>
              <button
                onClick={() => setShowDuplicateModal(true)}
                className="btn-icon"
                title="Mark as duplicate"
              >
                <Copy size={18} />
              </button>
              <button
                onClick={() => {
                  if (confirm('Are you sure you want to delete this task?')) {
                    deleteMutation.mutate()
                  }
                }}
                className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                title="Delete"
              >
                <Trash2 size={18} />
              </button>
            </>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">
        {/* Main Content */}
        <div className="lg:col-span-2 space-y-6">
          {/* Parent Task Link */}
          {task.parent_item && (
            <div className="flex items-center gap-2 text-sm">
              <GitBranch size={14} className="text-gray-400" />
              <span className="text-gray-500 dark:text-gray-400">Subtask of</span>
              <Link
                to={task.parent_item_key ? `/tasks/${task.parent_item_key}` : `/tasks/${task.parent_item}`}
                className="font-medium text-brand-600 dark:text-brand-400 hover:underline"
              >
                {task.parent_item_key || `#${task.parent_item}`}
              </Link>
              {task.parent_item_title && (
                <span className="text-gray-500 dark:text-gray-400 truncate">{task.parent_item_title}</span>
              )}
            </div>
          )}

          {/* Duplicate Banner — shown when task was marked as duplicate */}
          {task.metadata?.duplicate_of && (
            <div className="flex items-center gap-2 p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800/50 text-sm">
              <Copy size={14} className="text-amber-600 dark:text-amber-400 flex-shrink-0" />
              <span className="text-amber-800 dark:text-amber-200">
                Marked as duplicate of{' '}
                <Link
                  to={`/tasks/${task.metadata.duplicate_of.target_key || task.metadata.duplicate_of.target_id}`}
                  className="font-semibold hover:underline"
                >
                  {task.metadata.duplicate_of.target_key || `#${task.metadata.duplicate_of.target_id}`}
                </Link>
              </span>
              <div className="flex-1" />
              <button
                onClick={() => {
                  if (confirm('Reopen this task? This will remove the duplicate link and restore the previous status.')) {
                    unmarkDuplicateMutation.mutate()
                  }
                }}
                disabled={unmarkDuplicateMutation.isPending}
                className="text-xs font-medium text-amber-700 dark:text-amber-300 hover:text-amber-900 dark:hover:text-amber-100 hover:underline flex-shrink-0"
              >
                {unmarkDuplicateMutation.isPending ? 'Reopening...' : 'Reopen'}
              </button>
            </div>
          )}

          {/* Title and Status.
              Treat any task in the `done` status_category as completed for
              visual purposes (applies to Applied / Done / Completed / Closed /
              Resolved / Cancelled / etc.). The card itself dims and the
              leading button becomes a filled green check. */}
          <div className={cn(
            'card p-6 transition-opacity',
            (task.status_category === 'done' || task.status === 'completed') && 'opacity-75'
          )}>
            <div className="flex items-start gap-4">
              {/* Complete button — lights up for any done-category state */}
              <button
                onClick={() => (task.status_category !== 'done' && task.status !== 'completed') && completeMutation.mutate()}
                disabled={(task.status_category === 'done' || task.status === 'completed') || completeMutation.isPending}
                className={cn(
                  'mt-1 p-1 rounded-full transition-colors flex-shrink-0',
                  (task.status_category === 'done' || task.status === 'completed')
                    ? 'text-green-500'
                    : 'text-gray-400 hover:text-green-500 dark:text-gray-500 dark:hover:text-green-400'
                )}
              >
                {(task.status_category === 'done' || task.status === 'completed') ? (
                  <CheckCircle size={28} />
                ) : (
                  <Circle size={28} />
                )}
              </button>

              <div className="flex-1 min-w-0">
                {isEditing || isEditingTitle ? (
                  <input
                    type="text"
                    value={editData.title}
                    onChange={(e) => setEditData({ ...editData, title: e.target.value })}
                    onBlur={() => {
                      if (isEditingTitle && editData.title !== task.title) {
                        updateMutation.mutate({ title: editData.title })
                      }
                      setIsEditingTitle(false)
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault()
                        if (editData.title !== task.title) {
                          updateMutation.mutate({ title: editData.title })
                        }
                        setIsEditingTitle(false)
                      } else if (e.key === 'Escape') {
                        setEditData({ ...editData, title: task.title })
                        setIsEditingTitle(false)
                      }
                    }}
                    className="input text-xl font-semibold w-full"
                    placeholder="Task title"
                    autoFocus
                  />
                ) : (
                  <h1
                    onClick={() => setIsEditingTitle(true)}
                    className={cn(
                      'text-xl font-semibold text-gray-900 dark:text-gray-100 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800 rounded px-1 -mx-1',
                      // Strike through + mute for ANY terminal/done-category status
                      // (done, completed, applied, cancelled, closed, resolved, etc.)
                      // Use status_category from the serializer so we don't
                      // duplicate the list of terminal slugs across the stack.
                      (task.status_category === 'done' || task.status === 'completed') &&
                        'line-through text-gray-500 dark:text-gray-500'
                    )}
                    title="Click to edit title"
                  >
                    {task.title}
                  </h1>
                )}

                {/* Type badge */}
                <div className="flex items-center gap-2 mt-2">
                  <span className="badge badge-gray text-xs">
                    {task.item_type?.replace('task.', '') || 'task'}
                  </span>
                  {task.is_recurring && (
                    <span className="badge badge-info text-xs">
                      Recurring ({formatRecurrence(task.frequency, task.recurrence_pattern) || task.frequency})
                    </span>
                  )}
                </div>
              </div>
            </div>

            {/* Description */}
            <div className="mt-4">
              <label className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2 block">
                Description
              </label>
              <DescriptionEditor
                itemId={task.id}
                content={task.description || ''}
                onSave={(html) => updateMutation.mutate({ description: html })}
                isSaving={updateMutation.isPending}
                mentionableUsers={mentionableUsers}
              />
            </div>

            {/* Edit Actions */}
            {isEditing && (
              <div className="flex gap-3 mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
                <button
                  onClick={() => setIsEditing(false)}
                  className="btn-secondary"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSave}
                  disabled={!editData.title.trim() || updateMutation.isPending}
                  className="btn-primary"
                >
                  {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
                </button>
              </div>
            )}
          </div>

          {/* Linked Issues — Subtasks, Duplicates, Blocks, etc. all in one widget */}
          <div className="card p-4 space-y-3">
            <div className="flex items-center gap-2">
              <LinkIcon size={14} className="text-gray-500 dark:text-gray-400" />
              <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                Linked Issues
              </h3>
            </div>
            <SubtasksSection
              parentItemId={task.id}
              projectId={task.project}
              projectMembers={projectMembers}
              embedded
            />
            <InlineLinkedIssues itemId={task.id} embedded />
          </div>

          {/* Activity Tabs */}
          <div className="card">
            <div className="flex border-b border-gray-200 dark:border-gray-700 overflow-x-auto">
              {[
                { id: 'comments', label: 'Comments', icon: MessageSquare },
                { id: 'activity', label: 'History', icon: Activity },
                { id: 'worklogs', label: 'Work Logs', icon: Clock },
                { id: 'attachments', label: 'Attachments', icon: Paperclip },
                { id: 'links', label: 'Links', icon: LinkIcon },
              ].map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={cn(
                    'flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors',
                    activeTab === tab.id
                      ? 'border-brand-500 text-brand-600 dark:text-brand-400'
                      : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
                  )}
                >
                  <tab.icon size={16} />
                  {tab.label}
                </button>
              ))}
            </div>

            <div className="p-4">
              {activeTab === 'comments' && (
                <CommentsSection itemId={task.id} mentionableUsers={mentionableUsers} />
              )}
              {activeTab === 'activity' && (
                <ActivityTimeline itemId={task.id} />
              )}
              {activeTab === 'worklogs' && (
                <WorkLogList itemId={task.id} />
              )}
              {activeTab === 'attachments' && (
                <AttachmentSection itemId={task.id} />
              )}
              {activeTab === 'links' && (
                <LinkedIssues itemId={task.id} itemKey={task.item_key} />
              )}
            </div>
          </div>
        </div>

        {/* Sidebar */}
        <div className="space-y-4 lg:sticky lg:top-4">
          {/* Workflow Pipeline + Transitions */}
          {(task.workflow_name || task.workflow_status) && (
            <WorkflowStatusBar
              task={task}
              onTaskUpdate={() => {
                queryClient.invalidateQueries({ queryKey: ['task', taskKey] })
              }}
            />
          )}

          {/* Snooze action — visible for any recurring task, parks it in
              Scheduled with a user-chosen next_fire_at. When the task is
              already Scheduled, the menu label flips to "Reschedule" so
              picking a new time is a cleaner mental model than going
              through Run Now → re-snooze. */}
          {task.is_recurring && (
            <div className="flex justify-end">
              <SnoozeMenu
                task={task}
                onSnooze={(payload) => snoozeMutation.mutate(payload)}
                isSnoozing={snoozeMutation.isPending}
              />
            </div>
          )}

          {/* Scheduled banner — for recurring tasks parked between cycles.
              Makes the gap-state obvious, shows when the next occurrence
              will fire, and exposes the two actions users need here:
              Run Now (fire immediately) and Edit recurrence. */}
          {task.status_category === 'scheduled' && task.due_date && (
            <ScheduledBanner
              task={task}
              onRunNow={() => runNowMutation.mutate()}
              onEditRecurrence={(patch) => updateMutation.mutate(patch)}
              isRunning={runNowMutation.isPending}
              isSaving={updateMutation.isPending}
            />
          )}

          {/* Progress — placed directly under the workflow pipeline so the
              progress view tracks the workflow state visually. */}
          {task.percent_complete != null && (
            <ProgressSlider
              value={task.percent_complete}
              onChange={(val) => updateMutation.mutate({ percent_complete: val })}
              disabled={updateMutation.isPending}
            />
          )}

          {/* Details — consolidated Jira-style */}
          <div className="card p-4 space-y-3">
            <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">Details</h3>

            {/* Status — Jira-style colored pill.
                Only show this legacy pill for tasks WITHOUT a workflow. Workflow-backed
                tasks get their status display + transitions from the WorkflowStatusBar
                above, and this legacy STATUS_OPTIONS list doesn't know about workflow
                states like `done`/`awaiting_merge`/`verification`/etc. — without the
                guard it would always fall back to STATUS_OPTIONS[0] ('Pending'),
                causing the confusing "status shows Pending on a Done task" bug. */}
            {!task.workflow_name && (
              <div className="flex items-center justify-between gap-4">
                <span className="text-sm text-gray-500 dark:text-gray-400 flex-shrink-0">Status</span>
                <div className="relative" data-dropdown>
                  <button
                    onClick={() => { setShowStatusMenu(!showStatusMenu); setShowPriorityMenu(false) }}
                    disabled={updateMutation.isPending || statusTransitionMutation.isPending}
                    className={cn(
                      'px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wide cursor-pointer transition-opacity',
                      (STATUS_OPTIONS.find(o => o.value === task.status) || STATUS_OPTIONS[0]).bg,
                      (STATUS_OPTIONS.find(o => o.value === task.status) || STATUS_OPTIONS[0]).text,
                      (updateMutation.isPending || statusTransitionMutation.isPending) && 'opacity-50'
                    )}
                  >
                    {(STATUS_OPTIONS.find(o => o.value === task.status) || STATUS_OPTIONS[0]).label}
                  </button>
                  {showStatusMenu && (
                    <div className="absolute right-0 z-40 mt-1.5 w-44 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg py-1">
                      {STATUS_OPTIONS.map(opt => (
                        <button
                          key={opt.value}
                          onClick={() => {
                            handleFieldChange('status', opt.value)
                            setShowStatusMenu(false)
                          }}
                          className={cn(
                            'w-full flex items-center gap-2.5 px-3 py-2 text-sm transition-colors',
                            task.status === opt.value
                              ? 'bg-gray-50 dark:bg-gray-700/50'
                              : 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
                          )}
                        >
                          <span className={cn('px-2 py-0.5 rounded-full text-[10px] font-bold uppercase', opt.bg, opt.text)}>
                            {opt.label}
                          </span>
                          {task.status === opt.value && (
                            <CheckCircle size={14} className="text-brand-500 ml-auto" />
                          )}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Priority — Jira-style icon + label */}
            <div className="flex items-center justify-between gap-4">
              <span className="text-sm text-gray-500 dark:text-gray-400 flex-shrink-0">Priority</span>
              <div className="relative" data-dropdown>
                {(() => {
                  const p = PRIORITY_OPTIONS.find(o => o.value === task.priority) || PRIORITY_OPTIONS[1]
                  return (
                    <button
                      onClick={() => { setShowPriorityMenu(!showPriorityMenu); setShowStatusMenu(false) }}
                      disabled={updateMutation.isPending}
                      className={cn(
                        'flex items-center gap-1.5 px-2 py-1 rounded-md text-sm font-medium cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors',
                        p.text,
                        updateMutation.isPending && 'opacity-50'
                      )}
                    >
                      <span className="text-xs leading-none">{p.icon}</span>
                      {p.label}
                    </button>
                  )
                })()}
                {showPriorityMenu && (
                  <div className="absolute right-0 z-40 mt-1.5 w-40 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg py-1">
                    {PRIORITY_OPTIONS.map(opt => (
                      <button
                        key={opt.value}
                        onClick={() => {
                          handleFieldChange('priority', opt.value)
                          setShowPriorityMenu(false)
                        }}
                        className={cn(
                          'w-full flex items-center gap-2.5 px-3 py-2 text-sm transition-colors',
                          task.priority === opt.value
                            ? 'bg-gray-50 dark:bg-gray-700/50'
                            : 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
                        )}
                      >
                        <span className={cn('text-xs leading-none', opt.text)}>{opt.icon}</span>
                        <span className={cn('font-medium', opt.text)}>{opt.label}</span>
                        {task.priority === opt.value && (
                          <CheckCircle size={14} className="text-brand-500 ml-auto" />
                        )}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="border-t border-gray-100 dark:border-gray-800" />

            {/* Assignee */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-sm text-gray-500 dark:text-gray-400">Assignee</span>
                {assignMutation.isPending && (
                  <span className="text-xs text-gray-400">Saving...</span>
                )}
              </div>
              <UserSearchInput
                projectId={task.project}
                organizationId={taskOrganization.organization_id}
                organizationSlug={taskOrganization.organization_slug}
                organizationName={taskOrganization.organization_name}
                onSelect={(member) => {
                  if (member === null) {
                    assignMutation.mutate({ assignee_id: null, assignee_email: null, assignment_group: null })
                  } else if (member.is_team) {
                    assignMutation.mutate({
                      assignment_group: member.team_key || member.team_name,
                    })
                  } else {
                    assignMutation.mutate({
                      assignee_id: member.user_id,
                      assignee_email: member.user_email,
                    })
                  }
                }}
                currentValue={task.assignee_email ? {
                  id: task.assignee_id,
                  email: task.assignee_email,
                  name: assigneeDisplayName(task),
                } : null}
                placeholder="Search members..."
                disabled={assignMutation.isPending}
              />
              {assigneeRosterTeamLabels.length > 0 && (
                <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Assignee team: {assigneeRosterTeamLabels.join(', ')}
                </div>
              )}
              {task.assignment_group && (
                <div className={cn(
                  'mt-2 rounded-lg border px-3 py-2 text-xs',
                  assigneeGroupMismatch
                    ? 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800/60 dark:bg-amber-900/20 dark:text-amber-300'
                    : 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'
                )}>
                  <div className="flex items-center justify-between gap-2">
                    <span>Team</span>
                    <span className="font-medium text-right">{assignmentTeamLabel}</span>
                  </div>
                  {assigneeGroupMismatch && (
                    <div className="mt-1">
                      Assignee is not an active member of this team; this may be a direct-assignment routing mismatch.
                    </div>
                  )}
                </div>
              )}
              {task.assignee_health && (
                <div className={`mt-1 text-xs ${task.assignee_health.is_online ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>
                  Agent health: {task.assignee_health.status}
                  {task.assignee_health.last_activity_s != null ? ` · last activity ${Math.round(task.assignee_health.last_activity_s)}s ago` : ''}
                </div>
              )}
            </div>

            {/* Reporter */}
            {task.created_by_email && (
              <div className="flex items-center justify-between">
                <span className="text-sm text-gray-500 dark:text-gray-400">Reporter</span>
                <div className="flex items-center gap-1.5">
                  <div className="w-5 h-5 rounded-full bg-emerald-100 dark:bg-emerald-900 flex items-center justify-center flex-shrink-0">
                    <span className="text-[10px] text-emerald-600 dark:text-emerald-400 font-medium">
                      {(task.created_by_name || task.created_by_email)[0].toUpperCase()}
                    </span>
                  </div>
                  <span className="text-sm text-gray-900 dark:text-gray-100">
                    {task.reporter_group
                      ? `${formatTeamName(task.reporter_group)} (${task.created_by_name || task.created_by_email.split('@')[0]})`
                      : (task.created_by_name || task.created_by_email.split('@')[0])}
                  </span>
                </div>
              </div>
            )}

            <div className="border-t border-gray-100 dark:border-gray-800" />

            {/* Labels — inline-editable chip list (Jira pattern). Each
                chip has an X to remove; last cell is an add-input that
                appends on Enter. Stored as comma-separated tags. */}
            <LabelsRow
              task={task}
              onSave={(newTags) => updateMutation.mutate({ tags: newTags })}
              isSaving={updateMutation.isPending}
            />

            {/* Due Date */}
            <div className="flex items-center justify-between">
              <span className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-1.5">
                <Calendar size={13} />
                Due Date
              </span>
              {isEditingDueDate ? (
                <input
                  type="datetime-local"
                  value={editData.due_date || ''}
                  onChange={(e) => setEditData({ ...editData, due_date: e.target.value })}
                  onBlur={() => {
                    const newDueDate = editData.due_date ? new Date(editData.due_date).toISOString() : null
                    const currentDueDate = task.due_date ? task.due_date.slice(0, 16) : ''
                    if (editData.due_date !== currentDueDate) {
                      updateMutation.mutate({ due_date: newDueDate })
                    }
                    setIsEditingDueDate(false)
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      const newDueDate = editData.due_date ? new Date(editData.due_date).toISOString() : null
                      updateMutation.mutate({ due_date: newDueDate })
                      setIsEditingDueDate(false)
                    } else if (e.key === 'Escape') {
                      setEditData({ ...editData, due_date: task.due_date ? task.due_date.slice(0, 16) : '' })
                      setIsEditingDueDate(false)
                    }
                  }}
                  className="input text-sm py-1 w-auto"
                  autoFocus
                />
              ) : (
                <button
                  onClick={() => setIsEditingDueDate(true)}
                  className={cn(
                    'text-sm hover:bg-gray-100 dark:hover:bg-gray-800 rounded px-2 py-0.5 cursor-pointer',
                    isOverdue ? 'text-red-600 dark:text-red-400 font-medium' : 'text-gray-900 dark:text-gray-100'
                  )}
                  title="Click to edit due date"
                >
                  {task.due_date ? formatSmartDate(task.due_date) : 'None'}
                </button>
              )}
            </div>

            {/* Repeats — recurrence rule. Shown for every task (not just
                currently-Scheduled) so the config is always reachable,
                matching Todoist/Asana/ClickUp's "click the date/repeat row"
                pattern. */}
            <RepeatsRow
              task={task}
              onSave={(patch) => updateMutation.mutate(patch)}
              isSaving={updateMutation.isPending}
            />

            {/* Estimated Time */}
            {task.estimated_hours && (
              <div className="flex items-center justify-between">
                <span className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-1.5">
                  <Clock size={13} />
                  Estimate
                </span>
                <span className="text-sm text-gray-900 dark:text-gray-100">
                  {task.estimated_hours}h
                </span>
              </div>
            )}

            {/* Created */}
            <div className="flex items-center justify-between">
              <span className="text-sm text-gray-500 dark:text-gray-400">Created</span>
              <span className="text-sm text-gray-600 dark:text-gray-400">
                {formatRelativeTime(task.created_at)}
              </span>
            </div>
          </div>

          {/* Planning — Project, Sprint, Components, Versions */}
          <div className="card p-4 space-y-3">
            <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">Planning</h3>

            {/* Project */}
            <div>
              <span className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-1.5 mb-1">
                <FolderKanban size={13} />
                Project
              </span>
              <div className="relative" data-dropdown>
                <button
                  onClick={() => setShowProjectDropdown(!showProjectDropdown)}
                  className="w-full flex items-center gap-2 p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-left"
                >
                  {task.project ? (
                    <>
                      <div className="w-6 h-6 rounded bg-indigo-100 dark:bg-indigo-900 flex items-center justify-center flex-shrink-0">
                        <span className="text-xs text-indigo-600 dark:text-indigo-400 font-medium">
                          {(task.project_key || '?')[0]}
                        </span>
                      </div>
                      <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                        {task.project_name || task.project_key}
                      </span>
                    </>
                  ) : (
                    <span className="text-sm text-gray-400">No project</span>
                  )}
                  <ChevronDown size={14} className="ml-auto text-gray-400 flex-shrink-0" />
                </button>

                {showProjectDropdown && (
                  <div className="absolute z-40 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-60 overflow-y-auto">
                    {sameOrganizationProjects.length === 0 ? (
                      <div className="p-3 text-sm text-gray-500 dark:text-gray-400 text-center">
                        No projects available in this organization
                      </div>
                    ) : (
                      sameOrganizationProjects.map((project) => (
                        <button
                          key={project.id}
                          onClick={() => {
                            if (project.id !== task.project) {
                              updateMutation.mutate({ project: project.id })
                            }
                            setShowProjectDropdown(false)
                          }}
                          className={cn(
                            'w-full flex items-center gap-2 p-2.5 transition-colors text-sm',
                            project.id === task.project
                              ? 'bg-brand-50 dark:bg-brand-900/20'
                              : 'hover:bg-gray-100 dark:hover:bg-gray-700'
                          )}
                        >
                          <div className="w-5 h-5 rounded bg-indigo-100 dark:bg-indigo-900 flex items-center justify-center">
                            <span className="text-[10px] text-indigo-600 dark:text-indigo-400 font-medium">
                              {(project.key || '?')[0]}
                            </span>
                          </div>
                          <span className="text-gray-900 dark:text-white truncate">
                            {project.name}
                          </span>
                          {project.id === task.project && (
                            <CheckCircle size={14} className="text-brand-500 flex-shrink-0 ml-auto" />
                          )}
                        </button>
                      ))
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* Sprint & Story Points */}
            {task.project && (
              <>
                <div className="border-t border-gray-100 dark:border-gray-800 pt-3">
                  <SprintSelector
                    projectId={task.project}
                    currentSprintId={task.sprint}
                    currentStoryPoints={task.story_points}
                    onSprintChange={(sprintId) => updateMutation.mutate({ sprint: sprintId })}
                    onStoryPointsChange={(points) => updateMutation.mutate({ story_points: points })}
                    disabled={updateMutation.isPending}
                  />
                </div>

                {/* Component, Fix Version, Affects Versions — only for dev/bug/security workflows */}
                {['software-dev', 'bug', 'security-finding'].includes(task.workflow_name) && (
                  <div className="border-t border-gray-100 dark:border-gray-800 pt-3 space-y-3">
                    <ComponentSelector
                      projectId={task.project}
                      value={task.component}
                      onChange={(componentId) => updateMutation.mutate({ component: componentId })}
                      disabled={updateMutation.isPending}
                    />
                    <VersionSelector
                      projectId={task.project}
                      value={task.fix_version}
                      onChange={(versionId) => updateMutation.mutate({ fix_version: versionId })}
                      disabled={updateMutation.isPending}
                      label="Fix Version"
                    />
                    <VersionSelector
                      projectId={task.project}
                      value={task.affects_versions || []}
                      onChange={(versionIds) => updateMutation.mutate({ affects_versions: versionIds })}
                      disabled={updateMutation.isPending}
                      multiple={true}
                      label="Affects Versions"
                    />
                  </div>
                )}
              </>
            )}
          </div>

          {/* Time Tracking */}
          <div className="card p-4">
            <TimeTrackingDisplay
              originalEstimate={task.original_estimate_minutes}
              timeSpent={task.time_spent_minutes}
              remainingEstimate={task.remaining_estimate_minutes}
              onLogWork={() => setShowWorkLogModal(true)}
            />
          </div>

          {/* Development — Pull Requests */}
          <PullRequests
            task={task}
            alwaysShow={['software-dev', 'bug', 'security-finding'].includes(task.workflow_name)}
          />

          {/* Watchers */}
          {watchersData?.watchers?.length > 0 && (
            <div className="card p-4">
              <span className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider flex items-center gap-1.5">
                <Eye size={12} />
                Watchers ({watchersData.count})
              </span>
              <div className="flex -space-x-2 mt-2">
                {watchersData.watchers.slice(0, 5).map((watcher) => (
                  <div
                    key={watcher.id}
                    className="w-7 h-7 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center border-2 border-white dark:border-gray-900"
                    title={watcher.user_name || watcher.user_email}
                  >
                    <span className="text-[10px] text-brand-600 dark:text-brand-400 font-medium">
                      {(watcher.user_name || watcher.user_email)[0].toUpperCase()}
                    </span>
                  </div>
                ))}
                {watchersData.count > 5 && (
                  <div className="w-7 h-7 rounded-full bg-gray-200 dark:bg-gray-700 flex items-center justify-center border-2 border-white dark:border-gray-900">
                    <span className="text-[10px] text-gray-600 dark:text-gray-400">
                      +{watchersData.count - 5}
                    </span>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Work Log Modal */}
      <WorkLogModal
        itemId={task.id}
        itemKey={task.item_key}
        currentTimeSpent={task.time_spent_minutes || 0}
        remainingEstimate={task.remaining_estimate_minutes}
        isOpen={showWorkLogModal}
        onClose={() => setShowWorkLogModal(false)}
      />

      {/* Mark as Duplicate Modal */}
      {showDuplicateModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={(e) => { if (e.target === e.currentTarget) setShowDuplicateModal(false) }}
        >
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full mx-4 p-5">
            <div className="flex items-center gap-2 mb-3">
              <Copy size={18} className="text-brand-500" />
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                Mark as Duplicate
              </h3>
            </div>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              This will close <strong>{task.item_key || `#${task.id}`}</strong> as a duplicate of the
              target task and create a "duplicates" link between them.
            </p>

            <form
              onSubmit={(e) => {
                e.preventDefault()
                if (!duplicateTarget) return
                markDuplicateMutation.mutate({
                  target: duplicateTarget,
                  comment: duplicateComment.trim(),
                })
              }}
              className="space-y-3"
            >
              <div>
                <label className="text-sm text-gray-700 dark:text-gray-300 mb-1 block">
                  Duplicate of
                </label>
                <ItemPicker
                  value={duplicateTarget}
                  onChange={setDuplicateTarget}
                  excludeId={task.id}
                  placeholder="Search by key or title…"
                  autoFocus
                />
              </div>
              <div>
                <label className="text-sm text-gray-700 dark:text-gray-300 mb-1 block">
                  Reason (optional)
                </label>
                <textarea
                  value={duplicateComment}
                  onChange={(e) => setDuplicateComment(e.target.value)}
                  placeholder="Why is this a duplicate?"
                  rows={3}
                  className="input w-full text-sm resize-none"
                />
              </div>
              <div className="flex items-center justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowDuplicateModal(false)}
                  className="btn-secondary"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={!duplicateTarget || markDuplicateMutation.isPending}
                  className="btn-primary"
                >
                  {markDuplicateMutation.isPending ? 'Marking...' : 'Mark as Duplicate'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

// --------------------------------------------------------------------------
// ScheduledBanner — rendered when a recurring task is parked in Scheduled.
//
// Shows the next fire time, the humanized recurrence, and exposes two actions
// users need here: Run Now (fire the cycle immediately, skipping the timer)
// and Edit recurrence (change frequency/interval inline so a "every 4 hours"
// task can become "every 6 hours" without leaving the page).
// --------------------------------------------------------------------------
// --------------------------------------------------------------------------
// LabelsRow — inline chip list for task.tags. Jira pattern: each chip has
// an X to remove, plus an add-input at the end that appends on Enter.
// Tags are stored as a comma-separated string on the server.
// --------------------------------------------------------------------------
function LabelsRow({ task, onSave, isSaving }) {
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState('')

  const labels = (task.tags || '')
    .split(',')
    .map(t => t.trim())
    .filter(Boolean)

  const commit = (next) => {
    onSave(next.join(','))
  }

  const removeLabel = (label) => {
    commit(labels.filter(l => l !== label))
  }

  const addLabel = () => {
    // Jira: labels can't contain spaces — substitute hyphens.
    const sanitized = draft.trim().toLowerCase().replace(/\s+/g, '-')
    if (!sanitized) {
      setAdding(false)
      setDraft('')
      return
    }
    if (!labels.includes(sanitized)) {
      commit([...labels, sanitized])
    }
    setDraft('')
    setAdding(false)
  }

  return (
    <div>
      <span className="text-sm text-gray-500 dark:text-gray-400 mb-2 flex items-center gap-1.5">
        <Tag size={13} />
        Labels
      </span>
      <div className="flex flex-wrap gap-1 items-center mt-1.5">
        {labels.map((label) => (
          <span
            key={label}
            className="badge badge-gray text-xs inline-flex items-center gap-1 pl-2 pr-1"
          >
            {label}
            <button
              type="button"
              onClick={() => removeLabel(label)}
              disabled={isSaving}
              className="p-0.5 rounded hover:bg-gray-300/60 dark:hover:bg-gray-600/60 text-gray-500 hover:text-gray-700 disabled:opacity-40"
              title={`Remove "${label}"`}
            >
              <X size={10} />
            </button>
          </span>
        ))}
        {adding ? (
          <input
            autoFocus
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={addLabel}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); addLabel() }
              else if (e.key === 'Escape') { setDraft(''); setAdding(false) }
            }}
            placeholder="label-name"
            className="text-xs px-1.5 py-0.5 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 focus:outline-none focus:ring-1 focus:ring-brand-400 w-32"
          />
        ) : (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="text-xs px-2 py-0.5 rounded border border-dashed border-gray-300 dark:border-gray-600 text-gray-500 dark:text-gray-400 hover:border-brand-400 hover:text-brand-600"
            title="Add a label"
          >
            + Add
          </button>
        )}
      </div>
    </div>
  )
}


// --------------------------------------------------------------------------
// RepeatsRow — inline recurrence display + editor in the Details sidebar.
// Matches Todoist/Asana: click the value to edit, no separate action button.
// Shown for every task so recurring config is reachable from any state.
// --------------------------------------------------------------------------
function RepeatsRow({ task, onSave, isSaving }) {
  const [editing, setEditing] = useState(false)
  const humanized = task.is_recurring
    ? (formatRecurrence(task.frequency, task.recurrence_pattern) || 'Recurring')
    : null

  if (editing) {
    return (
      <div className="space-y-1">
        <span className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-1.5">
          <Repeat size={13} />
          Repeats
        </span>
        <RecurrenceEditor
          task={task}
          isSaving={isSaving}
          onCancel={() => setEditing(false)}
          onSave={(patch) => { onSave(patch); setEditing(false) }}
        />
      </div>
    )
  }

  return (
    <div className="flex items-center justify-between">
      <span className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-1.5">
        <Repeat size={13} />
        Repeats
      </span>
      <button
        onClick={() => setEditing(true)}
        className={cn(
          'text-sm hover:bg-gray-100 dark:hover:bg-gray-800 rounded px-2 py-0.5 cursor-pointer',
          humanized ? 'text-gray-900 dark:text-gray-100' : 'text-gray-500 dark:text-gray-500 italic'
        )}
        title="Click to change recurrence"
      >
        {humanized || 'Does not repeat'}
      </button>
    </div>
  )
}


function ScheduledBanner({ task, onRunNow, onEditRecurrence, isRunning, isSaving }) {
  const [editing, setEditing] = useState(false)
  const humanized = formatRecurrence(task.frequency, task.recurrence_pattern)

  return (
    <div className="card p-4 bg-violet-50 dark:bg-violet-900/20 border border-violet-200 dark:border-violet-800">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Repeat size={16} className="text-violet-600 dark:text-violet-400 flex-shrink-0" />
          <div className="text-sm font-medium text-violet-900 dark:text-violet-100 truncate">
            Waiting for next occurrence
          </div>
        </div>
        <button
          onClick={() => setEditing(v => !v)}
          className="flex-shrink-0 text-xs px-2 py-1 rounded text-violet-700 dark:text-violet-300 hover:bg-violet-100 dark:hover:bg-violet-800/40"
          title="Change the recurrence interval"
        >
          {editing ? 'Cancel' : 'Edit'}
        </button>
      </div>

      <div className="mt-2 space-y-0.5 text-xs text-violet-700 dark:text-violet-300 pl-6">
        <div>
          <span className="font-medium text-violet-800 dark:text-violet-200">Next fire: </span>
          {new Date(task.due_date).toLocaleString(undefined, {
            weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit',
          })}
        </div>
        {humanized && <div className="opacity-80">{humanized}</div>}
      </div>

      {!editing && (
        <div className="mt-3 pl-6">
          <button
            onClick={onRunNow}
            disabled={isRunning}
            className="w-full sm:w-auto inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md bg-violet-600 hover:bg-violet-700 text-white text-sm font-medium disabled:opacity-50 transition-colors"
            title="Fire the next cycle now instead of waiting for the timer"
          >
            <PlayCircle size={14} />
            {isRunning ? 'Starting…' : 'Run Now'}
          </button>
        </div>
      )}

      {editing && (
        <div className="mt-3 pl-6">
          <RecurrenceEditor
            task={task}
            isSaving={isSaving}
            onCancel={() => setEditing(false)}
            onSave={(patch) => { onEditRecurrence(patch); setEditing(false) }}
          />
        </div>
      )}
    </div>
  )
}
