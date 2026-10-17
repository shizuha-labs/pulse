import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Kanban, Plus, Edit2, Trash2, ChevronDown, Filter, Columns, Eye, Grid3X3,
} from 'lucide-react'
import { boardsApi, boardQuickFiltersApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner from '../../components/LoadingSpinner'

const BOARD_TYPE_COLORS = {
  scrum: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  kanban: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
}

const SWIMLANE_COLORS = {
  none: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
  assignee: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  priority: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  status: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  issue_type: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  epic: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400',
}

const SWIMLANE_OPTIONS = ['none', 'assignee', 'priority', 'status', 'issue_type', 'epic']

const emptyBoardForm = {
  name: '',
  project: '',
  board_type: 'scrum',
  is_default: false,
  filter_jql: '',
  swimlane_by: 'none',
  column_config: '',
}

const emptyFilterForm = {
  name: '',
  jql: '',
  description: '',
  position: 0,
}

export default function BoardsAdmin() {
  const queryClient = useQueryClient()

  // UI state
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [editingBoardId, setEditingBoardId] = useState(null)
  const [expandedBoardId, setExpandedBoardId] = useState(null)
  const [boardForm, setBoardForm] = useState(emptyBoardForm)
  const [filterForm, setFilterForm] = useState(emptyFilterForm)
  const [showAddFilter, setShowAddFilter] = useState(null)

  // Queries
  const { data: boards, isLoading, isError, error } = useQuery({
    queryKey: ['boards-admin'],
    queryFn: async () => {
      const res = await boardsApi.list()
      return res.data.results || res.data || []
    },
  })

  const { data: quickFilters } = useQuery({
    queryKey: ['board-quick-filters-admin'],
    queryFn: async () => {
      const res = await boardQuickFiltersApi.list()
      return res.data.results || res.data || []
    },
  })

  // Mutations
  const createBoardMutation = useMutation({
    mutationFn: (data) => {
      const payload = { ...data }
      if (payload.column_config) {
        try {
          payload.column_config = JSON.parse(payload.column_config)
        } catch {
          // leave as string, server will validate
        }
      } else {
        delete payload.column_config
      }
      if (!payload.filter_jql) delete payload.filter_jql
      if (!payload.project) delete payload.project
      return boardsApi.create(payload)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['boards-admin'] })
      setShowCreateForm(false)
      setBoardForm(emptyBoardForm)
    },
  })

  const updateBoardMutation = useMutation({
    mutationFn: ({ id, ...data }) => {
      const payload = { ...data }
      if (payload.column_config) {
        try {
          payload.column_config = JSON.parse(payload.column_config)
        } catch {
          // leave as string
        }
      } else {
        delete payload.column_config
      }
      if (!payload.filter_jql) delete payload.filter_jql
      return boardsApi.update(id, payload)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['boards-admin'] })
      setEditingBoardId(null)
      setBoardForm(emptyBoardForm)
    },
  })

  const deleteBoardMutation = useMutation({
    mutationFn: (id) => boardsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['boards-admin'] })
    },
  })

  const addQuickFilterMutation = useMutation({
    mutationFn: ({ boardId, data }) => boardsApi.addQuickFilter(boardId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['boards-admin'] })
      queryClient.invalidateQueries({ queryKey: ['board-quick-filters-admin'] })
      setShowAddFilter(null)
      setFilterForm(emptyFilterForm)
    },
  })

  const removeQuickFilterMutation = useMutation({
    mutationFn: ({ boardId, filterId }) =>
      boardsApi.removeQuickFilter(boardId, { filter_id: filterId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['boards-admin'] })
      queryClient.invalidateQueries({ queryKey: ['board-quick-filters-admin'] })
    },
  })

  // Handlers
  const handleBoardSubmit = (e) => {
    e.preventDefault()
    if (editingBoardId) {
      updateBoardMutation.mutate({ id: editingBoardId, ...boardForm })
    } else {
      createBoardMutation.mutate(boardForm)
    }
  }

  const startEditBoard = (board) => {
    setEditingBoardId(board.id)
    setBoardForm({
      name: board.name || '',
      project: board.project || '',
      board_type: board.board_type || 'scrum',
      is_default: board.is_default || false,
      filter_jql: board.filter_jql || '',
      swimlane_by: board.swimlane_by || 'none',
      column_config: board.column_config ? JSON.stringify(board.column_config, null, 2) : '',
    })
    setShowCreateForm(true)
  }

  const handleDeleteBoard = (board) => {
    if (confirm(`Delete board "${board.name}"? This cannot be undone.`)) {
      deleteBoardMutation.mutate(board.id)
    }
  }

  const handleAddFilterSubmit = (e, boardId) => {
    e.preventDefault()
    addQuickFilterMutation.mutate({
      boardId,
      data: {
        ...filterForm,
        position: parseInt(filterForm.position, 10) || 0,
      },
    })
  }

  const handleRemoveFilter = (boardId, filterId) => {
    if (confirm('Remove this quick filter?')) {
      removeQuickFilterMutation.mutate({ boardId, filterId })
    }
  }

  const getFiltersForBoard = (boardId) => {
    if (!quickFilters) return []
    return quickFilters.filter((f) => f.board === boardId)
  }

  const toggleExpand = (boardId) => {
    setExpandedBoardId(expandedBoardId === boardId ? null : boardId)
    setShowAddFilter(null)
  }

  // Loading state
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  // Error state
  if (isError) {
    return (
      <div className="p-4 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300 rounded-lg">
        Failed to load boards: {error?.message || 'Unknown error'}
      </div>
    )
  }

  const boardList = boards || []

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <Kanban size={24} />
            Boards Administration
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            Manage boards and quick filters
          </p>
        </div>
        <button
          onClick={() => {
            setEditingBoardId(null)
            setBoardForm(emptyBoardForm)
            setShowCreateForm(!showCreateForm)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          Create Board
        </button>
      </div>

      {/* Create / Edit Board Form */}
      {showCreateForm && (
        <form
          onSubmit={handleBoardSubmit}
          className="card p-4 space-y-4"
        >
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <Grid3X3 size={20} />
            {editingBoardId ? 'Edit Board' : 'Create Board'}
          </h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Name *
              </label>
              <input
                type="text"
                value={boardForm.name}
                onChange={(e) => setBoardForm({ ...boardForm, name: e.target.value })}
                className="input w-full"
                placeholder="e.g., Sprint Board, Dev Kanban"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Project ID
              </label>
              <input
                type="text"
                value={boardForm.project}
                onChange={(e) => setBoardForm({ ...boardForm, project: e.target.value })}
                className="input w-full"
                placeholder="Project UUID or ID"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Board Type
              </label>
              <select
                value={boardForm.board_type}
                onChange={(e) => setBoardForm({ ...boardForm, board_type: e.target.value })}
                className="input w-full"
              >
                <option value="scrum">Scrum</option>
                <option value="kanban">Kanban</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Swimlane By
              </label>
              <select
                value={boardForm.swimlane_by}
                onChange={(e) => setBoardForm({ ...boardForm, swimlane_by: e.target.value })}
                className="input w-full"
              >
                {SWIMLANE_OPTIONS.map((opt) => (
                  <option key={opt} value={opt}>
                    {opt === 'issue_type' ? 'Issue Type' : opt.charAt(0).toUpperCase() + opt.slice(1)}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-end">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={boardForm.is_default}
                  onChange={(e) => setBoardForm({ ...boardForm, is_default: e.target.checked })}
                  className="rounded border-gray-300 dark:border-gray-600 text-brand-600 focus:ring-brand-500"
                />
                <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  Default Board
                </span>
              </label>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Filter JQL
            </label>
            <textarea
              value={boardForm.filter_jql}
              onChange={(e) => setBoardForm({ ...boardForm, filter_jql: e.target.value })}
              className="input w-full font-mono text-sm"
              rows={2}
              placeholder='e.g., project = "PROJ" AND status != Done'
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Column Config (JSON)
            </label>
            <textarea
              value={boardForm.column_config}
              onChange={(e) => setBoardForm({ ...boardForm, column_config: e.target.value })}
              className="input w-full font-mono text-sm"
              rows={4}
              placeholder='{"columns": [{"name": "To Do", "statuses": ["open"]}]}'
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={() => {
                setShowCreateForm(false)
                setEditingBoardId(null)
                setBoardForm(emptyBoardForm)
              }}
              className="btn"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createBoardMutation.isPending || updateBoardMutation.isPending}
              className="btn-primary"
            >
              {createBoardMutation.isPending || updateBoardMutation.isPending
                ? 'Saving...'
                : editingBoardId
                  ? 'Update Board'
                  : 'Create Board'}
            </button>
          </div>
        </form>
      )}

      {/* Boards List */}
      {boardList.length === 0 ? (
        <div className="card p-8 text-center">
          <Kanban size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
          <p className="text-gray-500 dark:text-gray-400">No boards configured yet</p>
          <p className="text-sm text-gray-400 dark:text-gray-500 mt-1">
            Create a board to get started
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {boardList.map((board) => {
            const isExpanded = expandedBoardId === board.id
            const filters = getFiltersForBoard(board.id)

            return (
              <div key={board.id} className="card overflow-hidden">
                {/* Board Row */}
                <div className="p-4 flex items-center justify-between">
                  <div className="flex items-center gap-3 flex-1 min-w-0">
                    <button
                      onClick={() => toggleExpand(board.id)}
                      className="btn-icon flex-shrink-0"
                    >
                      <ChevronDown
                        size={16}
                        className={cn(
                          'transition-transform',
                          isExpanded && 'rotate-180'
                        )}
                      />
                    </button>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className="font-medium text-gray-900 dark:text-white">
                          {board.name}
                        </p>
                        <span className={cn(
                          'px-2 py-0.5 text-xs rounded font-medium',
                          BOARD_TYPE_COLORS[board.board_type] || BOARD_TYPE_COLORS.scrum
                        )}>
                          {board.board_type}
                        </span>
                        {board.is_default && (
                          <span className="px-2 py-0.5 text-xs rounded font-medium bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                            default
                          </span>
                        )}
                        {board.swimlane_by && board.swimlane_by !== 'none' && (
                          <span className={cn(
                            'px-2 py-0.5 text-xs rounded',
                            SWIMLANE_COLORS[board.swimlane_by] || SWIMLANE_COLORS.none
                          )}>
                            swimlane: {board.swimlane_by}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 text-xs text-gray-400 dark:text-gray-500 mt-1">
                        {board.project_name && (
                          <span>Project: {board.project_name}</span>
                        )}
                        <span className="flex items-center gap-1">
                          <Columns size={12} />
                          {board.column_count || 0} columns
                        </span>
                        <span className="flex items-center gap-1">
                          <Filter size={12} />
                          {filters.length} quick filters
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 flex-shrink-0">
                    <button
                      onClick={() => startEditBoard(board)}
                      className="btn-icon"
                      title="Edit board"
                    >
                      <Edit2 size={16} />
                    </button>
                    <button
                      onClick={() => handleDeleteBoard(board)}
                      className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                      title="Delete board"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </div>

                {/* Expanded: Quick Filters */}
                {isExpanded && (
                  <div className="border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 p-4">
                    <div className="flex items-center justify-between mb-3">
                      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 flex items-center gap-2">
                        <Filter size={14} />
                        Quick Filters ({filters.length})
                      </h3>
                      <button
                        onClick={() => {
                          setFilterForm(emptyFilterForm)
                          setShowAddFilter(showAddFilter === board.id ? null : board.id)
                        }}
                        className="btn-primary text-xs flex items-center gap-1 px-3 py-1.5"
                      >
                        <Plus size={14} />
                        Add Filter
                      </button>
                    </div>

                    {/* Add Quick Filter Form */}
                    {showAddFilter === board.id && (
                      <form
                        onSubmit={(e) => handleAddFilterSubmit(e, board.id)}
                        className="mb-4 p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 space-y-3"
                      >
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Name *
                            </label>
                            <input
                              type="text"
                              value={filterForm.name}
                              onChange={(e) => setFilterForm({ ...filterForm, name: e.target.value })}
                              className="input w-full"
                              placeholder="e.g., My Tasks, Critical"
                              required
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Position
                            </label>
                            <input
                              type="number"
                              value={filterForm.position}
                              onChange={(e) => setFilterForm({ ...filterForm, position: e.target.value })}
                              className="input w-full"
                              min={0}
                            />
                          </div>
                        </div>
                        <div>
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            JQL *
                          </label>
                          <input
                            type="text"
                            value={filterForm.jql}
                            onChange={(e) => setFilterForm({ ...filterForm, jql: e.target.value })}
                            className="input w-full font-mono text-sm"
                            placeholder='e.g., assignee = currentUser() AND status != Done'
                            required
                          />
                        </div>
                        <div>
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            Description
                          </label>
                          <input
                            type="text"
                            value={filterForm.description}
                            onChange={(e) => setFilterForm({ ...filterForm, description: e.target.value })}
                            className="input w-full"
                            placeholder="Brief description of this filter"
                          />
                        </div>
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => {
                              setShowAddFilter(null)
                              setFilterForm(emptyFilterForm)
                            }}
                            className="btn text-xs"
                          >
                            Cancel
                          </button>
                          <button
                            type="submit"
                            disabled={addQuickFilterMutation.isPending}
                            className="btn-primary text-xs"
                          >
                            {addQuickFilterMutation.isPending ? 'Adding...' : 'Add Filter'}
                          </button>
                        </div>
                      </form>
                    )}

                    {/* Filters List */}
                    {filters.length === 0 ? (
                      <p className="text-sm text-gray-400 dark:text-gray-500 text-center py-4">
                        No quick filters on this board
                      </p>
                    ) : (
                      <div className="space-y-2">
                        {filters.map((filter) => (
                          <div
                            key={filter.id}
                            className="flex items-center justify-between p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700"
                          >
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-2">
                                <Eye size={14} className="text-gray-400 flex-shrink-0" />
                                <p className="font-medium text-sm text-gray-900 dark:text-white truncate">
                                  {filter.name}
                                </p>
                                <span className="text-xs text-gray-400 dark:text-gray-500">
                                  #{filter.position}
                                </span>
                              </div>
                              {filter.jql && (
                                <p className="text-xs text-gray-500 dark:text-gray-400 font-mono mt-0.5 truncate ml-6">
                                  {filter.jql}
                                </p>
                              )}
                              {filter.description && (
                                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5 truncate ml-6">
                                  {filter.description}
                                </p>
                              )}
                            </div>
                            <button
                              onClick={() => handleRemoveFilter(board.id, filter.id)}
                              className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30 flex-shrink-0"
                              title="Remove quick filter"
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
