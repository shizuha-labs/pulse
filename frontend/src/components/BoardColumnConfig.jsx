import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Plus, Trash2, GripVertical, Edit2, Save, X,
  AlertCircle, CheckCircle
} from 'lucide-react'
import { boardColumnsApi, workflowsApi } from '../services/api'
import { cn } from '../utils/cn'

const DEFAULT_COLORS = [
  '#6B7280', // gray
  '#3B82F6', // blue
  '#10B981', // green
  '#F59E0B', // amber
  '#EF4444', // red
  '#8B5CF6', // violet
  '#EC4899', // pink
  '#06B6D4', // cyan
]

export default function BoardColumnConfig({ projectId, className = '' }) {
  const queryClient = useQueryClient()
  const [editingColumn, setEditingColumn] = useState(null)
  const [showAddForm, setShowAddForm] = useState(false)
  const [newColumn, setNewColumn] = useState({
    name: '',
    status_mapping: [],
    wip_limit: null,
    color: DEFAULT_COLORS[0],
  })

  // Fetch columns
  const { data: columns = [], isLoading: columnsLoading } = useQuery({
    queryKey: ['board-columns', projectId],
    queryFn: async () => {
      const res = await boardColumnsApi.list({ project: projectId })
      // API returns paginated {results: [...]} or direct array
      return res.data.results || res.data || []
    },
    enabled: !!projectId,
  })

  // Fetch workflow statuses for mapping
  const { data: workflow, isError: workflowError } = useQuery({
    queryKey: ['workflow', projectId],
    queryFn: async () => {
      try {
        const res = await workflowsApi.get(projectId)
        return res.data
      } catch (err) {
        // Return null if workflow doesn't exist (404)
        if (err.response?.status === 404) {
          return null
        }
        throw err
      }
    },
    enabled: !!projectId,
    retry: false, // Don't retry 404s
  })

  const statuses = workflow?.statuses || []

  // Create column mutation
  const createMutation = useMutation({
    mutationFn: (data) => boardColumnsApi.create({ project: projectId, ...data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board-columns', projectId] })
      setShowAddForm(false)
      setNewColumn({
        name: '',
        status_mapping: [],
        wip_limit: null,
        color: DEFAULT_COLORS[0],
      })
    },
  })

  // Update column mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => boardColumnsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board-columns', projectId] })
      setEditingColumn(null)
    },
  })

  // Delete column mutation
  const deleteMutation = useMutation({
    mutationFn: (id) => boardColumnsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board-columns', projectId] })
    },
  })

  // Reorder columns mutation
  const reorderMutation = useMutation({
    mutationFn: (columnIds) => boardColumnsApi.reorder(projectId, columnIds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board-columns', projectId] })
    },
  })

  const handleCreateColumn = () => {
    if (!newColumn.name) return
    createMutation.mutate({
      ...newColumn,
      position: columns.length,
    })
  }

  const handleUpdateColumn = (column) => {
    updateMutation.mutate(column)
  }

  const handleDeleteColumn = (columnId) => {
    if (confirm('Delete this column?')) {
      deleteMutation.mutate(columnId)
    }
  }

  const handleMoveColumn = (fromIndex, direction) => {
    const toIndex = fromIndex + direction
    if (toIndex < 0 || toIndex >= columns.length) return

    const reordered = [...columns]
    const [moved] = reordered.splice(fromIndex, 1)
    reordered.splice(toIndex, 0, moved)

    reorderMutation.mutate(reordered.map(c => c.id))
  }

  const toggleStatusMapping = (columnData, setColumnData, statusName) => {
    const mapping = columnData.status_mapping || []
    if (mapping.includes(statusName)) {
      setColumnData({
        ...columnData,
        status_mapping: mapping.filter(s => s !== statusName),
      })
    } else {
      setColumnData({
        ...columnData,
        status_mapping: [...mapping, statusName],
      })
    }
  }

  if (columnsLoading) {
    return (
      <div className={cn('text-gray-500 dark:text-gray-400', className)}>
        Loading board configuration...
      </div>
    )
  }

  return (
    <div className={cn('space-y-6', className)}>
      {/* Header */}
      <div>
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Board Columns
        </h3>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Configure the columns displayed on the project board
        </p>
      </div>

      {/* Column List */}
      <div className="space-y-3">
        {columns.map((column, index) => (
          <ColumnCard
            key={column.id}
            column={column}
            index={index}
            totalColumns={columns.length}
            statuses={statuses}
            isEditing={editingColumn === column.id}
            onEdit={() => setEditingColumn(column.id)}
            onCancelEdit={() => setEditingColumn(null)}
            onSave={handleUpdateColumn}
            onDelete={() => handleDeleteColumn(column.id)}
            onMove={(direction) => handleMoveColumn(index, direction)}
            toggleStatusMapping={toggleStatusMapping}
          />
        ))}
      </div>

      {/* Add Column Form */}
      {showAddForm ? (
        <div className="p-4 border border-gray-200 dark:border-gray-700 rounded-lg space-y-4">
          <h4 className="text-sm font-medium text-gray-900 dark:text-gray-100">
            Add New Column
          </h4>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-gray-500 mb-1">Column Name</label>
              <input
                type="text"
                value={newColumn.name}
                onChange={(e) => setNewColumn({ ...newColumn, name: e.target.value })}
                placeholder="e.g., In Review"
                className="input w-full text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">WIP Limit (optional)</label>
              <input
                type="number"
                min="0"
                value={newColumn.wip_limit || ''}
                onChange={(e) => setNewColumn({
                  ...newColumn,
                  wip_limit: e.target.value ? parseInt(e.target.value) : null
                })}
                placeholder="No limit"
                className="input w-full text-sm"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs text-gray-500 mb-2">Color</label>
            <div className="flex gap-2">
              {DEFAULT_COLORS.map(color => (
                <button
                  key={color}
                  onClick={() => setNewColumn({ ...newColumn, color })}
                  className={cn(
                    'w-6 h-6 rounded-full border-2 transition-transform',
                    newColumn.color === color
                      ? 'border-gray-900 dark:border-white scale-110'
                      : 'border-transparent hover:scale-110'
                  )}
                  style={{ backgroundColor: color }}
                />
              ))}
            </div>
          </div>

          <div>
            <label className="block text-xs text-gray-500 mb-2">Status Mapping</label>
            <p className="text-xs text-gray-400 mb-2">
              Select which statuses show in this column
            </p>
            <div className="flex flex-wrap gap-2">
              {statuses.map(status => (
                <button
                  key={status.name}
                  onClick={() => toggleStatusMapping(newColumn, setNewColumn, status.name)}
                  className={cn(
                    'px-2 py-1 rounded text-xs font-medium border transition-colors',
                    (newColumn.status_mapping || []).includes(status.name)
                      ? 'bg-brand-100 border-brand-300 text-brand-700 dark:bg-brand-900/30 dark:border-brand-700 dark:text-brand-400'
                      : 'bg-gray-100 border-gray-200 text-gray-600 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-400 hover:border-gray-300'
                  )}
                >
                  {status.label}
                </button>
              ))}
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <button
              onClick={() => setShowAddForm(false)}
              className="btn-secondary text-sm"
            >
              Cancel
            </button>
            <button
              onClick={handleCreateColumn}
              disabled={!newColumn.name || createMutation.isPending}
              className="btn-primary text-sm"
            >
              Add Column
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setShowAddForm(true)}
          className="flex items-center gap-2 text-sm text-brand-500 hover:text-brand-600"
        >
          <Plus size={16} />
          Add Column
        </button>
      )}

      {/* Error messages */}
      {(createMutation.isError || updateMutation.isError || deleteMutation.isError) && (
        <div className="flex items-center gap-2 text-sm text-red-600 dark:text-red-400">
          <AlertCircle size={14} />
          Failed to save changes
        </div>
      )}
    </div>
  )
}

