import { AlertTriangle, AlertCircle } from 'lucide-react'
import { cn } from '../utils/cn'

/**
 * WIP (Work In Progress) Indicator component
 * Shows a visual warning when column items approach or exceed WIP limit
 */
export default function WIPIndicator({
  current,
  limit,
  showCount = true,
  size = 'md',
  className = '',
}) {
  if (!limit) return null

  const percentage = (current / limit) * 100
  const isAtLimit = current >= limit
  const isNearLimit = current >= limit * 0.8

  // Size variants
  const sizes = {
    sm: {
      icon: 12,
      text: 'text-xs',
      padding: 'px-1.5 py-0.5',
    },
    md: {
      icon: 14,
      text: 'text-sm',
      padding: 'px-2 py-1',
    },
    lg: {
      icon: 16,
      text: 'text-base',
      padding: 'px-2.5 py-1.5',
    },
  }

  const sizeConfig = sizes[size] || sizes.md

  if (isAtLimit) {
    return (
      <div
        className={cn(
          'inline-flex items-center gap-1 rounded font-medium',
          sizeConfig.padding,
          sizeConfig.text,
          'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
          className
        )}
        title={`WIP limit reached: ${current}/${limit}`}
      >
        <AlertCircle size={sizeConfig.icon} />
        {showCount && (
          <span>{current}/{limit}</span>
        )}
      </div>
    )
  }

  if (isNearLimit) {
    return (
      <div
        className={cn(
          'inline-flex items-center gap-1 rounded font-medium',
          sizeConfig.padding,
          sizeConfig.text,
          'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
          className
        )}
        title={`Approaching WIP limit: ${current}/${limit}`}
      >
        <AlertTriangle size={sizeConfig.icon} />
        {showCount && (
          <span>{current}/{limit}</span>
        )}
      </div>
    )
  }

  // Normal state - just show count if requested
  if (showCount) {
    return (
      <div
        className={cn(
          'inline-flex items-center gap-1 rounded',
          sizeConfig.padding,
          sizeConfig.text,
          'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
          className
        )}
        title={`WIP: ${current}/${limit}`}
      >
        <span>{current}/{limit}</span>
      </div>
    )
  }

  return null
}

/**
 * WIP Progress Bar variant
 * Shows a visual progress bar for the WIP status
 */
export function WIPProgressBar({
  current,
  limit,
  className = '',
}) {
  if (!limit) return null

  const percentage = Math.min(100, (current / limit) * 100)
  const isAtLimit = current >= limit
  const isNearLimit = current >= limit * 0.8

  return (
    <div className={cn('space-y-1', className)}>
      <div className="flex justify-between text-xs">
        <span className="text-gray-500 dark:text-gray-400">WIP</span>
        <span className={cn(
          'font-medium',
          isAtLimit
            ? 'text-red-600 dark:text-red-400'
            : isNearLimit
              ? 'text-amber-600 dark:text-amber-400'
              : 'text-gray-600 dark:text-gray-400'
        )}>
          {current}/{limit}
        </span>
      </div>
      <div className="h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div
          className={cn(
            'h-full rounded-full transition-all',
            isAtLimit
              ? 'bg-red-500'
              : isNearLimit
                ? 'bg-amber-500'
                : 'bg-green-500'
          )}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  )
}

/**
 * Column Header with WIP
 * A complete column header component with name and WIP indicator
 */
export function ColumnHeaderWithWIP({
  name,
  count,
  wipLimit,
  color,
  className = '',
}) {
  const isAtLimit = wipLimit && count >= wipLimit
  const isNearLimit = wipLimit && count >= wipLimit * 0.8

  return (
    <div
      className={cn(
        'flex items-center justify-between p-3 rounded-t-lg',
        isAtLimit
          ? 'bg-red-50 dark:bg-red-900/10'
          : isNearLimit
            ? 'bg-amber-50 dark:bg-amber-900/10'
            : 'bg-gray-100 dark:bg-gray-800',
        className
      )}
    >
      <div className="flex items-center gap-2">
        {color && (
          <div
            className="w-2 h-2 rounded-full"
            style={{ backgroundColor: color }}
          />
        )}
        <h3 className="font-medium text-gray-900 dark:text-gray-100">
          {name}
        </h3>
        <span className={cn(
          'text-sm',
          isAtLimit
            ? 'text-red-600 dark:text-red-400 font-medium'
            : isNearLimit
              ? 'text-amber-600 dark:text-amber-400 font-medium'
              : 'text-gray-500 dark:text-gray-400'
        )}>
          {count}
          {wipLimit && `/${wipLimit}`}
        </span>
      </div>

      {isAtLimit && (
        <AlertCircle
          size={16}
          className="text-red-500 animate-pulse"
        />
      )}
      {isNearLimit && !isAtLimit && (
        <AlertTriangle
          size={16}
          className="text-amber-500"
        />
      )}
    </div>
  )
}
