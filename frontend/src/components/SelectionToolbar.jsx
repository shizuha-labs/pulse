import { useState } from 'react'
import {
  Check,
  Trash2,
  X,
  ChevronDown,
  CheckSquare,
  Square,
  Flag,
  Clock,
} from 'lucide-react'
import { cn } from '../utils/cn'

const STATUS_OPTIONS = [
  { value: 'pending', label: 'Pending' },
  { value: 'in_progress', label: 'In Progress' },
  { value: 'completed', label: 'Completed' },
]

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', color: 'text-gray-500' },
  { value: 'normal', label: 'Normal', color: 'text-blue-500' },
  { value: 'high', label: 'High', color: 'text-orange-500' },
  { value: 'urgent', label: 'Urgent', color: 'text-red-500' },
]

function Dropdown({ label, icon: Icon, options, onSelect, disabled }) {
  const [isOpen, setIsOpen] = useState(false)

  return (
    <div className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        disabled={disabled}
        className={cn(
          'flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border transition-colors',
          'border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800',
          'hover:bg-gray-50 dark:hover:bg-gray-700',
          'disabled:opacity-50 disabled:cursor-not-allowed'
        )}
      >
        {Icon && <Icon size={14} />}
        <span>{label}</span>
        <ChevronDown size={14} />
      </button>

      {isOpen && (
        <>
          <div
            className="fixed inset-0 z-30"
            onClick={() => setIsOpen(false)}
          />
          <div className="absolute top-full left-0 mt-1 z-40 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg min-w-[140px] py-1">
            {options.map((option) => (
              <button
                key={option.value}
                onClick={() => {
                  onSelect(option.value)
                  setIsOpen(false)
                }}
                className={cn(
                  'w-full text-left px-3 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700',
                  'text-gray-700 dark:text-gray-300',
                  option.color
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

export default function SelectionToolbar({
  selectedCount,
  totalCount,
  allSelected,
  someSelected,
  onSelectAll,
  onDeselectAll,
  onComplete,
  onDelete,
  onSetStatus,
  onSetPriority,
  isLoading = false,
}) {
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)

  if (selectedCount === 0) {
    return null
  }

  return (
    <div className="sticky top-0 z-40 bg-brand-50 dark:bg-brand-950 border border-brand-200 dark:border-brand-800 rounded-lg p-3 mb-4 shadow-sm">
      <div className="flex flex-wrap items-center gap-3">
        {/* Selection info */}
        <div className="flex items-center gap-2">
          <button
            onClick={allSelected ? onDeselectAll : onSelectAll}
            className="p-1 rounded hover:bg-brand-100 dark:hover:bg-brand-900"
            title={allSelected ? 'Deselect all' : 'Select all'}
          >
            {allSelected ? (
              <CheckSquare size={18} className="text-brand-600 dark:text-brand-400" />
            ) : someSelected ? (
              <div className="relative">
                <Square size={18} className="text-brand-600 dark:text-brand-400" />
                <div className="absolute inset-0 flex items-center justify-center">
                  <div className="w-2 h-0.5 bg-brand-600 dark:bg-brand-400" />
                </div>
              </div>
            ) : (
              <Square size={18} className="text-gray-400" />
            )}
          </button>
          <span className="text-sm font-medium text-brand-700 dark:text-brand-300">
            {selectedCount} of {totalCount} selected
          </span>
        </div>

        {/* Divider */}
        <div className="h-5 w-px bg-brand-200 dark:bg-brand-700" />

        {/* Actions */}
        <div className="flex items-center gap-2">
          {/* Complete All */}
          <button
            onClick={onComplete}
            disabled={isLoading}
            className={cn(
              'flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg transition-colors',
              'bg-green-600 text-white hover:bg-green-700',
              'disabled:opacity-50 disabled:cursor-not-allowed'
            )}
          >
            <Check size={14} />
            <span>Complete</span>
          </button>

          {/* Set Status Dropdown */}
          <Dropdown
            label="Status"
            icon={Clock}
            options={STATUS_OPTIONS}
            onSelect={onSetStatus}
            disabled={isLoading}
          />

          {/* Set Priority Dropdown */}
          <Dropdown
            label="Priority"
            icon={Flag}
            options={PRIORITY_OPTIONS}
            onSelect={onSetPriority}
            disabled={isLoading}
          />

          {/* Delete */}
          {!showDeleteConfirm ? (
            <button
              onClick={() => setShowDeleteConfirm(true)}
              disabled={isLoading}
              className={cn(
                'flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg transition-colors',
                'text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30',
                'disabled:opacity-50 disabled:cursor-not-allowed'
              )}
            >
              <Trash2 size={14} />
              <span>Delete</span>
            </button>
          ) : (
            <div className="flex items-center gap-2 bg-red-50 dark:bg-red-900/30 px-3 py-1.5 rounded-lg">
              <span className="text-sm text-red-700 dark:text-red-300">
                Delete {selectedCount} tasks?
              </span>
              <button
                onClick={() => {
                  onDelete()
                  setShowDeleteConfirm(false)
                }}
                disabled={isLoading}
                className="px-2 py-1 text-xs font-medium text-white bg-red-600 rounded hover:bg-red-700"
              >
                Yes
              </button>
              <button
                onClick={() => setShowDeleteConfirm(false)}
                className="px-2 py-1 text-xs font-medium text-red-700 dark:text-red-300 hover:underline"
              >
                No
              </button>
            </div>
          )}
        </div>

        {/* Cancel */}
        <div className="ml-auto">
          <button
            onClick={onDeselectAll}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
          >
            <X size={14} />
            <span>Cancel</span>
          </button>
        </div>
      </div>
    </div>
  )
}
