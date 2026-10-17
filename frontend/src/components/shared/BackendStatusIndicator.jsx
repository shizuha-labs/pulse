import { cn } from '../../utils/cn'

/**
 * Backend/service health indicator.
 *
 * Status values (from /cortex/backends health probes):
 *   healthy   - all checks passing (green)
 *   degraded  - some checks passing, reduced capacity (amber)
 *   down      - service unreachable or majority checks failed (red)
 *   unknown   - status not yet determined (gray)
 *
 * Props:
 *   status     - 'healthy' | 'degraded' | 'down' | 'unknown'
 *   label      - optional service name shown next to indicator
 *   size       - 'sm' (default) | 'md'
 *   className
 */
export default function BackendStatusIndicator({ status = 'unknown', label, size = 'sm', className }) {
  const config = STATUS_CONFIG[status] ?? STATUS_CONFIG.unknown

  const dotSizes = { sm: 'w-2 h-2', md: 'w-2.5 h-2.5' }
  const dotSz = dotSizes[size] ?? dotSizes.sm
  const textSz = size === 'md' ? 'text-xs' : 'text-[0.65rem]'

  return (
    <span
      className={cn('inline-flex items-center gap-1.5', className)}
      title={label ? `${label}: ${config.label}` : config.label}
      aria-label={label ? `${label} status: ${config.label}` : `Status: ${config.label}`}
    >
      <span className={cn('rounded-full shrink-0', dotSz, config.dotClass)} />
      {label && (
        <span className={cn('font-medium text-gray-700 dark:text-gray-300', textSz)}>{label}</span>
      )}
    </span>
  )
}

const STATUS_CONFIG = {
  healthy:  { label: 'Healthy',  dotClass: 'bg-green-500' },
  degraded: { label: 'Degraded', dotClass: 'bg-amber-400' },
  down:     { label: 'Down',     dotClass: 'bg-red-500' },
  unknown:  { label: 'Unknown',  dotClass: 'bg-gray-300 dark:bg-gray-600' },
}