// Column Card Component
function ColumnCard({
  column,
  index,
  totalColumns,
  statuses,
  isEditing,
  onEdit,
  onCancelEdit,
  onSave,
  onDelete,
  onMove,
  toggleStatusMapping,
}) {
  const [editData, setEditData] = useState(column)

  const handleSave = () => {
    onSave(editData)
  }

  if (isEditing) {
    return (
      <div className="p-4 border border-brand-200 dark:border-brand-800 bg-brand-50 dark:bg-brand-900/10 rounded-lg space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Column Name</label>
            <input
              type="text"
              value={editData.name}
              onChange={(e) => setEditData({ ...editData, name: e.target.value })}
              className="input w-full text-sm"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">WIP Limit</label>
            <input
              type="number"
              min="0"
              value={editData.wip_limit || ''}
              onChange={(e) => setEditData({
                ...editData,
                wip_limit: e.target.value ? parseInt(e.target.value) : null
              })}
              placeholder="No limit"
              className="input w-full text-sm"
            />
          </div>
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-2">Color</label>
          <div className="flex gap-2">
            {DEFAULT_COLORS.map(color => (
              <button
                key={color}
                onClick={() => setEditData({ ...editData, color })}
                className={cn(
                  'w-6 h-6 rounded-full border-2 transition-transform',
                  editData.color === color
                    ? 'border-gray-900 dark:border-white scale-110'
                    : 'border-transparent hover:scale-110'
                )}
                style={{ backgroundColor: color }}
              />
            ))}
          </div>
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-2">Status Mapping</label>
          <div className="flex flex-wrap gap-2">
            {statuses.map(status => (
              <button
                key={status.name}
                onClick={() => toggleStatusMapping(editData, setEditData, status.name)}
                className={cn(
                  'px-2 py-1 rounded text-xs font-medium border transition-colors',
                  (editData.status_mapping || []).includes(status.name)
                    ? 'bg-brand-100 border-brand-300 text-brand-700 dark:bg-brand-900/30 dark:border-brand-700 dark:text-brand-400'
                    : 'bg-gray-100 border-gray-200 text-gray-600 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-400 hover:border-gray-300'
                )}
              >
                {status.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <button onClick={onCancelEdit} className="btn-secondary text-sm">
            <X size={14} className="mr-1" />
            Cancel
          </button>
          <button onClick={handleSave} className="btn-primary text-sm">
            <Save size={14} className="mr-1" />
            Save
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg group">
      {/* Drag handle */}
      <div className="flex flex-col gap-1">
        <button
          onClick={() => onMove(-1)}
          disabled={index === 0}
          className="p-0.5 text-gray-400 hover:text-gray-600 disabled:opacity-30"
        >
          ▲
        </button>
        <button
          onClick={() => onMove(1)}
          disabled={index === totalColumns - 1}
          className="p-0.5 text-gray-400 hover:text-gray-600 disabled:opacity-30"
        >
          ▼
        </button>
      </div>

      {/* Color indicator */}
      <div
        className="w-3 h-8 rounded"
        style={{ backgroundColor: column.color || DEFAULT_COLORS[0] }}
      />

      {/* Column info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-900 dark:text-gray-100">
            {column.name}
          </span>
          {column.wip_limit && (
            <span className="text-xs px-1.5 py-0.5 bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 rounded">
              WIP: {column.wip_limit}
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-1 mt-1">
          {(column.status_mapping || []).map(status => (
            <span
              key={status}
              className="text-xs px-1.5 py-0.5 bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-400 rounded"
            >
              {status}
            </span>
          ))}
          {(!column.status_mapping || column.status_mapping.length === 0) && (
            <span className="text-xs text-gray-400 italic">No statuses mapped</span>
          )}
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
        <button
          onClick={onEdit}
          className="p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700 rounded"
        >
          <Edit2 size={14} />
        </button>
        <button
          onClick={onDelete}
          className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-gray-200 dark:hover:bg-gray-700 rounded"
        >
          <Trash2 size={14} />
        </button>
      </div>
    </div>
  )
}
