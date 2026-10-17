import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Bell,
  BellOff,
  CheckCheck,
  Clock,
  AlertTriangle,
  Zap,
  UserPlus,
  MessageSquare,
  Info,
  Filter,
  X,
  Trash2,
} from 'lucide-react'
import { notificationsApi } from '../services/api'
import EmptyState from '../components/EmptyState'
import { PageLoader } from '../components/LoadingSpinner'
import { cn } from '../utils/cn'

const TYPE_CONFIG = {
  reminder:      { icon: Clock,          color: 'text-teal-500',   bg: 'bg-teal-50 dark:bg-teal-950',   label: 'Reminder' },
  overdue:       { icon: AlertTriangle,  color: 'text-red-500',    bg: 'bg-red-50 dark:bg-red-950',     label: 'Overdue' },
  alert:         { icon: AlertTriangle,  color: 'text-orange-500', bg: 'bg-orange-50 dark:bg-orange-950', label: 'Alert' },
  action:        { icon: Zap,            color: 'text-amber-500',  bg: 'bg-amber-50 dark:bg-amber-950', label: 'Action' },
  assignment:    { icon: UserPlus,       color: 'text-blue-500',   bg: 'bg-blue-50 dark:bg-blue-950',   label: 'Assignment' },
  comment:       { icon: MessageSquare,  color: 'text-purple-500', bg: 'bg-purple-50 dark:bg-purple-950', label: 'Comment' },
  status_change: { icon: Info,           color: 'text-indigo-500', bg: 'bg-indigo-50 dark:bg-indigo-950', label: 'Status' },
  system:        { icon: Bell,           color: 'text-gray-500',   bg: 'bg-gray-50 dark:bg-gray-800',   label: 'System' },
}

const TYPE_FILTERS = [
  { value: '', label: 'All' },
  { value: 'reminder', label: 'Reminders' },
  { value: 'overdue', label: 'Overdue' },
  { value: 'alert', label: 'Alerts' },
  { value: 'action', label: 'Actions' },
  { value: 'assignment', label: 'Assignments' },
]

