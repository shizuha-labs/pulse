import { useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { X, Clock, Calendar, FileText } from 'lucide-react'
import { workLogsApi } from '../services/api'
import { cn } from '../utils/cn'

export default function WorkLogModal({
  itemId,
  itemKey,
  currentTimeSpent = 0,
  remainingEstimate,
  isOpen,
  onClose,
}) {
  const queryClient = useQueryClient()
  const [formData, setFormData] = useState({
    hours: '',
    minutes: '',
    worked_date: new Date().toISOString().slice(0, 10),
    description: '',
    adjustRemaining: 'auto', // auto, manual, leave
    newRemainingHours: '',
    newRemainingMinutes: '',
  })

  // Reset form when modal opens
  useEffect(() => {
    if (isOpen) {
      setFormData({
        hours: '',
        minutes: '',
        worked_date: new Date().toISOString().slice(0, 10),
        description: '',
        adjustRemaining: 'auto',
        newRemainingHours: remainingEstimate ? Math.floor(remainingEstimate / 60).toString() : '',
        newRemainingMinutes: remainingEstimate ? (remainingEstimate % 60).toString() : '',
      })
    }
  }, [isOpen, remainingEstimate])

  const createMutation = useMutation({
    mutationFn: (data) => workLogsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task'] })
      queryClient.invalidateQueries({ queryKey: ['work-logs'] })
      onClose()
    },
  })

  const handleSubmit = (e) => {
    e.preventDefault()

    const hours = parseInt(formData.hours) || 0
    const minutes = parseInt(formData.minutes) || 0
    const totalMinutes = hours * 60 + minutes

    if (totalMinutes <= 0) {
      return
    }

    const data = {
      item: itemId,
      duration_minutes: totalMinutes,
      worked_date: formData.worked_date,
      description: formData.description,
    }

    // Handle remaining estimate adjustment
    if (formData.adjustRemaining === 'manual') {
      const newRemHours = parseInt(formData.newRemainingHours) || 0
      const newRemMinutes = parseInt(formData.newRemainingMinutes) || 0
      data.remaining_estimate_minutes = newRemHours * 60 + newRemMinutes
    } else if (formData.adjustRemaining === 'auto' && remainingEstimate) {
      // Auto-reduce remaining by logged time
      const newRemaining = Math.max(0, remainingEstimate - totalMinutes)
      data.remaining_estimate_minutes = newRemaining
    }
    // 'leave' - don't send remaining_estimate_minutes

    createMutation.mutate(data)
  }

  const formatDuration = (minutes) => {
    if (!minutes) return '0m'
    const h = Math.floor(minutes / 60)
    const m = minutes % 60
    if (h && m) return `${h}h ${m}m`
    if (h) return `${h}h`
    return `${m}m`
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onClose}
      />

      {/* Modal */}
      <div className="relative bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-md mx-4">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            Log Work
          </h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded"
          >
            <X size={20} className="text-gray-400" />
          </button>
        </div>

        {/* Content */}
        <form onSubmit={handleSubmit} className="p-4 space-y-4">
          {/* Item reference */}
          {itemKey && (
            <div className="text-sm text-gray-500 dark:text-gray-400">
              Logging time for <span className="font-medium text-gray-900 dark:text-gray-100">{itemKey}</span>
            </div>
          )}

          {/* Time spent */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              <Clock size={14} className="inline mr-1" />
              Time Spent *
            </label>
            <div className="flex gap-2">
              <div className="flex-1">
                <input
                  type="number"
                  min="0"
                  value={formData.hours}
                  onChange={(e) => setFormData({ ...formData, hours: e.target.value })}
                  placeholder="0"
                  className="input w-full"
                />
                <span className="text-xs text-gray-500 mt-1">hours</span>
              </div>
              <div className="flex-1">
                <input
                  type="number"
                  min="0"
                  max="59"
                  value={formData.minutes}
                  onChange={(e) => setFormData({ ...formData, minutes: e.target.value })}
                  placeholder="0"
                  className="input w-full"
                />
                <span className="text-xs text-gray-500 mt-1">minutes</span>
              </div>
            </div>
          </div>

          {/* Date */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              <Calendar size={14} className="inline mr-1" />
              Date
            </label>
            <input
              type="date"
              value={formData.worked_date}
              onChange={(e) => setFormData({ ...formData, worked_date: e.target.value })}
              className="input w-full"
            />
          </div>

          {/* Description */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              <FileText size={14} className="inline mr-1" />
              Work Description
            </label>
            <textarea
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              placeholder="What did you work on?"
              rows={3}
              className="input w-full"
            />
          </div>

          {/* Current tracking info */}
          <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded-lg text-sm">
            <div className="flex justify-between">
              <span className="text-gray-500">Time Logged:</span>
              <span className="font-medium">{formatDuration(currentTimeSpent)}</span>
            </div>
            {remainingEstimate !== null && (
              <div className="flex justify-between mt-1">
                <span className="text-gray-500">Remaining:</span>
                <span className="font-medium">{formatDuration(remainingEstimate)}</span>
              </div>
            )}
          </div>

          {/* Remaining estimate adjustment */}
          {remainingEstimate !== null && (
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Adjust remaining estimate
              </label>
              <div className="space-y-2">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="adjustRemaining"
                    value="auto"
                    checked={formData.adjustRemaining === 'auto'}
                    onChange={(e) => setFormData({ ...formData, adjustRemaining: e.target.value })}
                    className="text-brand-500"
                  />
                  <span className="text-sm">Auto-adjust (reduce by logged time)</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="adjustRemaining"
                    value="leave"
                    checked={formData.adjustRemaining === 'leave'}
                    onChange={(e) => setFormData({ ...formData, adjustRemaining: e.target.value })}
                    className="text-brand-500"
                  />
                  <span className="text-sm">Leave unchanged</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="adjustRemaining"
                    value="manual"
                    checked={formData.adjustRemaining === 'manual'}
                    onChange={(e) => setFormData({ ...formData, adjustRemaining: e.target.value })}
                    className="text-brand-500"
                  />
                  <span className="text-sm">Set manually</span>
                </label>
                {formData.adjustRemaining === 'manual' && (
                  <div className="flex gap-2 ml-6">
                    <input
                      type="number"
                      min="0"
                      value={formData.newRemainingHours}
                      onChange={(e) => setFormData({ ...formData, newRemainingHours: e.target.value })}
                      placeholder="0"
                      className="input w-20"
                    />
                    <span className="text-sm text-gray-500 self-center">h</span>
                    <input
                      type="number"
                      min="0"
                      max="59"
                      value={formData.newRemainingMinutes}
                      onChange={(e) => setFormData({ ...formData, newRemainingMinutes: e.target.value })}
                      placeholder="0"
                      className="input w-20"
                    />
                    <span className="text-sm text-gray-500 self-center">m</span>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Error message */}
          {createMutation.isError && (
            <div className="text-sm text-red-600 dark:text-red-400">
              Failed to log work. Please try again.
            </div>
          )}

          {/* Actions */}
          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="btn-secondary flex-1"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || (!formData.hours && !formData.minutes)}
              className="btn-primary flex-1"
            >
              {createMutation.isPending ? 'Logging...' : 'Log Work'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
