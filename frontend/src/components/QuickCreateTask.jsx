import { useState, useRef, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, X, Calendar, Flag } from 'lucide-react'
import { tasksApi } from '../services/api'
import { useOrganization } from '../contexts/OrganizationContext'
import { cn } from '../utils/cn'
import { formatApiError, organizationIdForNewTask } from '../utils/createTaskRequest'

const PRIORITIES = [
  { value: 'low', label: 'Low', color: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300' },
  { value: 'normal', label: 'Normal', color: 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300' },
  { value: 'high', label: 'High', color: 'bg-orange-100 text-orange-700 dark:bg-orange-900 dark:text-orange-300' },
  { value: 'urgent', label: 'Urgent', color: 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300' },
]

export default function QuickCreateTask() {
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [dueDate, setDueDate] = useState('')
  const [priority, setPriority] = useState('normal')
  const [quickError, setQuickError] = useState('')
  const inputRef = useRef(null)
  const queryClient = useQueryClient()
  const { currentOrganization, organizations } = useOrganization()

  const close = () => {
    setOpen(false)
    setTitle('')
    setDueDate('')
    setPriority('normal')
    setQuickError('')
  }

  const createMutation = useMutation({
    mutationFn: ({ organizationId, task }) => tasksApi.create(task, organizationId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['pending'] })
      queryClient.invalidateQueries({ queryKey: ['statistics'] })
      queryClient.invalidateQueries({ queryKey: ['dueSoon'] })
      queryClient.invalidateQueries({ queryKey: ['overdue'] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      close()
    },
  })

  useEffect(() => {
    if (open && inputRef.current) {
      // Small delay for the sheet animation to start
      setTimeout(() => inputRef.current?.focus(), 100)
    }
  }, [open])

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!title.trim()) return
    const organizationId = organizationIdForNewTask(currentOrganization, organizations)
    if (!organizationId) {
      setQuickError('Choose a workspace first. A task has to belong to one.')
      return
    }
    setQuickError('')
    // No project and no explicit assignee: the backend attaches this to the
    // workspace default project and owns it as the creator.
    createMutation.mutate({
      organizationId,
      task: {
        mode: 'task',
        item_type: 'task.generic',
        title: title.trim(),
        priority,
        due_date: dueDate ? new Date(dueDate).toISOString() : null,
      },
    })
  }

  return (
    <>
      {/* FAB */}
      <button
        onClick={() => setOpen(true)}
        className="fixed right-4 bottom-20 lg:bottom-6 z-30 w-14 h-14 rounded-full bg-brand-600 hover:bg-brand-700 active:bg-brand-800 text-white shadow-lg shadow-brand-600/30 flex items-center justify-center transition-transform active:scale-95"
        aria-label="Quick create task"
      >
        <Plus size={28} strokeWidth={2.5} />
      </button>

      {/* Bottom sheet overlay */}
      {open && (
        <div className="fixed inset-0 z-50">
          <div className="absolute inset-0 bg-black/40" onClick={close} />
          <div className="absolute bottom-0 left-0 right-0 bg-white dark:bg-gray-900 rounded-t-2xl shadow-2xl animate-slide-up safe-area-inset-bottom">
            {/* Handle bar */}
            <div className="flex justify-center pt-3 pb-1">
              <div className="w-10 h-1 rounded-full bg-gray-300 dark:bg-gray-600" />
            </div>

            {/* Header */}
            <div className="flex items-center justify-between px-4 pb-3">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                New Task
              </h3>
              <button
                onClick={close}
                className="p-1.5 rounded-full text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:text-gray-300 dark:hover:bg-gray-800"
              >
                <X size={20} />
              </button>
            </div>

            {/* Form */}
            <form onSubmit={handleSubmit} className="px-4 pb-4 space-y-4">
              {/* Title */}
              <input
                ref={inputRef}
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="What needs to be done?"
                className="w-full text-base px-3 py-3 rounded-xl border border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                autoComplete="off"
              />

              {/* Priority chips */}
              <div>
                <label className="flex items-center gap-1.5 text-sm font-medium text-gray-600 dark:text-gray-400 mb-2">
                  <Flag size={14} />
                  Priority
                </label>
                <div className="flex gap-2">
                  {PRIORITIES.map((p) => (
                    <button
                      key={p.value}
                      type="button"
                      onClick={() => setPriority(p.value)}
                      className={cn(
                        'flex-1 py-2 rounded-lg text-sm font-medium transition-all',
                        priority === p.value
                          ? `${p.color} ring-2 ring-brand-500 ring-offset-1 dark:ring-offset-gray-900`
                          : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
                      )}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Due date */}
              <div>
                <label className="flex items-center gap-1.5 text-sm font-medium text-gray-600 dark:text-gray-400 mb-2">
                  <Calendar size={14} />
                  Due date
                </label>
                <input
                  type="datetime-local"
                  value={dueDate}
                  onChange={(e) => setDueDate(e.target.value)}
                  className="w-full px-3 py-2.5 rounded-xl border border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>

              {/* Submit */}
              <button
                type="submit"
                disabled={!title.trim() || createMutation.isPending}
                className="w-full py-3 rounded-xl bg-brand-600 hover:bg-brand-700 disabled:bg-gray-300 disabled:dark:bg-gray-700 text-white font-semibold text-base transition-colors disabled:cursor-not-allowed"
              >
                {createMutation.isPending ? 'Creating...' : 'Create Task'}
              </button>

              {(quickError || createMutation.isError) && (
                <p role="alert" className="text-sm text-red-600 dark:text-red-400 text-center">
                  {quickError || formatApiError(createMutation.error, 'The task could not be created.')}
                </p>
              )}
            </form>
          </div>
        </div>
      )}
    </>
  )
}
