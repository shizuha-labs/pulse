import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Clock, Calendar, User, Trash2 } from 'lucide-react'
import { workLogsApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { cn } from '../utils/cn'

export default function WorkLogList({ itemId, className = '' }) {
  const queryClient = useQueryClient()
  const { user } = useAuth()

  const { data, isLoading } = useQuery({
    queryKey: ['work-logs', itemId],
    queryFn: async () => {
      const res = await workLogsApi.byItem(itemId)
      return res.data
    },
    enabled: !!itemId,
  })

  const deleteMutation = useMutation({
    mutationFn: (logId) => workLogsApi.delete(logId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['work-logs', itemId] })
      queryClient.invalidateQueries({ queryKey: ['task'] })
    },
  })

  const formatDuration = (minutes) => {
    if (!minutes) return '0m'
    const h = Math.floor(minutes / 60)
    const m = minutes % 60
    if (h && m) return `${h}h ${m}m`
    if (h) return `${h}h`
    return `${m}m`
  }

  const formatDate = (dateStr) => {
    const date = new Date(dateStr)
    const today = new Date()
    const yesterday = new Date(today)
    yesterday.setDate(yesterday.getDate() - 1)

    if (date.toDateString() === today.toDateString()) {
      return 'Today'
    } else if (date.toDateString() === yesterday.toDateString()) {
      return 'Yesterday'
    } else {
      return date.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: date.getFullYear() !== today.getFullYear() ? 'numeric' : undefined,
      })
    }
  }

  if (isLoading) {
    return (
      <div className={cn('text-sm text-gray-500 dark:text-gray-400', className)}>
        Loading work logs...
      </div>
    )
  }

  const logs = data?.work_logs || []

  if (logs.length === 0) {
    return (
      <div className={cn('text-sm text-gray-500 dark:text-gray-400 text-center py-4', className)}>
        No time logged yet
      </div>
    )
  }

  return (
    <div className={cn('space-y-3', className)}>
      {/* Summary */}
      <div className="flex items-center justify-between text-sm">
        <span className="text-gray-500 dark:text-gray-400">
          {logs.length} work log{logs.length !== 1 ? 's' : ''}
        </span>
        <span className="font-medium text-gray-900 dark:text-gray-100">
          Total: {formatDuration(data?.total_minutes || 0)}
        </span>
      </div>

      {/* Log list */}
      <div className="space-y-2">
        {logs.map((log) => (
          <div
            key={log.id}
            className="flex items-start gap-3 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg"
          >
            <div className="w-8 h-8 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center flex-shrink-0">
              <span className="text-xs font-medium text-brand-600 dark:text-brand-400">
                {(log.user_email || '?')[0].toUpperCase()}
              </span>
            </div>

            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-sm">
                <span className="font-medium text-gray-900 dark:text-gray-100">
                  {log.user_email?.split('@')[0] || 'Unknown'}
                </span>
                <span className="text-gray-400">logged</span>
                <span className="font-semibold text-brand-600 dark:text-brand-400">
                  {log.duration_display || formatDuration(log.duration_minutes)}
                </span>
              </div>

              <div className="flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400 mt-1">
                <span className="flex items-center gap-1">
                  <Calendar size={12} />
                  {formatDate(log.worked_date)}
                </span>
              </div>

              {log.description && (
                <p className="text-sm text-gray-600 dark:text-gray-300 mt-2">
                  {log.description}
                </p>
              )}
            </div>

            {/* Delete button (only for own logs) */}
            {user && log.user_id === user.id && (
              <button
                onClick={() => {
                  if (confirm('Delete this work log?')) {
                    deleteMutation.mutate(log.id)
                  }
                }}
                disabled={deleteMutation.isPending}
                className="p-1 text-gray-400 hover:text-red-500 dark:hover:text-red-400 hover:bg-gray-200 dark:hover:bg-gray-700 rounded"
                title="Delete"
              >
                <Trash2 size={14} />
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
