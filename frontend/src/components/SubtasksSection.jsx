import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Plus,
  CheckCircle,
  Circle,
  ChevronRight,
  Loader2,
  Flag,
  User,
  X,
} from 'lucide-react'
import { itemsApi } from '../services/api'
import { cn } from '../utils/cn'
import { getPriorityClasses } from '../utils/format'

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low' },
  { value: 'normal', label: 'Normal' },
  { value: 'high', label: 'High' },
  { value: 'urgent', label: 'Urgent' },
]

export default function SubtasksSection({ parentItemId, projectId, projectMembers = [], embedded = false }) {
  const queryClient = useQueryClient()
  const [isCreating, setIsCreating] = useState(false)
  const [newTaskTitle, setNewTaskTitle] = useState('')
  const [newTaskPriority, setNewTaskPriority] = useState('normal')
  const [createError, setCreateError] = useState(null)

  // Fetch subtasks
  const { data: subtasksData, isLoading } = useQuery({
    queryKey: ['subtasks', parentItemId],
    queryFn: async () => {
      const res = await itemsApi.list({ parent_item: parentItemId })
      return res.data.results || res.data || []
    },
    enabled: !!parentItemId,
  })

  const subtasks = subtasksData || []

  // Create subtask mutation
  const createMutation = useMutation({
    mutationFn: (data) => itemsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['subtasks', parentItemId] })
      setNewTaskTitle('')
      setNewTaskPriority('normal')
      setIsCreating(false)
      setCreateError(null)
    },
    onError: (error) => {
      const data = error.response?.data
      if (data?.project) {
        setCreateError('You are not a member of this project. Ask a project admin to add you.')
      } else {
        const msg = typeof data === 'string' ? data
          : data?.detail || data?.error || Object.values(data || {}).flat().join(', ')
          || 'Failed to create subtask'
        setCreateError(msg)
      }
    },
  })

  // Complete subtask mutation
  const completeMutation = useMutation({
    mutationFn: (id) => itemsApi.complete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['subtasks', parentItemId] })
    },
  })

  const handleCreateSubtask = (e) => {
    e.preventDefault()
    if (!newTaskTitle.trim()) return

    setCreateError(null)
    createMutation.mutate({
      title: newTaskTitle,
      priority: newTaskPriority,
      parent_item: parentItemId,
      project: projectId,
      mode: 'task',
    })
  }

  const completedCount = subtasks.filter(t => t.status === 'completed').length
  const totalCount = subtasks.length
  const progressPercent = totalCount > 0 ? Math.round((completedCount / totalCount) * 100) : 0

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-4">
        <Loader2 className="animate-spin text-gray-400" size={20} />
      </div>
    )
  }

  return (
    <div className={embedded ? 'space-y-1.5' : 'space-y-3'}>
      {/* Header with progress */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {embedded ? (
            <span className="text-xs text-gray-500 dark:text-gray-400 font-medium">
              Subtasks {totalCount > 0 && `(${completedCount}/${totalCount})`}
            </span>
          ) : (
            <h4 className="font-medium text-gray-900 dark:text-gray-100">
              Subtasks {totalCount > 0 && `(${completedCount}/${totalCount})`}
            </h4>
          )}
          {totalCount > 0 && (
            <div className="flex items-center gap-1.5">
              <div className={cn(
                'bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden',
                embedded ? 'w-16 h-1' : 'w-24 h-1.5'
              )}>
                <div
                  className="h-full bg-green-500 transition-all duration-300"
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
              <span className={cn(
                'text-gray-500 dark:text-gray-400',
                embedded ? 'text-[10px]' : 'text-xs'
              )}>
                {progressPercent}%
              </span>
            </div>
          )}
        </div>
        {!isCreating && (
          <button
            onClick={() => setIsCreating(true)}
            className={cn(
              'text-brand-600 dark:text-brand-400 hover:bg-brand-50 dark:hover:bg-brand-900/30 rounded transition-colors',
              embedded ? 'p-0.5' : 'btn-icon'
            )}
            title="Add subtask"
          >
            <Plus size={embedded ? 14 : 18} />
          </button>
        )}
      </div>

      {/* Subtask list */}
      {subtasks.length > 0 ? (
        <div className="space-y-1">
          {subtasks.map((subtask) => {
            const priorityClasses = getPriorityClasses(subtask.priority)
            const isCompleted = subtask.status === 'completed'

            return (
              <div
                key={subtask.id}
                className="flex items-center gap-2 p-2 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors group"
              >
                {/* Complete toggle */}
                <button
                  onClick={() => !isCompleted && completeMutation.mutate(subtask.id)}
                  disabled={isCompleted || completeMutation.isPending}
                  className={cn(
                    'flex-shrink-0 transition-colors',
                    isCompleted
                      ? 'text-green-500'
                      : 'text-gray-400 hover:text-green-500 dark:text-gray-500 dark:hover:text-green-400'
                  )}
                >
                  {isCompleted ? (
                    <CheckCircle size={18} />
                  ) : (
                    <Circle size={18} />
                  )}
                </button>

                {/* Task info */}
                <Link
                  to={`/tasks/${subtask.item_key || subtask.id}`}
                  className={cn(
                    'flex-1 min-w-0 text-sm transition-colors',
                    isCompleted
                      ? 'text-gray-400 dark:text-gray-500 line-through'
                      : 'text-gray-900 dark:text-gray-100 hover:text-brand-600 dark:hover:text-brand-400'
                  )}
                >
                  <span className="truncate block">{subtask.title}</span>
                </Link>

                {/* Priority */}
                {subtask.priority && subtask.priority !== 'normal' && (
                  <Flag
                    size={14}
                    className={cn('flex-shrink-0', priorityClasses.text)}
                    title={subtask.priority}
                  />
                )}

                {/* Assignee */}
                {subtask.assignee_email && (
                  <div
                    className="w-5 h-5 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center flex-shrink-0"
                    title={subtask.assignee_name || subtask.assignee_email}
                  >
                    <span className="text-[10px] text-brand-600 dark:text-brand-400 font-medium">
                      {(subtask.assignee_name || subtask.assignee_email)[0].toUpperCase()}
                    </span>
                  </div>
                )}

                {/* Status badge */}
                {subtask.status && subtask.status !== 'pending' && subtask.status !== 'completed' && (
                  <span className={cn(
                    'badge text-xs',
                    subtask.status === 'in_progress' ? 'badge-info' :
                    subtask.status === 'blocked' ? 'badge-warning' : 'badge-gray'
                  )}>
                    {subtask.status.replace('_', ' ')}
                  </span>
                )}

                {/* Item key */}
                {subtask.item_key && (
                  <span className="text-xs text-gray-400 dark:text-gray-500 flex-shrink-0">
                    {subtask.item_key}
                  </span>
                )}

                <ChevronRight
                  size={16}
                  className="text-gray-300 dark:text-gray-600 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0"
                />
              </div>
            )
          })}
        </div>
      ) : !isCreating ? (
        embedded ? null : (
          <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
            No subtasks yet. Click + to add one.
          </p>
        )
      ) : null}

      {/* Error message */}
      {createError && (
        <div className="flex items-start gap-2 p-2.5 bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 text-sm rounded-lg">
          <span className="flex-1">{createError}</span>
          <button onClick={() => setCreateError(null)} className="flex-shrink-0 text-red-400 hover:text-red-600">
            <X size={14} />
          </button>
        </div>
      )}

      {/* Create form */}
      {isCreating && (
        <form onSubmit={handleCreateSubtask} className="flex items-center gap-2 p-2 bg-gray-50 dark:bg-gray-800 rounded-lg">
          <Circle size={18} className="text-gray-300 dark:text-gray-600 flex-shrink-0" />
          <input
            type="text"
            value={newTaskTitle}
            onChange={(e) => setNewTaskTitle(e.target.value)}
            placeholder="Subtask title..."
            className="flex-1 bg-transparent border-none outline-none text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400"
            autoFocus
          />
          <select
            value={newTaskPriority}
            onChange={(e) => setNewTaskPriority(e.target.value)}
            className="text-xs bg-transparent border border-gray-200 dark:border-gray-700 rounded px-1.5 py-0.5"
          >
            {PRIORITY_OPTIONS.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={!newTaskTitle.trim() || createMutation.isPending}
            className="btn-primary btn-sm"
          >
            {createMutation.isPending ? (
              <Loader2 className="animate-spin h-4 w-4" />
            ) : (
              'Add'
            )}
          </button>
          <button
            type="button"
            onClick={() => {
              setIsCreating(false)
              setNewTaskTitle('')
            }}
            className="btn-icon text-gray-400 hover:text-gray-600"
          >
            <X size={16} />
          </button>
        </form>
      )}
    </div>
  )
}
