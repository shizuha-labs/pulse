import { useNavigate } from 'react-router-dom'
import {
  CheckCircle,
  Circle,
  Clock,
  AlertTriangle,
  Zap,
  MoreVertical,
  Check,
  X,
  Bell,
  BellOff,
  User,
} from 'lucide-react'
import { cn } from '../utils/cn'
import {
  formatSmartDate,
  formatRelativeTime,
  getSeverityClasses,
  getStatusClasses,
  getPriorityClasses,
  formatStatus,
} from '../utils/format'
import { getEntityBadge, getEntityBadgeTitle } from '../utils/entityKey'
import { useOrganization } from '../contexts/OrganizationContext'
import { formatOrganizationName, formatTeamName } from '../utils/teamLabels'


const healthDotClass = (health) => {
  const status = health?.status
  if (status === 'active') return 'bg-emerald-500 ring-emerald-100'
  if (['absent', 'disabled', 'wedged', 'provider_down'].includes(status)) return 'bg-red-500 ring-red-100'
  if (status) return 'bg-amber-500 ring-amber-100'
  return 'bg-gray-400 ring-gray-100'
}

const healthTitle = (health, fallback) => {
  if (!health) return fallback
  return `${fallback} · agent ${health.status}${health.last_activity_s != null ? ` · last activity ${Math.round(health.last_activity_s)}s ago` : ''}`
}

const EntityBadge = ({ badge, title }) => {
  if (!badge?.key) return null
  return (
    <span
      className="inline-flex h-5 min-w-[1.75rem] flex-shrink-0 items-center justify-center rounded border border-brand-200 bg-brand-50 px-1.5 text-[10px] font-bold leading-none text-brand-700 dark:border-brand-800 dark:bg-brand-950/60 dark:text-brand-300"
      title={title || `Entity ${badge.key}`}
    >
      {badge.key}
    </span>
  )
}

/**
 * Card component for displaying items (tasks, actions, alerts)
 */
// Get initials from email or name
const getInitials = (email, name) => {
  if (name) {
    const parts = name.split(' ')
    if (parts.length >= 2) {
      return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
    }
    return name.substring(0, 2).toUpperCase()
  }
  if (email) {
    return email.substring(0, 2).toUpperCase()
  }
  return '??'
}

// Generate a consistent color based on string
const getAvatarColor = (str) => {
  if (!str) return 'bg-gray-400'
  const colors = [
    'bg-blue-500', 'bg-green-500', 'bg-purple-500', 'bg-pink-500',
    'bg-indigo-500', 'bg-teal-500', 'bg-orange-500', 'bg-cyan-500',
  ]
  let hash = 0
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash)
  }
  return colors[Math.abs(hash) % colors.length]
}

