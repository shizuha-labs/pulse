import { cn } from '../../utils/cn'

/**
 * Agent liveness/availability status badge.
 *
 * Status values (from pulse:agent_availability Redis key, PLAT-303 §3):
 *   active    - process up + heartbeat responsive (green)
 *   idle      - process up, no recent progress >30 min (amber)
 *   wedged    - process up but stuck in error/retry loop (red)
 *   stopped   - process down / container stopped (dark)
 *   unknown   - availability data not yet loaded (gray)
 *
 * Source of truth: Pulse availability API (pulse:agent_availability Redis key).
 * NOT the Prometheus exporter — see PLAT-303 §5 data sources.
 *
 * Props:
 *   status     - 'active' | 'idle' | 'wedged' | 'stopped' | 'unknown'
 *   size       - 'sm' (default) | 'md' | 'lg'
 *   show_label - show text label alongside dot (default false)
 *   className
 */
export default function StatusBadge({ status = 'unknown', size = 'sm', show_label = false, className }) {
  const config = STATUS_CONFIG[status] ?? STATUS_CONFIG.unknown

  const dotSizes = { sm: 'w-2 h-2', md: 'w-2.5 h-2.5', lg: 'w-3 h-3' }
  const dotSz = dotSizes[size] ?? dotSizes.sm

  return (
    <span
      className={cn('inline-flex items-center gap-1', className)}
      title={config.label}
      aria-label={`Status: ${config.label}`}
    >
      <span
        className={cn(
          'rounded-full shrink-0',
          dotSz,
          config.dotClass,
          status === 'active' && 'ring-2 ring-green-400/30',
        )}
      />
      {show_label && (
        <span className={cn('text-xs font-medium', config.textClass)}>{config.label}</span>
      )}
    </span>
  )
}

const STATUS_CONFIG = {
  active:  { label: 'Active',   dotClass: 'bg-green-500',  textClass: 'text-green-700 dark:text-green-400' },
  idle:    { label: 'Idle',     dotClass: 'bg-amber-400',  textClass: 'text-amber-700 dark:text-amber-400' },
  wedged:  { label: 'Wedged',   dotClass: 'bg-red-500',    textClass: 'text-red-700 dark:text-red-400' },
  stopped: { label: 'Stopped',  dotClass: 'bg-gray-500',   textClass: 'text-gray-700 dark:text-gray-400' },
  unknown: { label: 'Unknown',  dotClass: 'bg-gray-300 dark:bg-gray-600', textClass: 'text-gray-500 dark:text-gray-400' },
}

/** Map raw pulse:agent_availability boolean → StatusBadge status string. */
export function availabilityToStatus(available) {
  if (available === true)  return 'active'
  if (available === false) return 'stopped'
  return 'unknown'
}