function timeAgo(dateStr) {
  const now = new Date()
  const date = new Date(dateStr)
  const diffMs = now - date
  const mins = Math.floor(diffMs / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 7) return `${days}d ago`
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export default function Notifications() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [typeFilter, setTypeFilter] = useState('')
  const [readFilter, setReadFilter] = useState('') // '', 'true', 'false'

  const params = {}
  if (typeFilter) params.type = typeFilter
  if (readFilter) params.is_read = readFilter

  const { data, isLoading } = useQuery({
    queryKey: ['notifications', params],
    queryFn: () => notificationsApi.list(params).then(r => r.data),
    refetchInterval: 30000,
  })

  const invalidateNotifications = () => {
    queryClient.invalidateQueries({ queryKey: ['notifications'] })
    queryClient.invalidateQueries({ queryKey: ['unreadCount'] })
  }

  const markReadMutation = useMutation({
    mutationFn: (id) => notificationsApi.read(id),
    onSuccess: invalidateNotifications,
  })

  const markAllReadMutation = useMutation({
    mutationFn: () => notificationsApi.readAll(),
    onSuccess: invalidateNotifications,
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => notificationsApi.delete(id),
    onSuccess: invalidateNotifications,
  })

  const clearAllMutation = useMutation({
    mutationFn: () => notificationsApi.clearAll(),
    onSuccess: invalidateNotifications,
  })

  const notifications = data?.results || data || []
  const hasUnread = notifications.some(n => !n.is_read)

  const handleClick = (notification) => {
    // Mark as read
    if (!notification.is_read) {
      markReadMutation.mutate(notification.id)
    }
    // Navigate to the related item if available
    if (notification.item_key) {
      navigate(`/tasks/${notification.item_key}`)
    } else if (notification.item) {
      navigate(`/tasks/${notification.item}`)
    }
  }

  if (isLoading) return <PageLoader />

  return (
    <div className="space-y-4 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Notifications</h1>
          <p className="text-gray-500 dark:text-gray-400">
            Your notification history
          </p>
        </div>
        <div className="flex items-center gap-2">
          {hasUnread && (
            <button
              onClick={() => markAllReadMutation.mutate()}
              disabled={markAllReadMutation.isPending}
              className="btn-secondary flex items-center gap-2 text-sm"
            >
              <CheckCheck size={16} />
              Mark all read
            </button>
          )}
          {notifications.length > 0 && (
            <button
              onClick={() => clearAllMutation.mutate()}
              disabled={clearAllMutation.isPending}
              className="btn-secondary flex items-center gap-2 text-sm text-red-600 dark:text-red-400 border-red-200 dark:border-red-800 hover:bg-red-50 dark:hover:bg-red-900/30"
            >
              <Trash2 size={16} />
              {clearAllMutation.isPending ? 'Clearing...' : 'Clear all'}
            </button>
          )}
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <Filter size={16} className="text-gray-400" />
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="input w-auto text-sm"
        >
          {TYPE_FILTERS.map(f => (
            <option key={f.value} value={f.value}>{f.label}</option>
          ))}
        </select>
        <select
          value={readFilter}
          onChange={(e) => setReadFilter(e.target.value)}
          className="input w-auto text-sm"
        >
          <option value="">All</option>
          <option value="false">Unread</option>
          <option value="true">Read</option>
        </select>
      </div>

      {/* Notification list */}
      {notifications.length > 0 ? (
        <div className="space-y-2">
          {notifications.map((n) => {
            const config = TYPE_CONFIG[n.notification_type] || TYPE_CONFIG.system
            const Icon = config.icon
            return (
              <div
                key={n.id}
                className={cn(
                  'group relative rounded-xl border p-4 transition-colors cursor-pointer',
                  n.is_read
                    ? 'bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800'
                    : 'bg-brand-50/50 dark:bg-brand-950/30 border-brand-200 dark:border-brand-800',
                  'hover:bg-gray-50 dark:hover:bg-gray-800/60'
                )}
                onClick={() => handleClick(n)}
              >
                <div className="flex gap-3">
                  {/* Icon */}
                  <div className={cn(
                    'flex-shrink-0 w-10 h-10 rounded-full flex items-center justify-center',
                    config.bg
                  )}>
                    <Icon size={18} className={config.color} />
                  </div>

                  {/* Content */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-start justify-between gap-2">
                      <p className={cn(
                        'text-sm',
                        n.is_read
                          ? 'text-gray-700 dark:text-gray-300'
                          : 'text-gray-900 dark:text-gray-100 font-semibold'
                      )}>
                        {n.title}
                      </p>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        <span className="text-xs text-gray-400 dark:text-gray-500 whitespace-nowrap">
                          {timeAgo(n.created_at)}
                        </span>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            deleteMutation.mutate(n.id)
                          }}
                          className="p-1 rounded opacity-0 group-hover:opacity-100 hover:bg-gray-200 dark:hover:bg-gray-700 transition-opacity"
                          title="Dismiss"
                        >
                          <X size={14} className="text-gray-400" />
                        </button>
                      </div>
                    </div>

                    {n.message && (
                      <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">
                        {n.message}
                      </p>
                    )}

                    <div className="flex items-center gap-2 mt-1.5">
                      <span className={cn(
                        'text-xs px-2 py-0.5 rounded-full',
                        config.bg, config.color
                      )}>
                        {config.label}
                      </span>
                      {n.item_key && (
                        <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">
                          {n.item_key}
                        </span>
                      )}
                      {!n.is_read && (
                        <span className="w-2 h-2 rounded-full bg-brand-500 flex-shrink-0" />
                      )}
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      ) : (
        <EmptyState
          icon={BellOff}
          title="No notifications"
          description={
            typeFilter || readFilter
              ? 'No notifications match your filters.'
              : "You're all caught up! Notifications from reminders, overdue tasks, and alerts will appear here."
          }
        />
      )}
    </div>
  )
}
