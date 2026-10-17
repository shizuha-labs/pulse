import { useState, useEffect, useRef, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search, X, Check } from 'lucide-react'
import { itemsApi } from '../services/api'
import { cn } from '../utils/cn'

/**
 * Searchable item picker — search by key or title, select one item.
 *
 * Props:
 *  - value: currently selected item {id, item_key, title} or null
 *  - onChange: (item | null) => void
 *  - excludeId: optional ID to exclude from results (e.g., the current task)
 *  - placeholder: input placeholder
 *  - autoFocus: focus the search box on mount
 */
export default function ItemPicker({ value, onChange, excludeId, placeholder = 'Search by key or title…', autoFocus = false }) {
  const [search, setSearch] = useState('')
  const [open, setOpen] = useState(false)
  const containerRef = useRef(null)
  const inputRef = useRef(null)

  // Debounced search term
  const [debouncedSearch, setDebouncedSearch] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 200)
    return () => clearTimeout(t)
  }, [search])

  // Close on outside click
  useEffect(() => {
    const h = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  useEffect(() => {
    if (autoFocus && inputRef.current) inputRef.current.focus()
  }, [autoFocus])

  // Search query — only runs when there's a search term and the picker is open
  const { data, isFetching } = useQuery({
    queryKey: ['item-picker-search', debouncedSearch],
    queryFn: () => itemsApi.list({ search: debouncedSearch, page_size: 15, mode: 'task' }).then(r => r.data),
    enabled: debouncedSearch.length >= 1 && open,
    staleTime: 30_000,
  })

  const results = useMemo(() => {
    const items = data?.results || []
    return excludeId ? items.filter(i => i.id !== excludeId) : items
  }, [data, excludeId])

  const handleSelect = (item) => {
    onChange({ id: item.id, item_key: item.item_key, title: item.title })
    setSearch('')
    setOpen(false)
  }

  const handleClear = (e) => {
    e.stopPropagation()
    onChange(null)
    setSearch('')
  }

  // If a value is selected, show it as a chip with a clear button
  if (value) {
    return (
      <div className="flex items-center gap-2 p-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
        <span className="text-sm font-semibold text-gray-900 dark:text-white flex-shrink-0">
          {value.item_key || `#${value.id}`}
        </span>
        <span className="text-sm text-gray-500 dark:text-gray-400 truncate flex-1 min-w-0">
          {value.title}
        </span>
        <button
          type="button"
          onClick={handleClear}
          className="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 hover:text-gray-600 flex-shrink-0"
          title="Change selection"
        >
          <X size={14} />
        </button>
      </div>
    )
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
        <input
          ref={inputRef}
          type="text"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setOpen(true) }}
          onFocus={() => setOpen(true)}
          placeholder={placeholder}
          className="input pl-9 w-full text-sm"
        />
      </div>

      {open && debouncedSearch.length >= 1 && (
        <div className="absolute z-50 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-64 overflow-y-auto">
          {isFetching ? (
            <div className="px-3 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
              Searching…
            </div>
          ) : results.length === 0 ? (
            <div className="px-3 py-4 text-center text-sm text-gray-500 dark:text-gray-400">
              No matches
            </div>
          ) : (
            results.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => handleSelect(item)}
                className="w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
              >
                <span className="text-xs font-bold text-gray-900 dark:text-white flex-shrink-0 mt-0.5 min-w-[50px]">
                  {item.item_key || `#${item.id}`}
                </span>
                <div className="flex-1 min-w-0">
                  <div className={cn(
                    'text-sm truncate',
                    item.status === 'completed'
                      ? 'line-through text-gray-400'
                      : 'text-gray-700 dark:text-gray-200'
                  )}>
                    {item.title}
                  </div>
                  {item.project_key && (
                    <div className="text-[11px] text-gray-500 dark:text-gray-500">
                      {item.project_key} · {item.status}
                    </div>
                  )}
                </div>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}
