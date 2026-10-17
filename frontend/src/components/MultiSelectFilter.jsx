import { useState, useRef, useEffect, useMemo } from 'react'
import { Check, ChevronDown, Search, X } from 'lucide-react'
import { cn } from '../utils/cn'

/**
 * Excel-style multi-select filter dropdown with search.
 *
 * Props:
 *  - label: button label when nothing selected (e.g. "All Assignees")
 *  - options: [{ value, label, sublabel?, avatar? }]
 *  - selectedValues: array of selected values
 *  - onChange: (newSelectedArray) => void
 *  - searchPlaceholder: placeholder for the search input
 */
export default function MultiSelectFilter({
  label = 'All',
  options = [],
  selectedValues = [],
  onChange,
  searchPlaceholder = 'Search...',
  className,
}) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const containerRef = useRef(null)
  const inputRef = useRef(null)

  // Close on outside click
  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Focus search when opening
  useEffect(() => {
    if (open && inputRef.current) {
      setTimeout(() => inputRef.current?.focus(), 0)
    }
  }, [open])

  // Dedupe options by value before use. React needs unique keys and some
  // callers (e.g. Tasks.jsx's assignee filter) can produce collisions when
  // the same user appears in both the "My Tasks" shortcut and the full
  // user list due to string/int id coercion. Last-writer-wins here is the
  // same behavior a strict dedupe would give us with fewer surprises.
  const uniqueOptions = useMemo(() => {
    const seen = new Map()
    for (const opt of options) {
      const key = String(opt?.value ?? '')
      if (!key) continue
      seen.set(key, opt)
    }
    return [...seen.values()]
  }, [options])

  const filteredOptions = useMemo(() => {
    if (!search.trim()) return uniqueOptions
    const q = search.toLowerCase()
    return uniqueOptions.filter(opt =>
      String(opt.label || '').toLowerCase().includes(q) ||
      String(opt.sublabel || '').toLowerCase().includes(q)
    )
  }, [uniqueOptions, search])

  const toggleValue = (value) => {
    const isSelected = selectedValues.includes(value)
    const next = isSelected
      ? selectedValues.filter(v => v !== value)
      : [...selectedValues, value]
    onChange(next)
  }

  const clearAll = (e) => {
    e.stopPropagation()
    onChange([])
  }

  const selectedCount = selectedValues.length
  const buttonLabel = selectedCount === 0
    ? label
    : selectedCount === 1
      ? (options.find(o => o.value === selectedValues[0])?.label || `${selectedCount} selected`)
      : `${selectedCount} selected`

  return (
    <div ref={containerRef} className={cn('relative', className)}>
      {/* Trigger is a div, not a button, so the inline Clear (X) can be
          a real button without violating "button cannot descend from
          button" HTML nesting. Keyboard + role attrs keep it accessible. */}
      <div
        role="button"
        tabIndex={0}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            setOpen(!open)
          }
        }}
        className={cn(
          'input flex items-center gap-2 min-w-[140px] cursor-pointer select-none',
          selectedCount > 0 && 'border-brand-400 dark:border-brand-600'
        )}
      >
        <span className={cn(
          'flex-1 text-left truncate text-sm',
          selectedCount === 0 && 'text-gray-500 dark:text-gray-400'
        )}>
          {buttonLabel}
        </span>
        {selectedCount > 0 && (
          <button
            type="button"
            onClick={clearAll}
            className="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 hover:text-gray-600"
            title="Clear"
          >
            <X size={12} />
          </button>
        )}
        <ChevronDown size={14} className="text-gray-400 flex-shrink-0" />
      </div>

      {open && (
        <div className="absolute z-50 mt-1 w-72 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg overflow-hidden">
          {/* Search input */}
          <div className="p-2 border-b border-gray-200 dark:border-gray-700">
            <div className="relative">
              <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                ref={inputRef}
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={searchPlaceholder}
                className="input pl-8 text-sm py-1.5 w-full"
              />
            </div>
          </div>

          {/* Options */}
          <div className="max-h-72 overflow-y-auto py-1">
            {filteredOptions.length === 0 ? (
              <div className="px-3 py-4 text-sm text-gray-500 dark:text-gray-400 text-center">
                No matches
              </div>
            ) : (
              filteredOptions.map((opt) => {
                const isSelected = selectedValues.includes(opt.value)
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => toggleValue(opt.value)}
                    className={cn(
                      'w-full flex items-center gap-2.5 px-3 py-2 text-sm transition-colors text-left',
                      isSelected
                        ? 'bg-brand-50 dark:bg-brand-900/20'
                        : 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
                    )}
                  >
                    <div className={cn(
                      'w-4 h-4 rounded border-2 flex items-center justify-center flex-shrink-0',
                      isSelected
                        ? 'bg-brand-500 border-brand-500'
                        : 'border-gray-300 dark:border-gray-600'
                    )}>
                      {isSelected && <Check size={11} className="text-white" />}
                    </div>
                    {opt.avatar && (
                      <div className="w-6 h-6 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center flex-shrink-0">
                        <span className="text-[10px] text-brand-600 dark:text-brand-400 font-medium">
                          {opt.avatar}
                        </span>
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="text-gray-900 dark:text-white truncate">{opt.label}</div>
                      {opt.sublabel && (
                        <div className="text-[11px] text-gray-500 dark:text-gray-400 truncate">{opt.sublabel}</div>
                      )}
                    </div>
                  </button>
                )
              })
            )}
          </div>

          {/* Footer */}
          {selectedCount > 0 && (
            <div className="border-t border-gray-200 dark:border-gray-700 p-2 flex items-center justify-between">
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {selectedCount} selected
              </span>
              <button
                type="button"
                onClick={() => onChange([])}
                className="text-xs text-brand-600 dark:text-brand-400 hover:underline"
              >
                Clear all
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
