import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  X,
  Calendar,
  Clock,
  Bell,
  BellOff,
  Flag,
  CheckCircle,
  Circle,
  Repeat,
  Edit3,
  Trash2,
  AlarmClock,
  Tag,
  FileText,
  ChevronDown,
  ChevronUp,
  User,
  Users,
} from 'lucide-react'
import { tasksApi, itemsApi } from '../services/api'
import { cn } from '../utils/cn'
import { formatSmartDate, formatRelativeTime, getPriorityClasses } from '../utils/format'
import { formatScopedTeamName } from '../utils/teamLabels'
import CommentsSection from './CommentsSection'
import DescriptionEditor from './DescriptionEditor'
import UserSearchInput from './UserSearchInput'

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', color: 'text-gray-500' },
  { value: 'normal', label: 'Normal', color: 'text-blue-500' },
  { value: 'high', label: 'High', color: 'text-orange-500' },
  { value: 'urgent', label: 'Urgent', color: 'text-red-500' },
]

const FREQUENCY_OPTIONS = [
  { value: 'one_time', label: 'Does not repeat' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'yearly', label: 'Yearly' },
]

const SNOOZE_OPTIONS = [
  { value: 15, label: '15 minutes' },
  { value: 30, label: '30 minutes' },
  { value: 60, label: '1 hour' },
  { value: 180, label: '3 hours' },
  { value: 480, label: '8 hours' },
  { value: 1440, label: '1 day' },
  { value: 10080, label: '1 week' },
]

