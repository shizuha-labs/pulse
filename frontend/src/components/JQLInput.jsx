import { useState, useRef, useEffect } from 'react'
import { Search, HelpCircle, X, History, Star, Check, Pencil, Trash2, Plus } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { savedFiltersApi } from '../services/api'
import { cn } from '../utils/cn'

const JQL_HELP = {
  fields: [
    { name: 'status', desc: 'Item status (pending, in_progress, completed, etc.)' },
    { name: 'priority', desc: 'Priority level (low, normal, high, urgent)' },
    { name: 'assignee', desc: 'Assigned user email' },
    { name: 'reporter', desc: 'Creator email (also: created_by, creator)' },
    { name: 'project', desc: 'Project key (e.g., PROJ)' },
    { name: 'sprint', desc: 'Sprint name' },
    { name: 'component', desc: 'Component name' },
    { name: 'fixVersion', desc: 'Fix version name' },
    { name: 'title', desc: 'Item title (also: summary)' },
    { name: 'due', desc: 'Due date (also: dueDate)' },
    { name: 'created', desc: 'Creation date' },
    { name: 'storyPoints', desc: 'Story point estimate' },
    { name: 'labels', desc: 'JSON labels field' },
  ],
  operators: [
    { op: '=', desc: 'Equals' },
    { op: '!=', desc: 'Not equals' },
    { op: '~', desc: 'Contains (text search)' },
    { op: '!~', desc: 'Does not contain' },
    { op: '>', desc: 'Greater than (dates, numbers)' },
    { op: '<', desc: 'Less than' },
    { op: '>=', desc: 'Greater than or equal' },
    { op: '<=', desc: 'Less than or equal' },
    { op: 'IN', desc: 'Value in list' },
    { op: 'NOT IN', desc: 'Value not in list' },
    { op: 'IS EMPTY', desc: 'Field is empty/null' },
    { op: 'IS NOT EMPTY', desc: 'Field has a value' },
  ],
  functions: [
    { fn: 'currentUser()', desc: 'Current logged-in user' },
    { fn: 'now()', desc: 'Current date/time' },
    { fn: 'startOfDay()', desc: 'Start of today' },
    { fn: 'endOfDay()', desc: 'End of today' },
    { fn: 'startOfWeek()', desc: 'Start of current week' },
    { fn: 'startOfMonth()', desc: 'Start of current month' },
  ],
  examples: [
    'status = "pending" AND priority = "high"',
    'assignee = currentUser()',
    'due < now() AND status != "completed"',
    'project = "PROJ" AND sprint = "Sprint 1"',
    'project IS EMPTY',  // Personal tasks without project
    'project IS NOT EMPTY',  // Only tasks with project
    'title ~ "bug"',
    '(status = "pending" OR status = "blocked") AND priority IN ("high", "urgent")',
  ],
}