export default function ItemCard({
  item,
  onComplete,
  onApprove,
  onReject,
  onAcknowledge,
  onResolve,
  onSilence,
  onClick,
  showMode = false,
  compact = false,
  // Selection props
  selectable = false,
  isSelected = false,
  onToggleSelect,
  isFocused = false,
  // Navigation - if true, clicking navigates to detail page instead of calling onClick
  navigateOnClick = false,
}) {
  const navigate = useNavigate()
  const { organizations, currentOrganization, isLoading: isOrganizationLoading } = useOrganization()
  const {
    mode, status, severity, priority, title, description, due_date, occurrence_count,
    // Jira-like fields
    item_key, project, project_key, project_name,
    assignee_email, assignee_id, assignee_name, assignee_health,
    assignment_group, source,
  } = item

  const statusClasses = getStatusClasses(status)
  const severityClasses = severity ? getSeverityClasses(severity) : null
  const priorityClasses = getPriorityClasses(priority)
  const entityBadge = getEntityBadge(item)
  const entityBadgeTitle = getEntityBadgeTitle(item, entityBadge)
  const organization = organizations.find(candidate => (
    item.organization_id !== undefined
    && item.organization_id !== null
    && String(candidate.id) === String(item.organization_id)
  ))
  const organizationLabel = formatOrganizationName({
    organization_name: item.organization_name || organization?.name,
    organization_slug: item.organization_slug || organization?.slug,
  })
  const showOrganization = !isOrganizationLoading && !currentOrganization && Boolean(organizationLabel)

  // Determine the icon based on mode and status
  const getIcon = () => {
    if (mode === 'task') {
      if (status === 'completed') return <CheckCircle className="text-green-500" size={20} />
      if (status === 'in_progress') return <Clock className="text-blue-500" size={20} />
      return <Circle className="text-gray-400" size={20} />
    }
    if (mode === 'action') {
      return <Zap className={cn('text-amber-500', status === 'pending' && 'animate-pulse')} size={20} />
    }
    if (mode === 'alert') {
      return (
        <AlertTriangle
          className={cn(
            severity === 'critical' && 'text-red-500',
            severity === 'error' && 'text-orange-500',
            severity === 'warning' && 'text-yellow-500',
            severity === 'info' && 'text-blue-500',
            status === 'firing' && 'animate-pulse-slow'
          )}
          size={20}
        />
      )
    }
    return <Circle className="text-gray-400" size={20} />
  }

  // Quick actions based on mode and status
  const renderActions = () => {
    if (compact) return null

    return (
      <div className="flex items-center gap-1 ml-2">
        {/* Task actions */}
        {mode === 'task' && status !== 'completed' && onComplete && (
          <button
            onClick={(e) => { e.stopPropagation(); onComplete(item) }}
            className="btn-icon p-1.5 hover:bg-green-100 dark:hover:bg-green-900/30"
            title="Complete"
          >
            <Check size={16} className="text-green-600 dark:text-green-400" />
          </button>
        )}

        {/* Action actions */}
        {mode === 'action' && status === 'pending' && (
          <>
            {onApprove && (
              <button
                onClick={(e) => { e.stopPropagation(); onApprove(item) }}
                className="btn-icon p-1.5 hover:bg-green-100 dark:hover:bg-green-900/30"
                title="Approve"
              >
                <Check size={16} className="text-green-600 dark:text-green-400" />
              </button>
            )}
            {onReject && (
              <button
                onClick={(e) => { e.stopPropagation(); onReject(item) }}
                className="btn-icon p-1.5 hover:bg-red-100 dark:hover:bg-red-900/30"
                title="Reject"
              >
                <X size={16} className="text-red-600 dark:text-red-400" />
              </button>
            )}
          </>
        )}

        {/* Alert actions */}
        {mode === 'alert' && status === 'firing' && (
          <>
            {onAcknowledge && (
              <button
                onClick={(e) => { e.stopPropagation(); onAcknowledge(item) }}
                className="btn-icon p-1.5 hover:bg-yellow-100 dark:hover:bg-yellow-900/30"
                title="Acknowledge"
              >
                <Bell size={16} className="text-yellow-600 dark:text-yellow-400" />
              </button>
            )}
            {onSilence && (
              <button
                onClick={(e) => { e.stopPropagation(); onSilence(item) }}
                className="btn-icon p-1.5 hover:bg-gray-100 dark:hover:bg-gray-700"
                title="Silence"
              >
                <BellOff size={16} className="text-gray-600 dark:text-gray-400" />
              </button>
            )}
          </>
        )}

        {mode === 'alert' && (status === 'firing' || status === 'acknowledged') && onResolve && (
          <button
            onClick={(e) => { e.stopPropagation(); onResolve(item) }}
            className="btn-icon p-1.5 hover:bg-green-100 dark:hover:bg-green-900/30"
            title="Resolve"
          >
            <CheckCircle size={16} className="text-green-600 dark:text-green-400" />
          </button>
        )}
      </div>
    )
  }

  const handleCheckboxChange = (e) => {
    e.stopPropagation()
    onToggleSelect?.(item.id, e.nativeEvent.shiftKey)
  }

  const handleCardClick = (e) => {
    // If selectable and clicking the card (not action buttons), toggle selection
    if (selectable && onToggleSelect) {
      onToggleSelect(item.id, e.shiftKey)
    } else if (navigateOnClick && item_key) {
      // Navigate to task detail page
      navigate(`/tasks/${item_key}`)
    } else if (navigateOnClick && item.id) {
      // Fallback to ID-based navigation if no item_key
      navigate(`/tasks/${item.id}`)
    } else {
      onClick?.(item)
    }
  }

  return (
    <div
      onClick={handleCardClick}
      className={cn(
        'card p-4 hover:shadow-md transition-all cursor-pointer',
        mode === 'alert' && status === 'firing' && severityClasses?.border && `border-l-4 ${severityClasses.border}`,
        status === 'completed' && 'opacity-60',
        status === 'silenced' && 'opacity-50',
        // Selection styles
        isSelected && 'ring-2 ring-brand-500 bg-brand-50/50 dark:bg-brand-950/50',
        // Focus styles for keyboard navigation
        isFocused && 'ring-2 ring-brand-400 ring-offset-2 dark:ring-offset-gray-900'
      )}
    >
      <div className="flex items-start gap-3">
        {/* Checkbox for selection mode */}
        {selectable && (
          <div className="flex-shrink-0 mt-0.5">
            <input
              type="checkbox"
              checked={isSelected}
              onChange={handleCheckboxChange}
              onClick={(e) => e.stopPropagation()}
              className={cn(
                'h-4 w-4 rounded border-gray-300 dark:border-gray-600',
                'text-brand-600 focus:ring-brand-500',
                'cursor-pointer'
              )}
            />
          </div>
        )}

        {/* Icon - clickable to toggle complete for tasks */}
        <div
          className={cn(
            'flex-shrink-0 mt-0.5',
            mode === 'task' && onComplete && 'cursor-pointer hover:scale-110 transition-transform'
          )}
          onClick={(e) => {
            if (mode === 'task' && onComplete) {
              e.stopPropagation()
              onComplete(item)
            }
          }}
          title={mode === 'task' && onComplete ? (status === 'completed' ? 'Mark as pending' : 'Mark as complete') : undefined}
        >
          {getIcon()}
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-2">
            <div className="flex-1 min-w-0">
              {/* Issue Key + Title Row */}
              <div className="flex items-center gap-2 mb-0.5">
                <EntityBadge badge={entityBadge} title={entityBadgeTitle} />
                {/* Issue Key (Jira-style: PROJECT-123) - Clickable link */}
                {item_key && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      navigate(`/tasks/${item_key}`)
                    }}
                    className="text-xs font-semibold text-brand-600 dark:text-brand-400 flex-shrink-0 hover:underline"
                  >
                    {item_key}
                  </button>
                )}
                {/* Fallback: show project key + id if no item_key */}
                {!item_key && project_key && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      navigate(`/tasks/${item.id}`)
                    }}
                    className="text-xs font-semibold text-brand-600 dark:text-brand-400 flex-shrink-0 hover:underline"
                  >
                    {project_key}-{item.id}
                  </button>
                )}
                {/* Title */}
                <h3 className={cn(
                  'font-medium text-gray-900 dark:text-gray-100 truncate',
                  status === 'completed' && 'line-through text-gray-500 dark:text-gray-500'
                )}>
                  {title}
                </h3>
              </div>

              {/* Description (if not compact) */}
              {!compact && description && (
                <p className="text-sm text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
                  {description}
                </p>
              )}

              {/* Meta info */}
              <div className="flex flex-wrap items-center gap-2 mt-2">
                {/* Mode badge */}
                {showMode && (
                  <span className={cn('badge', statusClasses.bg, statusClasses.text)}>
                    {mode}
                  </span>
                )}

                {/* Status badge */}
                <span className={cn('badge', statusClasses.bg, statusClasses.text)}>
                  {formatStatus(status)}
                </span>

                {/* Severity badge (for alerts) */}
                {severity && severityClasses && (
                  <span className={cn('badge', severityClasses.bg, severityClasses.text)}>
                    {severity}
                  </span>
                )}

                {/* Priority badge (if not normal) */}
                {priority && priority !== 'normal' && (
                  <span className={cn('badge', priorityClasses.bg, priorityClasses.text)}>
                    {priority}
                  </span>
                )}

                {/* Source badge (for non-manual items) */}
                {source && source !== 'manual' && (
                  <span className="badge bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
                    {source}
                  </span>
                )}

                {mode === 'alert' && assignment_group && (
                  <span
                    className="badge bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300"
                    title={`On-call team: ${formatTeamName(assignment_group)}`}
                  >
                    {formatTeamName(assignment_group)}
                  </span>
                )}

                {/* Project badge */}
                {showOrganization && (
                  <span
                    data-testid="item-organization"
                    className="badge bg-violet-50 text-violet-700 dark:bg-violet-950/50 dark:text-violet-300"
                    title={`Owning organization: ${organizationLabel}`}
                  >
                    {organizationLabel}
                  </span>
                )}

                {project_name && (
                  <span className="badge bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
                    {project_key || project_name}
                  </span>
                )}

                {/* Occurrence count (for alerts) */}
                {occurrence_count && occurrence_count > 1 && (
                  <span className="badge badge-gray">
                    {occurrence_count}x
                  </span>
                )}

                {/* Due date */}
                {due_date && (
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {formatSmartDate(due_date)}
                  </span>
                )}
              </div>
            </div>

            {/* Assignee Avatar + Actions */}
            <div className="flex items-center gap-2 flex-shrink-0">
              {/* Assignee Avatar */}
              {assignee_email && (
                <div
                  className={cn(
                    'relative w-7 h-7 rounded-full flex items-center justify-center text-white text-xs font-medium',
                    getAvatarColor(assignee_name || assignee_email)
                  )}
                  title={healthTitle(assignee_health, assignee_name || assignee_email)}
                >
                  {getInitials(assignee_name || assignee_email)}
                  {assignee_health && (
                    <span className={cn('absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full ring-2', healthDotClass(assignee_health))} />
                  )}
                </div>
              )}
              {/* Unassigned indicator — tasks and alerts. Alerts with no
                  assignee are invisible to pulse_get_my_alerts, so the empty
                  chip must be visible on the Alerts page (not only on tasks). */}
              {!assignee_email && (mode === 'task' || mode === 'alert') && (
                <div
                  className="w-7 h-7 rounded-full flex items-center justify-center bg-gray-200 dark:bg-gray-700 text-gray-400"
                  title={assignment_group ? `Unassigned · ${formatTeamName(assignment_group)} on-call` : 'Unassigned'}
                >
                  <User size={14} />
                </div>
              )}
              {/* Actions */}
              {renderActions()}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