export default function TaskDetailModal({ task, onClose, onUpdate, projectMembers = [] }) {
  const queryClient = useQueryClient()
  const [isEditing, setIsEditing] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [showSnoozeOptions, setShowSnoozeOptions] = useState(false)
  const [editData, setEditData] = useState({
    title: task.title || '',
    description: task.description || '',
    priority: task.priority || 'normal',
    due_date: task.due_date ? task.due_date.slice(0, 16) : '',
    reminder_time: task.reminder_time ? task.reminder_time.slice(0, 16) : '',
    is_recurring: task.is_recurring || false,
    frequency: task.frequency || 'one_time',
    tags: task.tags || '',
    estimated_hours: task.estimated_hours || '',
  })
  const [currentAssignee, setCurrentAssignee] = useState({
    id: task.assignee_id,
    email: task.assignee_email,
    name: task.assignee_name || task.assignee_email?.split('@')[0],
  })

  // Mutations
  const updateMutation = useMutation({
    mutationFn: (data) => tasksApi.update(task.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['statistics'] })
      setIsEditing(false)
      onUpdate?.()
    },
  })

  const completeMutation = useMutation({
    mutationFn: () => itemsApi.complete(task.id),
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['statistics'] })
      if (!response?.data?.pending) onClose()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => tasksApi.delete(task.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['statistics'] })
      onClose()
    },
  })

  const snoozeMutation = useMutation({
    mutationFn: (minutes) => tasksApi.snooze(task.id, { duration_minutes: minutes }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      setShowSnoozeOptions(false)
    },
  })

  const assignMutation = useMutation({
    mutationFn: (data) => itemsApi.assign(task.id, data),
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      // Also invalidate project members if a new member was added
      if (response.data.member_added && task.project) {
        queryClient.invalidateQueries({ queryKey: ['project-members', task.project] })
      }
      setCurrentAssignee({
        id: response.data.assignee_id,
        email: response.data.assignee_email,
        name: response.data.assignee_name || response.data.assignee?.name || response.data.assignee_email?.split('@')[0],
      })
    },
  })

  const handleAssigneeSelect = (member) => {
    if (member === null) {
      // Unassign
      assignMutation.mutate({
        assignee_id: null,
        assignee_email: null,
      })
    } else if (member.is_team) {
      assignMutation.mutate({
        assignment_group: member.team_key || member.team_name,
      })
    } else {
      // Assign to selected member
      assignMutation.mutate({
        assignee_id: member.user_id,
        assignee_email: member.user_email,
      })
    }
  }

  const handleSave = (e) => {
    e.preventDefault()
    // Description is handled separately by DescriptionEditor
    const data = {
      title: editData.title,
      priority: editData.priority,
      is_recurring: editData.is_recurring,
      frequency: editData.frequency,
      tags: editData.tags,
      due_date: editData.due_date ? new Date(editData.due_date).toISOString() : null,
      reminder_time: editData.reminder_time ? new Date(editData.reminder_time).toISOString() : null,
      estimated_hours: editData.estimated_hours ? parseFloat(editData.estimated_hours) : null,
    }
    updateMutation.mutate(data)
  }

  const priorityClasses = getPriorityClasses(task.priority)
  const isOverdue = task.due_date && new Date(task.due_date) < new Date() && (task.status_category !== 'done' && task.status !== 'completed')
  const isSilenced = task.silenced_until && new Date(task.silenced_until) > new Date()

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-3">
            <button
              onClick={() => (task.status_category !== 'done' && task.status !== 'completed') && completeMutation.mutate()}
              disabled={(task.status_category === 'done' || task.status === 'completed') || completeMutation.isPending}
              className={cn(
                'p-1 rounded-full transition-colors',
                (task.status_category === 'done' || task.status === 'completed')
                  ? 'text-green-500'
                  : 'text-gray-400 hover:text-green-500 dark:text-gray-500 dark:hover:text-green-400'
              )}
            >
              {(task.status_category === 'done' || task.status === 'completed') ? (
                <CheckCircle size={24} />
              ) : (
                <Circle size={24} />
              )}
            </button>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              {isEditing ? 'Edit Task' : 'Task Details'}
            </h2>
          </div>
          <div className="flex items-center gap-2">
            {!isEditing && (
              <>
                <button
                  onClick={() => setIsEditing(true)}
                  className="btn-icon"
                  title="Edit"
                >
                  <Edit3 size={18} />
                </button>
                <button
                  onClick={() => {
                    if (confirm('Are you sure you want to delete this task?')) {
                      deleteMutation.mutate()
                    }
                  }}
                  className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                  title="Delete"
                >
                  <Trash2 size={18} />
                </button>
              </>
            )}
            <button onClick={onClose} className="btn-icon">
              <X size={20} />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4">
          {isEditing ? (
            <form onSubmit={handleSave} className="space-y-4">
              {/* Title */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Title *
                </label>
                <input
                  type="text"
                  value={editData.title}
                  onChange={(e) => setEditData({ ...editData, title: e.target.value })}
                  className="input"
                  placeholder="What needs to be done?"
                  required
                  autoFocus
                />
              </div>

              {/* Description - handled inline via DescriptionEditor below */}

              {/* Due Date & Time */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  <Calendar size={14} className="inline mr-1" />
                  Due Date & Time
                </label>
                <input
                  type="datetime-local"
                  value={editData.due_date}
                  onChange={(e) => setEditData({ ...editData, due_date: e.target.value })}
                  className="input"
                />
              </div>

              {/* Reminder */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  <Bell size={14} className="inline mr-1" />
                  Reminder
                </label>
                <input
                  type="datetime-local"
                  value={editData.reminder_time}
                  onChange={(e) => setEditData({ ...editData, reminder_time: e.target.value })}
                  className="input"
                />
              </div>

              {/* Priority */}
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  <Flag size={14} className="inline mr-1" />
                  Priority
                </label>
                <select
                  value={editData.priority}
                  onChange={(e) => setEditData({ ...editData, priority: e.target.value })}
                  className="input"
                >
                  {PRIORITY_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </div>

              {/* Advanced Options Toggle */}
              <button
                type="button"
                onClick={() => setShowAdvanced(!showAdvanced)}
                className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
              >
                {showAdvanced ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                Advanced options
              </button>

              {showAdvanced && (
                <div className="space-y-4 pl-4 border-l-2 border-gray-200 dark:border-gray-700">
                  {/* Recurring */}
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      <Repeat size={14} className="inline mr-1" />
                      Repeat
                    </label>
                    <select
                      value={editData.frequency}
                      onChange={(e) => setEditData({
                        ...editData,
                        frequency: e.target.value,
                        is_recurring: e.target.value !== 'one_time',
                      })}
                      className="input"
                    >
                      {FREQUENCY_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  {/* Tags */}
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      <Tag size={14} className="inline mr-1" />
                      Tags
                    </label>
                    <input
                      type="text"
                      value={editData.tags}
                      onChange={(e) => setEditData({ ...editData, tags: e.target.value })}
                      className="input"
                      placeholder="work, personal, urgent (comma-separated)"
                    />
                  </div>

                  {/* Estimated Time */}
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      <Clock size={14} className="inline mr-1" />
                      Estimated Time (hours)
                    </label>
                    <input
                      type="number"
                      step="0.5"
                      min="0"
                      value={editData.estimated_hours}
                      onChange={(e) => setEditData({ ...editData, estimated_hours: e.target.value })}
                      className="input"
                      placeholder="e.g., 2.5"
                    />
                  </div>

                </div>
              )}

              {/* Actions */}
              <div className="flex gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setIsEditing(false)}
                  className="btn-secondary flex-1"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={!editData.title.trim() || updateMutation.isPending}
                  className="btn-primary flex-1"
                >
                  {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
                </button>
              </div>
            </form>
          ) : (
            <div className="space-y-4">
              {/* Title */}
              <div>
                <h3 className={cn(
                  'text-xl font-medium text-gray-900 dark:text-gray-100',
                  (task.status_category === 'done' || task.status === 'completed') && 'line-through text-gray-500'
                )}>
                  {task.title}
                </h3>
                <div className="mt-2">
                  <DescriptionEditor
                    itemId={task.id}
                    content={task.description || ''}
                    onSave={(html) => updateMutation.mutate({ description: html })}
                    isSaving={updateMutation.isPending}
                  />
                </div>
              </div>

              {/* Status Badges */}
              <div className="flex flex-wrap gap-2">
                <span className={cn('badge', priorityClasses.bg, priorityClasses.text)}>
                  {task.priority}
                </span>
                <span className={cn(
                  'badge',
                  (task.status_category === 'done' || task.status === 'completed') ? 'badge-success' :
                  task.status === 'in_progress' ? 'badge-info' : 'badge-gray'
                )}>
                  {task.status?.replace('_', ' ')}
                </span>
                {isOverdue && (
                  <span className="badge badge-critical">Overdue</span>
                )}
                {isSilenced && (
                  <span className="badge badge-warning">
                    <BellOff size={12} className="mr-1" />
                    Snoozed
                  </span>
                )}
                {task.is_recurring && (
                  <span className="badge badge-info">
                    <Repeat size={12} className="mr-1" />
                    {task.frequency}
                  </span>
                )}
              </div>

              {/* Assignee Section */}
              <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
                <div className="flex items-center gap-2 mb-2">
                  <User size={16} className="text-gray-500 dark:text-gray-400" />
                  <span className="text-sm text-gray-500 dark:text-gray-400">Assignee</span>
                  {assignMutation.isPending && (
                    <span className="text-xs text-gray-400">Saving...</span>
                  )}
                </div>
                <UserSearchInput
                  projectId={task.project}
                  organizationId={task.organization_id}
                  organizationSlug={task.organization_slug}
                  organizationName={task.organization_name}
                  onSelect={handleAssigneeSelect}
                  currentValue={currentAssignee.email ? {
                    id: currentAssignee.id,
                    email: currentAssignee.email,
                    name: currentAssignee.name,
                  } : null}
                  placeholder="Search organization members..."
                  disabled={assignMutation.isPending}
                />
              </div>

              {/* Team */}
              {task.assignment_group && (
                <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
                  <div className="flex items-center gap-2 mb-1">
                    <Users size={16} className="text-gray-500 dark:text-gray-400" />
                    <span className="text-sm text-gray-500 dark:text-gray-400">Team</span>
                  </div>
                  <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                    {formatScopedTeamName({
                      canonical_name: task.assignment_group,
                      display_name: task.assignment_group,
                    }, {
                      organization_name: task.organization_name,
                      organization_slug: task.organization_slug,
                    })}
                  </p>
                </div>
              )}

              {/* Details Grid */}
              <div className="grid gap-3 text-sm">
                {/* Due Date */}
                {task.due_date && (
                  <div className="flex items-center gap-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
                    <Calendar size={18} className={cn(
                      isOverdue ? 'text-red-500' : 'text-gray-500 dark:text-gray-400'
                    )} />
                    <div>
                      <p className="text-gray-500 dark:text-gray-400">Due</p>
                      <p className={cn(
                        'font-medium',
                        isOverdue ? 'text-red-600 dark:text-red-400' : 'text-gray-900 dark:text-gray-100'
                      )}>
                        {formatSmartDate(task.due_date)}
                      </p>
                    </div>
                  </div>
                )}

                {/* Reminder */}
                {task.reminder_time && (
                  <div className="flex items-center gap-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
                    <Bell size={18} className="text-gray-500 dark:text-gray-400" />
                    <div>
                      <p className="text-gray-500 dark:text-gray-400">Reminder</p>
                      <p className="font-medium text-gray-900 dark:text-gray-100">
                        {formatSmartDate(task.reminder_time)}
                      </p>
                    </div>
                  </div>
                )}

                {/* Snoozed Until */}
                {isSilenced && (
                  <div className="flex items-center gap-3 p-3 rounded-lg bg-yellow-50 dark:bg-yellow-900/20">
                    <AlarmClock size={18} className="text-yellow-600 dark:text-yellow-400" />
                    <div>
                      <p className="text-yellow-700 dark:text-yellow-300">Snoozed until</p>
                      <p className="font-medium text-yellow-800 dark:text-yellow-200">
                        {formatSmartDate(task.silenced_until)}
                      </p>
                    </div>
                  </div>
                )}

                {/* Tags */}
                {task.tags && (
                  <div className="flex items-center gap-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
                    <Tag size={18} className="text-gray-500 dark:text-gray-400" />
                    <div className="flex flex-wrap gap-1">
                      {task.tags.split(',').map((tag, i) => (
                        <span key={i} className="badge badge-gray text-xs">
                          {tag.trim()}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {/* Estimated/Actual Hours */}
                {(task.estimated_hours || task.actual_hours) && (
                  <div className="flex items-center gap-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
                    <Clock size={18} className="text-gray-500 dark:text-gray-400" />
                    <div>
                      <p className="text-gray-500 dark:text-gray-400">Time</p>
                      <p className="font-medium text-gray-900 dark:text-gray-100">
                        {task.actual_hours ? `${task.actual_hours}h actual` : ''}
                        {task.actual_hours && task.estimated_hours ? ' / ' : ''}
                        {task.estimated_hours ? `${task.estimated_hours}h estimated` : ''}
                      </p>
                    </div>
                  </div>
                )}

                {/* Created/Updated */}
                <div className="flex items-center gap-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
                  <FileText size={18} className="text-gray-500 dark:text-gray-400" />
                  <div className="text-gray-500 dark:text-gray-400">
                    <p>Created {formatRelativeTime(task.created_at)}</p>
                    {task.updated_at !== task.created_at && (
                      <p>Updated {formatRelativeTime(task.updated_at)}</p>
                    )}
                    {task.completed_at && (
                      <p>Completed {formatRelativeTime(task.completed_at)}</p>
                    )}
                  </div>
                </div>
              </div>

              {/* Snooze Section */}
              {(task.status_category !== 'done' && task.status !== 'completed') && (
                <div className="pt-2">
                  <button
                    onClick={() => setShowSnoozeOptions(!showSnoozeOptions)}
                    className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
                  >
                    <AlarmClock size={16} />
                    Snooze notifications
                    {showSnoozeOptions ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                  </button>
                  {showSnoozeOptions && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {SNOOZE_OPTIONS.map((opt) => (
                        <button
                          key={opt.value}
                          onClick={() => snoozeMutation.mutate(opt.value)}
                          disabled={snoozeMutation.isPending}
                          className="badge badge-gray hover:bg-gray-200 dark:hover:bg-gray-600 cursor-pointer"
                        >
                          {opt.label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Comments Section (for project tasks) */}
              {(task.project || projectMembers.length > 0) && (
                <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
                  <CommentsSection itemId={task.id} />
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer Actions (View Mode) */}
        {!isEditing && (task.status_category !== 'done' && task.status !== 'completed') && (
          <div className="p-4 border-t border-gray-200 dark:border-gray-700">
            <button
              onClick={() => completeMutation.mutate()}
              disabled={completeMutation.isPending}
              className="btn-primary w-full flex items-center justify-center gap-2"
            >
              <CheckCircle size={18} />
              {completeMutation.isPending ? 'Completing...' : 'Mark as Complete'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