export default function JQLInput({
  value,
  onChange,
  onSearch,
  projectId,
  placeholder = 'Search with JQL...',
  className = '',
}) {
  const [showHelp, setShowHelp] = useState(false)
  const [showFilters, setShowFilters] = useState(false)
  const [showSaveForm, setShowSaveForm] = useState(false)
  const [newFilterName, setNewFilterName] = useState('')
  const [editingFilter, setEditingFilter] = useState(null)
  const [editName, setEditName] = useState('')
  const [filterError, setFilterError] = useState('')
  const [recentQueries, setRecentQueries] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem('jql-recent') || '[]')
    } catch {
      return []
    }
  })
  const inputRef = useRef(null)
  const queryClient = useQueryClient()

  // Fetch saved filters
  const { data: savedFilters = [] } = useQuery({
    queryKey: ['saved-filters', projectId],
    queryFn: async () => {
      const res = await savedFiltersApi.list({ project: projectId })
      return res.data?.results || res.data || []
    },
    enabled: showFilters,
  })

  const invalidateFilters = () => {
    queryClient.invalidateQueries({ queryKey: ['saved-filters', projectId] })
  }

  const createMutation = useMutation({
    mutationFn: (data) => savedFiltersApi.create(data),
    onSuccess: () => {
      invalidateFilters()
      setShowSaveForm(false)
      setNewFilterName('')
      setFilterError('')
    },
    onError: (err) => {
      const detail = err?.response?.data
      const msg = typeof detail === 'string'
        ? detail
        : (detail?.name?.[0] || detail?.jql?.[0] || detail?.detail || 'Failed to save filter')
      setFilterError(msg)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }) => savedFiltersApi.update(id, data),
    onSuccess: () => {
      invalidateFilters()
      setEditingFilter(null)
      setEditName('')
      setFilterError('')
    },
    onError: (err) => {
      const detail = err?.response?.data
      const msg = typeof detail === 'string'
        ? detail
        : (detail?.name?.[0] || detail?.jql?.[0] || detail?.detail || 'Failed to update filter')
      setFilterError(msg)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => savedFiltersApi.delete(id),
    onSuccess: () => invalidateFilters(),
    onError: (err) => {
      const detail = err?.response?.data
      const msg = typeof detail === 'string' ? detail : (detail?.detail || 'Failed to delete filter')
      setFilterError(msg)
    },
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    if (value.trim()) {
      // Save to recent queries
      const newRecent = [
        value,
        ...recentQueries.filter(q => q !== value),
      ].slice(0, 10)
      setRecentQueries(newRecent)
      localStorage.setItem('jql-recent', JSON.stringify(newRecent))
    }
    onSearch?.(value)
  }

  const handleSelectFilter = (jql) => {
    onChange(jql)
    setShowFilters(false)
    onSearch?.(jql)
  }

  const handleSaveFilter = (e) => {
    e.preventDefault()
    if (!newFilterName.trim() || !value.trim()) return
    setFilterError('')
    createMutation.mutate({
      name: newFilterName.trim(),
      jql: value,
      ...(projectId ? { project: projectId } : {}),
    })
  }

  const handleStartEdit = (filter) => {
    setEditingFilter(filter)
    setEditName(filter.name)
    setFilterError('')
  }

  const handleSaveEdit = (e) => {
    e.preventDefault()
    if (!editingFilter || !editName.trim()) return
    setFilterError('')
    updateMutation.mutate({ id: editingFilter.id, data: { name: editName.trim() } })
  }

  const handleDeleteFilter = (filter) => {
    if (!window.confirm(`Delete saved filter "${filter.name}"?`)) return
    setFilterError('')
    deleteMutation.mutate(filter.id)
  }

  const handleClear = () => {
    onChange('')
    onSearch?.('')
    inputRef.current?.focus()
  }

  return (
    <div className={cn('relative', className)}>
      <form onSubmit={handleSubmit} className="relative">
        <Search
          size={16}
          className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
        />
        <input
          ref={inputRef}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className={cn(
            'input w-full pl-9 pr-20',
            'font-mono text-sm'
          )}
        />
        <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
          {value && (
            <button
              type="button"
              onClick={handleClear}
              className="p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded"
              title="Clear"
            >
              <X size={14} className="text-gray-400" />
            </button>
          )}
          <button
            type="button"
            onClick={() => setShowFilters(!showFilters)}
            className={cn(
              'p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded',
              showFilters && 'bg-gray-100 dark:bg-gray-800'
            )}
            title="Saved Filters"
          >
            <Star size={14} className="text-gray-400" />
          </button>
          <button
            type="button"
            onClick={() => setShowHelp(!showHelp)}
            className={cn(
              'p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded',
              showHelp && 'bg-gray-100 dark:bg-gray-800'
            )}
            title="JQL Help"
          >
            <HelpCircle size={14} className="text-gray-400" />
          </button>
        </div>
      </form>

      {/* Saved Filters Dropdown */}
      {showFilters && (
        <div className="absolute z-40 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-80 overflow-y-auto">
          {/* Recent queries */}
          {recentQueries.length > 0 && (
            <div className="p-2 border-b border-gray-200 dark:border-gray-700">
              <div className="text-xs font-medium text-gray-500 dark:text-gray-400 px-2 py-1 flex items-center gap-1">
                <History size={12} />
                Recent
              </div>
              {recentQueries.slice(0, 5).map((query, idx) => (
                <button
                  key={idx}
                  onClick={() => handleSelectFilter(query)}
                  className="w-full text-left px-2 py-1.5 text-sm font-mono text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded truncate"
                >
                  {query}
                </button>
              ))}
            </div>
          )}

          {/* Saved filters */}
          <div className="p-2">
            <div className="text-xs font-medium text-gray-500 dark:text-gray-400 px-2 py-1 flex items-center justify-between">
              <span className="flex items-center gap-1">
                <Star size={12} />
                Saved Filters
              </span>
              {value.trim() && (
                <button
                  type="button"
                  onClick={() => {
                    setShowSaveForm(!showSaveForm)
                    setFilterError('')
                  }}
                  className="text-brand-600 dark:text-brand-400 hover:text-brand-700 flex items-center gap-1 text-xs"
                  title="Save current query"
                >
                  <Plus size={12} />
                  Save query
                </button>
              )}
            </div>

            {/* Inline save form */}
            {showSaveForm && (
              <form
                onSubmit={handleSaveFilter}
                className="p-2 border border-gray-200 dark:border-gray-700 rounded-lg mb-2"
              >
                <input
                  type="text"
                  value={newFilterName}
                  onChange={(e) => setNewFilterName(e.target.value)}
                  placeholder="Filter name..."
                  className="input w-full text-sm px-2 py-1.5 mb-2"
                  autoFocus
                />
                <div className="flex items-center gap-1">
                  <button
                    type="submit"
                    disabled={!newFilterName.trim()}
                    className="btn-icon text-green-600 hover:text-green-700 disabled:opacity-40"
                    title="Save filter"
                  >
                    <Check size={14} />
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setShowSaveForm(false)
                      setNewFilterName('')
                      setFilterError('')
                    }}
                    className="btn-icon text-gray-400 hover:text-gray-600"
                    title="Cancel"
                  >
                    <X size={14} />
                  </button>
                </div>
              </form>
            )}

            {/* Filter error (duplicate name / malformed JQL) */}
            {filterError && (
              <div className="text-xs text-red-600 dark:text-red-400 px-2 py-1.5 bg-red-50 dark:bg-red-900/20 rounded mb-1">
                {filterError}
              </div>
            )}

            {savedFilters.length === 0 ? (
              <div className="text-sm text-gray-400 px-2 py-2">
                No saved filters
              </div>
            ) : (
              savedFilters.map((filter) => (
                <div
                  key={filter.id}
                  className="flex items-center gap-1 px-1 py-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded group"
                >
                  {editingFilter?.id === filter.id ? (
                    <form onSubmit={handleSaveEdit} className="flex items-center gap-1 flex-1 min-w-0">
                      <input
                        type="text"
                        value={editName}
                        onChange={(e) => setEditName(e.target.value)}
                        className="input text-sm px-2 py-1 flex-1 min-w-0"
                        autoFocus
                      />
                      <button
                        type="submit"
                        disabled={!editName.trim()}
                        className="btn-icon text-green-600 hover:text-green-700 disabled:opacity-40"
                        title="Save"
                      >
                        <Check size={14} />
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditingFilter(null)}
                        className="btn-icon text-gray-400 hover:text-gray-600"
                        title="Cancel"
                      >
                        <X size={14} />
                      </button>
                    </form>
                  ) : (
                    <>
                      <button
                        onClick={() => handleSelectFilter(filter.jql)}
                        className="flex-1 min-w-0 text-left px-1 py-0.5"
                      >
                        <div className="text-sm text-gray-900 dark:text-gray-100 truncate">
                          {filter.name}
                        </div>
                        <div className="text-xs font-mono text-gray-500 dark:text-gray-400 truncate">
                          {filter.jql}
                        </div>
                      </button>
                      <button
                        type="button"
                        onClick={() => handleStartEdit(filter)}
                        className="opacity-0 group-hover:opacity-100 btn-icon text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-opacity"
                        title="Rename filter"
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDeleteFilter(filter)}
                        className="opacity-0 group-hover:opacity-100 btn-icon text-red-500 hover:text-red-600 transition-opacity"
                        title="Delete filter"
                      >
                        <Trash2 size={13} />
                      </button>
                    </>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* Help Dropdown */}
      {showHelp && (
        <div className="absolute z-40 mt-1 w-full max-w-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-96 overflow-y-auto">
          <div className="p-4 space-y-4">
            <h3 className="font-semibold text-gray-900 dark:text-gray-100">
              JQL Quick Reference
            </h3>

            {/* Fields */}
            <div>
              <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-2">
                Fields
              </h4>
              <div className="grid grid-cols-2 gap-1 text-xs">
                {JQL_HELP.fields.map(({ name, desc }) => (
                  <button
                    key={name}
                    onClick={() => onChange(value + name)}
                    className="text-left px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
                  >
                    <span className="font-mono text-brand-600 dark:text-brand-400">{name}</span>
                    <span className="text-gray-500 dark:text-gray-400 ml-1">- {desc}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* Operators */}
            <div>
              <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-2">
                Operators
              </h4>
              <div className="flex flex-wrap gap-1">
                {JQL_HELP.operators.map(({ op, desc }) => (
                  <span
                    key={op}
                    className="text-xs px-2 py-1 bg-gray-100 dark:bg-gray-700 rounded"
                    title={desc}
                  >
                    <code>{op}</code>
                  </span>
                ))}
              </div>
            </div>

            {/* Functions */}
            <div>
              <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-2">
                Functions
              </h4>
              <div className="flex flex-wrap gap-1">
                {JQL_HELP.functions.map(({ fn, desc }) => (
                  <button
                    key={fn}
                    onClick={() => onChange(value + fn)}
                    className="text-xs px-2 py-1 bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400 rounded hover:bg-blue-100 dark:hover:bg-blue-900/30"
                    title={desc}
                  >
                    <code>{fn}</code>
                  </button>
                ))}
              </div>
            </div>

            {/* Examples */}
            <div>
              <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-2">
                Examples
              </h4>
              <div className="space-y-1">
                {JQL_HELP.examples.map((example, idx) => (
                  <button
                    key={idx}
                    onClick={() => {
                      onChange(example)
                      setShowHelp(false)
                    }}
                    className="w-full text-left text-xs font-mono px-2 py-1 bg-gray-50 dark:bg-gray-700 rounded hover:bg-gray-100 dark:hover:bg-gray-600"
                  >
                    {example}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
