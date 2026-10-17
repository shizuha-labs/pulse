import { useState } from 'react'
import { cn } from '../../utils/cn'

const SIZES = {
  sm: { outer: 'w-6 h-6', text: 'text-xs' },
  md: { outer: 'w-8 h-8', text: 'text-sm' },
  lg: { outer: 'w-10 h-10', text: 'text-base' },
}

function initials(displayName) {
  if (!displayName) return '?'
  const parts = displayName.trim().split(/\s+/)
  if (parts.length === 1) return parts[0][0].toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

/**
 * Agent/user avatar with image support and initials fallback.
 *
 * Props:
 *   display_name - full name (drives initials fallback)
 *   src          - explicit image URL; if absent or it fails to load, initials show
 *   size         - 'sm' | 'md' (default) | 'lg'
 *   className    - extra Tailwind classes
 *
 * Note: avatars require an explicit `src` — there is no `users/<id>/avatar/` route
 * to synthesize from a user_id (Codex P2), and the API base is configurable.
 */
export default function AgentAvatar({ display_name, src, size = 'md', className }) {
  const sz = SIZES[size] ?? SIZES.md
  const [imgError, setImgError] = useState(false)
  // Render the image only when a real src is given AND it hasn't failed; otherwise
  // show initials alone (Codex P2: don't stack the img and initials in the circle).
  const showImage = Boolean(src) && !imgError
  const label = initials(display_name)

  return (
    <span
      title={display_name}
      className={cn(
        'inline-flex items-center justify-center rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden select-none shrink-0',
        sz.outer,
        className,
      )}
    >
      {showImage ? (
        <img
          src={src}
          alt={display_name ?? 'Avatar'}
          className="w-full h-full object-cover"
          onError={() => setImgError(true)}
        />
      ) : (
        <span className={cn('font-medium text-gray-600 dark:text-gray-300', sz.text)} aria-hidden>
          {label}
        </span>
      )}
    </span>
  )
}
