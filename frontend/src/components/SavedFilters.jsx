import { useState, useEffect } from 'react'
import {
  Bookmark,
  BookmarkPlus,
  Trash2,
  ChevronDown,
  ChevronUp,
  X,
  Check,
} from 'lucide-react'
import { cn } from '../utils/cn'
import { DEFAULT_TASK_SORT } from '../utils/taskSort'

const STORAGE_KEY = 'shizuha-pulse-saved-filters'

// Load saved filters from localStorage
function loadSavedFilters() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored) {
      return JSON.parse(stored)
    }
  } catch (err) {
    console.error('Failed to load saved filters:', err)
  }
  return []
}

// Save filters to localStorage
function saveSavedFilters(filters) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(filters))
  } catch (err) {
    console.error('Failed to save filters:', err)
  }
}

export default function SavedFilters({
  currentFilters, // { status, priority, assignee, reporter, sort }
  onApplyFilter,
}) {
  const [savedFilters, setSavedFilters] = useState([])
  const [isOpen, setIsOpen] = useState(false)
  const [showSaveForm, setShowSaveForm] = useState(false)
  const [newFilterName, setNewFilterName] = useState('')

  // Load saved filters on mount
  useEffect(() => {
    setSavedFilters(loadSavedFilters())
  }, [])

  const hasActiveFilters = () => {
    return (
      currentFilters.status ||
      currentFilters.priority ||
      currentFilters.assignee ||
      currentFilters.reporter ||
      currentFilters.sort !== DEFAULT_TASK_SORT
    )
  }

  const handleSaveFilter = (e) => {
    e.preventDefault()
    if (!newFilterName.trim()) return

    const newFilter = {
      id: Date.now().toString(),
      name: newFilterName.trim(),
      ...currentFilters,
      createdAt: new Date().toISOString(),
    }

    const updated = [...savedFilters, newFilter]
    setSavedFilters(updated)
    saveSavedFilters(updated)
    setNewFilterName('')
    setShowSaveForm(false)
  }

  const handleDeleteFilter = (filterId) => {
    const updated = savedFilters.filter(f => f.id !== filterId)
    setSavedFilters(updated)
    saveSavedFilters(updated)
  }

  const handleApplyFilter = (filter) => {
    onApplyFilter({
      status: filter.status || '',
      priority: filter.priority || '',
      assignee: filter.assignee || '',
      reporter: filter.reporter || '',
      sort: filter.sort || DEFAULT_TASK_SORT,
    })
    setIsOpen(false)
  }

  const getFilterDescription = (filter) => {
    const parts = []
    if (filter.status) parts.push(`Status: ${filter.status}`)
    if (filter.priority) parts.push(`Priority: ${filter.priority}`)
    if (filter.assignee) parts.push('Assigned')
    if (filter.reporter) parts.push('Reported')
    if (filter.sort && filter.sort !== DEFAULT_TASK_SORT) {
      const sortLabel = filter.sort.replace('-asc', ' ↑').replace('-desc', ' ↓')
      parts.push(`Sort: ${sortLabel}`)
    }
    return parts.length > 0 ? parts.join(', ') : 'No filters'
  }

  return (
    <div className="relative">
      {/* Toggle button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={cn(
          'flex items-center gap-1.5 px-3 py-2 rounded-lg border transition-colors',
          savedFilters.length > 0 || hasActiveFilters()
            ? 'border-brand-300 dark:border-brand-700 text-brand-700 dark:text-brand-300 bg-brand-50 dark:bg-brand-900/30'
            : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'
        )}
        title="Saved filters"
      >
        <Bookmark size={16} />
        <span className="hidden sm:inline text-sm">Filters</span>
        {savedFilters.length > 0 && (
          <span className="text-xs bg-brand-600 text-white rounded-full w-4 h-4 flex items-center justify-center">
            {savedFilters.length}
          </span>
        )}
        {isOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {/* Dropdown */}
      {isOpen && (
        <div className="absolute right-0 mt-2 w-72 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 z-40">
          {/* Save current filter */}
          {hasActiveFilters() && (
            <div className="p-3 border-b border-gray-200 dark:border-gray-700">
              {showSaveForm ? (
                <form onSubmit={handleSaveFilter} className="flex items-center gap-2">
                  <input
                    type="text"
                    value={newFilterName}
                    onChange={(e) => setNewFilterName(e.target.value)}
                    placeholder="Filter name..."
                    className="input text-sm flex-1"
                    autoFocus
                  />
                  <button
                    type="submit"
                    disabled={!newFilterName.trim()}
                    className="btn-icon text-green-600 hover:text-green-700"
                    title="Save"
                  >
                    <Check size={16} />
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setShowSaveForm(false)
                      setNewFilterName('')
                    }}
                    className="btn-icon text-gray-400 hover:text-gray-600"
                    title="Cancel"
                  >
                    <X size={16} />
                  </button>
                </form>
              ) : (
                <button
                  onClick={() => setShowSaveForm(true)}
                  className="flex items-center gap-2 w-full px-3 py-2 text-sm text-brand-600 dark:text-brand-400 hover:bg-brand-50 dark:hover:bg-brand-900/30 rounded-lg transition-colors"
                >
                  <BookmarkPlus size={16} />
                  Save current filters
                </button>
              )}
            </div>
          )}

          {/* Saved filters list */}
          <div className="max-h-64 overflow-y-auto">
            {savedFilters.length === 0 ? (
              <div className="p-4 text-center text-sm text-gray-500 dark:text-gray-400">
                No saved filters yet.
                {hasActiveFilters() && ' Apply filters and save them for quick access.'}
              </div>
            ) : (
              <div className="py-1">
                {savedFilters.map((filter) => (
                  <div
                    key={filter.id}
                    className="flex items-center gap-2 px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/50 group"
                  >
                    <button
                      onClick={() => handleApplyFilter(filter)}
                      className="flex-1 text-left"
                    >
                      <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                        {filter.name}
                      </p>
                      <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                        {getFilterDescription(filter)}
                      </p>
                    </button>
                    <button
                      onClick={() => handleDeleteFilter(filter.id)}
                      className="opacity-0 group-hover:opacity-100 btn-icon text-red-500 hover:text-red-600 transition-opacity"
                      title="Delete filter"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Clear all link */}
          {savedFilters.length > 0 && (
            <div className="p-2 border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={() => {
                  if (confirm('Delete all saved filters?')) {
                    setSavedFilters([])
                    saveSavedFilters([])
                  }
                }}
                className="w-full text-center text-xs text-red-500 hover:text-red-600 py-1"
              >
                Clear all saved filters
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
