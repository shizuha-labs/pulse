import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Zap, ChevronDown, X, Calendar, Target } from 'lucide-react'
import { sprintsApi } from '../services/api'
import { cn } from '../utils/cn'
import { format } from 'date-fns'

const STORY_POINTS = [1, 2, 3, 5, 8, 13, 21]

export default function SprintSelector({
  projectId,
  currentSprintId,
  currentStoryPoints,
  onSprintChange,
  onStoryPointsChange,
  disabled = false
}) {
  const [showSprintDropdown, setShowSprintDropdown] = useState(false)
  const [showPointsDropdown, setShowPointsDropdown] = useState(false)

  // Fetch sprints for this project
  const { data: sprints = [], isLoading } = useQuery({
    queryKey: ['sprints', projectId],
    queryFn: async () => {
      if (!projectId) return []
      const res = await sprintsApi.list({ project: projectId })
      return res.data.results || res.data || []
    },
    enabled: !!projectId,
  })

  // Find current sprint object
  const currentSprint = sprints.find(s => s.id === currentSprintId)

  // Close dropdowns when clicking outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (showSprintDropdown || showPointsDropdown) {
        const isDropdownClick = e.target.closest('[data-sprint-dropdown]')
        if (!isDropdownClick) {
          setShowSprintDropdown(false)
          setShowPointsDropdown(false)
        }
      }
    }
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [showSprintDropdown, showPointsDropdown])

  const getStatusBadge = (status) => {
    switch (status) {
      case 'active':
        return 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
      case 'completed':
        return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
      default: // planning
        return 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400'
    }
  }

  if (!projectId) {
    return null
  }

  return (
    <div className="space-y-4">
      {/* Sprint Selector */}
      <div>
        <label className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-2">
          <Zap size={14} />
          Sprint
        </label>
        <div className="relative mt-1" data-sprint-dropdown>
          <button
            onClick={() => !disabled && setShowSprintDropdown(!showSprintDropdown)}
            disabled={disabled || isLoading}
            className="w-full flex items-center gap-3 p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-left disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {currentSprint ? (
              <>
                <div className="w-8 h-8 rounded-lg bg-purple-100 dark:bg-purple-900/30 flex items-center justify-center">
                  <Zap size={16} className="text-purple-600 dark:text-purple-400" />
                </div>
                <div className="flex-1 min-w-0">
                  <span className="font-medium text-gray-900 dark:text-gray-100 block truncate">
                    {currentSprint.name}
                  </span>
                  <span className={cn(
                    'text-xs px-1.5 py-0.5 rounded',
                    getStatusBadge(currentSprint.status)
                  )}>
                    {currentSprint.status}
                  </span>
                </div>
              </>
            ) : (
              <>
                <div className="w-8 h-8 rounded-lg bg-gray-100 dark:bg-gray-800 flex items-center justify-center">
                  <Zap size={16} className="text-gray-400" />
                </div>
                <span className="text-gray-400">Backlog</span>
              </>
            )}
            <ChevronDown size={16} className="ml-auto text-gray-400 flex-shrink-0" />
          </button>

          {showSprintDropdown && (
            <div className="absolute z-40 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-60 overflow-y-auto">
              {/* Backlog option */}
              <button
                onClick={() => {
                  onSprintChange(null)
                  setShowSprintDropdown(false)
                }}
                className={cn(
                  'w-full flex items-center gap-3 p-3 transition-colors',
                  !currentSprintId
                    ? 'bg-brand-50 dark:bg-brand-900/20'
                    : 'hover:bg-gray-100 dark:hover:bg-gray-700'
                )}
              >
                <div className="w-6 h-6 rounded bg-gray-100 dark:bg-gray-700 flex items-center justify-center">
                  <X size={14} className="text-gray-500" />
                </div>
                <span className="text-gray-900 dark:text-white text-sm">Backlog</span>
              </button>

              {sprints.length === 0 ? (
                <div className="p-3 text-sm text-gray-500 dark:text-gray-400 text-center">
                  No sprints available
                </div>
              ) : (
                sprints.map((sprint) => (
                  <button
                    key={sprint.id}
                    onClick={() => {
                      onSprintChange(sprint.id)
                      setShowSprintDropdown(false)
                    }}
                    className={cn(
                      'w-full flex items-center gap-3 p-3 transition-colors',
                      sprint.id === currentSprintId
                        ? 'bg-brand-50 dark:bg-brand-900/20'
                        : 'hover:bg-gray-100 dark:hover:bg-gray-700'
                    )}
                  >
                    <div className="w-6 h-6 rounded bg-purple-100 dark:bg-purple-900/30 flex items-center justify-center">
                      <Zap size={14} className="text-purple-600 dark:text-purple-400" />
                    </div>
                    <div className="flex-1 min-w-0 text-left">
                      <span className="text-gray-900 dark:text-white text-sm block truncate">
                        {sprint.name}
                      </span>
                      <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                        <span className={cn(
                          'px-1.5 py-0.5 rounded',
                          getStatusBadge(sprint.status)
                        )}>
                          {sprint.status}
                        </span>
                        {sprint.start_date && sprint.end_date && (
                          <span>
                            {format(new Date(sprint.start_date), 'MMM d')} - {format(new Date(sprint.end_date), 'MMM d')}
                          </span>
                        )}
                      </div>
                    </div>
                  </button>
                ))
              )}
            </div>
          )}
        </div>
      </div>

      {/* Story Points Selector */}
      <div>
        <label className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-2">
          <Target size={14} />
          Story Points
        </label>
        <div className="relative mt-1" data-sprint-dropdown>
          <button
            onClick={() => !disabled && setShowPointsDropdown(!showPointsDropdown)}
            disabled={disabled}
            className="w-full flex items-center gap-3 p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 text-left disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {currentStoryPoints ? (
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 rounded-lg bg-amber-100 dark:bg-amber-900/30 flex items-center justify-center">
                  <span className="text-sm font-bold text-amber-600 dark:text-amber-400">
                    {currentStoryPoints}
                  </span>
                </div>
                <span className="text-gray-900 dark:text-gray-100">
                  {currentStoryPoints} {currentStoryPoints === 1 ? 'point' : 'points'}
                </span>
              </div>
            ) : (
              <>
                <div className="w-8 h-8 rounded-lg bg-gray-100 dark:bg-gray-800 flex items-center justify-center">
                  <Target size={16} className="text-gray-400" />
                </div>
                <span className="text-gray-400">Not estimated</span>
              </>
            )}
            <ChevronDown size={16} className="ml-auto text-gray-400 flex-shrink-0" />
          </button>

          {showPointsDropdown && (
            <div className="absolute z-40 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg">
              {/* No estimate option */}
              <button
                onClick={() => {
                  onStoryPointsChange(null)
                  setShowPointsDropdown(false)
                }}
                className={cn(
                  'w-full flex items-center gap-3 p-3 transition-colors',
                  !currentStoryPoints
                    ? 'bg-brand-50 dark:bg-brand-900/20'
                    : 'hover:bg-gray-100 dark:hover:bg-gray-700'
                )}
              >
                <X size={16} className="text-gray-500" />
                <span className="text-gray-900 dark:text-white text-sm">No estimate</span>
              </button>

              {/* Fibonacci points */}
              <div className="grid grid-cols-4 gap-2 p-3 border-t border-gray-200 dark:border-gray-700">
                {STORY_POINTS.map((points) => (
                  <button
                    key={points}
                    onClick={() => {
                      onStoryPointsChange(points)
                      setShowPointsDropdown(false)
                    }}
                    className={cn(
                      'p-2 rounded-lg text-center font-medium transition-colors',
                      points === currentStoryPoints
                        ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                        : 'bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600'
                    )}
                  >
                    {points}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
