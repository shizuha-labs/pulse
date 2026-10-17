import { useState, useEffect } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Zap,
  Target,
  Calendar,
  Clock,
  CheckCircle2,
  Circle,
  Loader2,
  AlertCircle,
  MoreVertical,
  TrendingDown,
  Play,
} from 'lucide-react'
import { sprintsApi, projectsApi, itemsApi } from '../services/api'
import { cn } from '../utils/cn'
import { formatApiError } from '../utils/createTaskRequest'
import { moveTaskToColumn } from '../utils/moveTaskOnBoard'
import { format, differenceInDays } from 'date-fns'
import { PageLoader } from '../components/LoadingSpinner'

// Kanban columns
const COLUMNS = [
  { id: 'pending', label: 'To Do', icon: Circle, color: 'text-gray-500' },
  { id: 'in_progress', label: 'In Progress', icon: Loader2, color: 'text-blue-500' },
  { id: 'completed', label: 'Done', icon: CheckCircle2, color: 'text-green-500' },
]

export default function SprintBoard() {
  const { projectId, sprintId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [draggedItem, setDraggedItem] = useState(null)
  const [boardMoveError, setBoardMoveError] = useState('')
  const [showBurndown, setShowBurndown] = useState(false)

  // Fetch project
  const { data: project, isLoading: projectLoading } = useQuery({
    queryKey: ['project', projectId],
    queryFn: async () => {
      const res = await projectsApi.get(projectId)
      return res.data
    },
  })

  // Fetch sprint
  const { data: sprint, isLoading: sprintLoading } = useQuery({
    queryKey: ['sprint', sprintId],
    queryFn: async () => {
      const res = await sprintsApi.get(sprintId)
      return res.data
    },
  })

  // Fetch sprint items
  const { data: items = [], isLoading: itemsLoading } = useQuery({
    queryKey: ['sprint-items', sprintId],
    queryFn: async () => {
      const res = await sprintsApi.items(sprintId)
      return res.data.items || res.data || []
    },
  })

  // Fetch burndown data
  const { data: burndownData } = useQuery({
    queryKey: ['sprint-burndown', sprintId],
    queryFn: async () => {
      const res = await sprintsApi.burndown(sprintId)
      return res.data
    },
    enabled: sprint?.status === 'active' || sprint?.status === 'completed',
  })

  // Start sprint mutation
  const startMutation = useMutation({
    mutationFn: () => sprintsApi.start(sprintId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sprint', sprintId] })
      queryClient.invalidateQueries({ queryKey: ['sprints', projectId] })
    },
  })

  // Complete sprint mutation
  const completeMutation = useMutation({
    mutationFn: () => sprintsApi.complete(sprintId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sprint', sprintId] })
      queryClient.invalidateQueries({ queryKey: ['sprints', projectId] })
    },
  })

  const handleDragStart = (e, item) => {
    setDraggedItem(item)
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', item.id)
    e.target.classList.add('opacity-50')
  }

  const handleDragEnd = (e) => {
    e.target.classList.remove('opacity-50')
    setDraggedItem(null)
  }

  const handleDragOver = (e) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
  }

  const handleDrop = async (e, columnId) => {
    e.preventDefault()
    const item = draggedItem
    setDraggedItem(null)
    if (!item) return
    setBoardMoveError('')
    try {
      const result = await moveTaskToColumn(item, columnId, itemsApi)
      if (result.moved) {
        queryClient.invalidateQueries({ queryKey: ['sprint-items', sprintId] })
        queryClient.invalidateQueries({ queryKey: ['sprint', sprintId] })
      }
    } catch (err) {
      setBoardMoveError(formatApiError(err, 'This task could not move.'))
    }
  }

  const getItemsByStatus = (status) => items.filter(item => item.status === status)

  const getPriorityColor = (priority) => {
    switch (priority) {
      case 'urgent': return 'border-l-red-500'
      case 'high': return 'border-l-orange-500'
      case 'normal': return 'border-l-blue-500'
      case 'low': return 'border-l-gray-400'
      default: return 'border-l-gray-300'
    }
  }

  const isLoading = projectLoading || sprintLoading || itemsLoading

  if (isLoading) {
    return <PageLoader />
  }

  if (!project || !sprint) {
    return (
      <div className="text-center py-12">
        <AlertCircle className="mx-auto h-12 w-12 text-red-500" />
        <h3 className="mt-2 text-lg font-medium text-gray-900 dark:text-white">
          Sprint not found
        </h3>
        <Link to={`/projects/${projectId}`} className="btn-primary mt-4 inline-flex items-center gap-2">
          <ArrowLeft size={16} />
          Back to Project
        </Link>
      </div>
    )
  }

  const totalPoints = items.reduce((sum, item) => sum + (item.story_points || 0), 0)
  const completedPoints = items
    .filter(item => item.status === 'completed')
    .reduce((sum, item) => sum + (item.story_points || 0), 0)
  const progress = totalPoints > 0 ? Math.round((completedPoints / totalPoints) * 100) : 0
  const daysRemaining = sprint.end_date ? differenceInDays(new Date(sprint.end_date), new Date()) : null

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
        <div className="flex items-center gap-4">
          <Link to={`/projects/${projectId}/backlog`} className="btn-icon">
            <ArrowLeft size={20} />
          </Link>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-purple-100 dark:bg-purple-900/30 flex items-center justify-center">
              <Zap size={20} className="text-purple-600 dark:text-purple-400" />
            </div>
            <div>
              <h1 className="text-xl font-bold text-gray-900 dark:text-white">
                {sprint.name}
              </h1>
              <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
                <span className={cn(
                  'px-2 py-0.5 rounded text-xs',
                  sprint.status === 'active' && 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
                  sprint.status === 'planning' && 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
                  sprint.status === 'completed' && 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                )}>
                  {sprint.status}
                </span>
                {sprint.start_date && sprint.end_date && (
                  <span className="flex items-center gap-1">
                    <Calendar size={14} />
                    {format(new Date(sprint.start_date), 'MMM d')} - {format(new Date(sprint.end_date), 'MMM d')}
                  </span>
                )}
              </div>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-4">
          {/* Sprint Stats */}
          <div className="flex items-center gap-4 text-sm">
            <div className="flex items-center gap-2">
              <Target size={16} className="text-gray-400" />
              <span className="text-gray-900 dark:text-white font-medium">
                {completedPoints}/{totalPoints} pts
              </span>
            </div>
            {sprint.status === 'active' && daysRemaining !== null && (
              <div className={cn(
                'flex items-center gap-2',
                daysRemaining < 0 && 'text-red-500',
                daysRemaining <= 2 && daysRemaining >= 0 && 'text-amber-500'
              )}>
                <Clock size={16} />
                <span>{daysRemaining < 0 ? 'Overdue' : `${daysRemaining}d left`}</span>
              </div>
            )}
          </div>

          {/* Actions */}
          {sprint.status === 'planning' && (
            <button
              onClick={() => startMutation.mutate()}
              disabled={startMutation.isPending}
              className="btn-primary flex items-center gap-2"
            >
              <Play size={16} />
              Start Sprint
            </button>
          )}
          {sprint.status === 'active' && (
            <>
              <button
                onClick={() => setShowBurndown(!showBurndown)}
                className={cn('btn flex items-center gap-2', showBurndown && 'bg-gray-200 dark:bg-gray-700')}
              >
                <TrendingDown size={16} />
                Burndown
              </button>
              <button
                onClick={() => completeMutation.mutate()}
                disabled={completeMutation.isPending}
                className="btn-primary flex items-center gap-2"
              >
                <CheckCircle2 size={16} />
                Complete Sprint
              </button>
            </>
          )}
        </div>
      </div>

      {/* Sprint Goal */}
      {sprint.goal && (
        <div className="mb-4 p-3 bg-purple-50 dark:bg-purple-900/20 border border-purple-200 dark:border-purple-800 rounded-lg">
          <span className="text-sm font-medium text-purple-700 dark:text-purple-300">Goal: </span>
          <span className="text-sm text-purple-600 dark:text-purple-400">{sprint.goal}</span>
        </div>
      )}

      {/* Progress Bar */}
      {sprint.status !== 'planning' && (
        <div className="mb-4">
          <div className="h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-green-500 transition-all"
              style={{ width: `${progress}%` }}
            />
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{progress}% complete</p>
        </div>
      )}

      {/* Burndown Chart */}
      {showBurndown && burndownData && (
        <div className="mb-6 card p-4">
          <h3 className="font-semibold text-gray-900 dark:text-white mb-4">Burndown Chart</h3>
          <BurndownChart data={burndownData} />
        </div>
      )}

      {boardMoveError && (
        <p role="alert" className="mb-3 text-sm text-red-600 dark:text-red-400">
          {boardMoveError}
        </p>
      )}

      {/* Kanban Board */}
      <div className="flex-1 overflow-x-auto pb-4">
        <div className="flex gap-4 min-w-max h-full">
          {COLUMNS.map((column) => {
            const columnItems = getItemsByStatus(column.id)
            const columnPoints = columnItems.reduce((sum, item) => sum + (item.story_points || 0), 0)
            const Icon = column.icon

            return (
              <div
                key={column.id}
                className="w-80 flex flex-col bg-gray-100 dark:bg-gray-800/50 rounded-lg"
                onDragOver={handleDragOver}
                onDrop={(e) => handleDrop(e, column.id)}
              >
                {/* Column Header */}
                <div className="flex items-center justify-between p-3 border-b border-gray-200 dark:border-gray-700">
                  <div className="flex items-center gap-2">
                    <Icon size={16} className={column.color} />
                    <span className="font-medium text-gray-900 dark:text-white">
                      {column.label}
                    </span>
                    <span className="text-sm text-gray-500 dark:text-gray-400 bg-gray-200 dark:bg-gray-700 px-2 py-0.5 rounded-full">
                      {columnItems.length}
                    </span>
                  </div>
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {columnPoints} pts
                  </span>
                </div>

                {/* Column Content */}
                <div className="flex-1 overflow-y-auto p-2 space-y-2">
                  {columnItems.map((item) => (
                    <div
                      key={item.id}
                      draggable
                      onDragStart={(e) => handleDragStart(e, item)}
                      onDragEnd={handleDragEnd}
                      onClick={() => navigate(`/tasks/${item.item_key || item.id}`)}
                      className={cn(
                        'bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-3',
                        'border-l-4',
                        getPriorityColor(item.priority),
                        'hover:shadow-md transition-shadow cursor-pointer',
                        draggedItem?.id === item.id && 'opacity-50 cursor-grabbing'
                      )}
                    >
                      {/* Item Key & Points */}
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
                          {item.item_key}
                        </span>
                        {item.story_points && (
                          <span className="text-xs px-2 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 font-medium">
                            {item.story_points}
                          </span>
                        )}
                      </div>

                      {/* Title */}
                      <h4 className="text-sm font-medium text-gray-900 dark:text-white mb-2">
                        {item.title}
                      </h4>

                      {/* Assignee */}
                      {item.assignee_email && (
                        <div className="flex items-center gap-2 mt-2">
                          <div
                            className="w-6 h-6 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center"
                            title={item.assignee_name || item.assignee_email}
                          >
                            <span className="text-xs text-brand-600 dark:text-brand-400 font-medium">
                              {(item.assignee_name || item.assignee_email)[0].toUpperCase()}
                            </span>
                          </div>
                          <span className="text-xs text-gray-500 dark:text-gray-400 truncate">
                            {item.assignee_name || item.assignee_email.split('@')[0]}
                          </span>
                        </div>
                      )}
                    </div>
                  ))}

                  {/* Empty state */}
                  {columnItems.length === 0 && (
                    <div className="text-center py-8 text-gray-400 text-sm">
                      Drop items here
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// Simple Burndown Chart Component
function BurndownChart({ data }) {
  if (!data || !data.burndown || data.burndown.length === 0) {
    return (
      <div className="text-center py-8 text-gray-500">
        No burndown data available
      </div>
    )
  }

  const maxPoints = Math.max(...data.burndown.map(d => d.ideal), ...data.burndown.map(d => d.remaining))
  const chartHeight = 200

  return (
    <div className="relative" style={{ height: chartHeight + 40 }}>
      {/* Y-axis labels */}
      <div className="absolute left-0 top-0 bottom-8 w-8 flex flex-col justify-between text-xs text-gray-500">
        <span>{maxPoints}</span>
        <span>{Math.round(maxPoints / 2)}</span>
        <span>0</span>
      </div>

      {/* Chart area */}
      <div className="ml-10 relative" style={{ height: chartHeight }}>
        <svg className="w-full h-full">
          {/* Grid lines */}
          <line x1="0" y1="0" x2="100%" y2="0" stroke="currentColor" className="text-gray-200 dark:text-gray-700" />
          <line x1="0" y1="50%" x2="100%" y2="50%" stroke="currentColor" className="text-gray-200 dark:text-gray-700" />
          <line x1="0" y1="100%" x2="100%" y2="100%" stroke="currentColor" className="text-gray-200 dark:text-gray-700" />

          {/* Ideal line */}
          <polyline
            fill="none"
            stroke="currentColor"
            className="text-gray-400"
            strokeDasharray="4"
            points={data.burndown.map((d, i) => {
              const x = (i / (data.burndown.length - 1)) * 100
              const y = chartHeight - (d.ideal / maxPoints) * chartHeight
              return `${x}%,${y}`
            }).join(' ')}
          />

          {/* Actual line */}
          <polyline
            fill="none"
            stroke="currentColor"
            className="text-brand-500"
            strokeWidth="2"
            points={data.burndown.map((d, i) => {
              const x = (i / (data.burndown.length - 1)) * 100
              const y = chartHeight - (d.remaining / maxPoints) * chartHeight
              return `${x}%,${y}`
            }).join(' ')}
          />
        </svg>
      </div>

      {/* X-axis labels */}
      <div className="ml-10 flex justify-between text-xs text-gray-500 mt-2">
        {data.burndown.filter((_, i) => i % Math.ceil(data.burndown.length / 5) === 0 || i === data.burndown.length - 1).map((d, i) => (
          <span key={i}>{format(new Date(d.date), 'MMM d')}</span>
        ))}
      </div>

      {/* Legend */}
      <div className="ml-10 flex items-center gap-4 mt-4 text-xs">
        <div className="flex items-center gap-1">
          <div className="w-4 h-0.5 bg-gray-400" style={{ borderStyle: 'dashed' }} />
          <span className="text-gray-500">Ideal</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-4 h-0.5 bg-brand-500" />
          <span className="text-gray-500">Actual</span>
        </div>
      </div>
    </div>
  )
}
