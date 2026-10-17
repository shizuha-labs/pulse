import { Moon, Sun, Monitor } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { cn } from '../utils/cn'

export default function ThemeToggle({ showLabel = false, className }) {
  const { theme, toggleTheme, isDark } = useTheme()

  return (
    <button
      onClick={toggleTheme}
      className={cn(
        'btn-icon flex items-center gap-2 text-gray-600 dark:text-gray-400',
        className
      )}
      title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      {isDark ? (
        <Sun size={20} className="text-amber-500" />
      ) : (
        <Moon size={20} className="text-brand-600" />
      )}
      {showLabel && (
        <span className="text-sm">{isDark ? 'Light' : 'Dark'}</span>
      )}
    </button>
  )
}

// Dropdown version with system preference option
export function ThemeDropdown({ className }) {
  const { theme, setTheme } = useTheme()

  const options = [
    { value: 'light', label: 'Light', icon: Sun },
    { value: 'dark', label: 'Dark', icon: Moon },
  ]

  return (
    <div className={cn('relative', className)}>
      <select
        value={theme}
        onChange={(e) => setTheme(e.target.value)}
        className="input appearance-none pr-8 cursor-pointer"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <div className="absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none">
        {theme === 'dark' ? (
          <Moon size={16} className="text-gray-400" />
        ) : (
          <Sun size={16} className="text-gray-400" />
        )}
      </div>
    </div>
  )
}
