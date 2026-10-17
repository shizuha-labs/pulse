import { Clock, PlayCircle } from 'lucide-react'
import { cn } from '../utils/cn'

export default function TimeTrackingDisplay({
  originalEstimate, // in minutes
  timeSpent, // in minutes
  remainingEstimate, // in minutes
  onLogWork,
  compact = false,
  className = '',
}) {
  const formatDuration = (minutes) => {
    if (minutes === null || minutes === undefined) return '-'
    if (minutes === 0) return '0m'
    const h = Math.floor(minutes / 60)
    const m = minutes % 60
    if (h && m) return `${h}h ${m}m`
    if (h) return `${h}h`
    return `${m}m`
  }

  // Calculate progress
  const totalEstimate = originalEstimate || 0
  const spent = timeSpent || 0
  const remaining = remainingEstimate !== null && remainingEstimate !== undefined
    ? remainingEstimate
    : Math.max(0, totalEstimate - spent)

  // Progress percentage (capped at 100%)
  const progressPercent = totalEstimate > 0
    ? Math.min(100, Math.round((spent / totalEstimate) * 100))
    : 0

  // Is over estimate?
  const isOverEstimate = totalEstimate > 0 && spent > totalEstimate

  if (compact) {
    return (
      <div className={cn('flex items-center gap-2', className)}>
        <Clock size={14} className="text-gray-400" />
        <span className={cn(
          'text-sm',
          isOverEstimate ? 'text-red-600 dark:text-red-400' : 'text-gray-600 dark:text-gray-400'
        )}>
          {formatDuration(spent)}
          {totalEstimate > 0 && (
            <span className="text-gray-400"> / {formatDuration(totalEstimate)}</span>
          )}
        </span>
        {onLogWork && (
          <button
            onClick={onLogWork}
            className="text-brand-500 hover:text-brand-600 text-xs font-medium"
          >
            Log
          </button>
        )}
      </div>
    )
  }

  return (
    <div className={cn('space-y-3', className)}>
      {/* Header with log button */}
      <div className="flex items-center justify-between">
        <label className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-2">
          <Clock size={14} />
          Time Tracking
        </label>
        {onLogWork && (
          <button
            onClick={onLogWork}
            className="text-sm text-brand-500 hover:text-brand-600 font-medium flex items-center gap-1"
          >
            <PlayCircle size={14} />
            Log Work
          </button>
        )}
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-2 text-center">
        <div className="p-2 bg-gray-50 dark:bg-gray-800 rounded">
          <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">Original</div>
          <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
            {formatDuration(originalEstimate)}
          </div>
        </div>
        <div className={cn(
          'p-2 rounded',
          isOverEstimate
            ? 'bg-red-50 dark:bg-red-900/20'
            : 'bg-blue-50 dark:bg-blue-900/20'
        )}>
          <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">Logged</div>
          <div className={cn(
            'text-sm font-medium',
            isOverEstimate
              ? 'text-red-600 dark:text-red-400'
              : 'text-blue-600 dark:text-blue-400'
          )}>
            {formatDuration(spent)}
          </div>
        </div>
        <div className="p-2 bg-gray-50 dark:bg-gray-800 rounded">
          <div className="text-xs text-gray-500 dark:text-gray-400 mb-1">Remaining</div>
          <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
            {formatDuration(remaining)}
          </div>
        </div>
      </div>

      {/* Progress bar */}
      {totalEstimate > 0 && (
        <div>
          <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
            <span>Progress</span>
            <span>{progressPercent}%</span>
          </div>
          <div className="h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
            <div
              className={cn(
                'h-full rounded-full transition-all',
                isOverEstimate ? 'bg-red-500' : 'bg-blue-500'
              )}
              style={{ width: `${Math.min(100, progressPercent)}%` }}
            />
          </div>
          {isOverEstimate && (
            <p className="text-xs text-red-600 dark:text-red-400 mt-1">
              Over estimate by {formatDuration(spent - totalEstimate)}
            </p>
          )}
        </div>
      )}

      {/* No estimate hint */}
      {!totalEstimate && !spent && (
        <p className="text-xs text-gray-400 dark:text-gray-500">
          Set an estimate to track progress
        </p>
      )}
    </div>
  )
}
