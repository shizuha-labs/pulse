import { useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Plus,
  Zap,
  Target,
  GripVertical,
  ChevronDown,
  ChevronRight,
  CheckCircle,
  Circle,
  Clock,
  User,
  MoreVertical,
  Trash2,
  Edit3,
  MoveRight,
  AlertCircle,
} from 'lucide-react'
import { sprintsApi, projectsApi, itemsApi } from '../services/api'
import { cn } from '../utils/cn'
import { format, formatDistanceToNow } from 'date-fns'
import LoadingSpinner, { PageLoader } from '../components/LoadingSpinner'

export default function Backlog() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [expandedSprints, setExpandedSprints] = useState({})
  const [showMoveModal, setShowMoveModal] = useState(false)
  const [selectedItems, setSelectedItems] = useState([])
  const [targetSprint, setTargetSprint] = useState(null)
  const [showDone, setShowDone] = useState(false)

  // Fetch project
  const { data: project, isLoading: projectLoading } = useQuery({
    queryKey: ['project', projectId],
    queryFn: async () => {
      const res = await projectsApi.get(projectId)
      return res.data
    },
  })

  // Fetch sprints
  const { data: sprints = [], isLoading: sprintsLoading } = useQuery({
    queryKey: ['sprints', projectId],
    queryFn: async () => {
      const res = await sprintsApi.list({ project: projectId })
      return res.data.results || res.data || []
    },
  })

  // Fetch backlog items (no sprint assigned).
  // Defaults to active-only (is_active=true) to exclude terminal items; user can toggle showDone.
  const { data: backlogItems = [], isLoading: backlogLoading } = useQuery({
    queryKey: ['backlog-items', projectId, showDone],
    queryFn: async () => {
      const params = { mode: 'task', sprint__isnull: true }
      if (!showDone) params.is_active = 'true'
      const res = await projectsApi.items(projectId, params)
      return res.data.items || res.data || []
    },
  })

  // Fetch sprint items
  const { data: sprintItemsMap = {}, isLoading: sprintItemsLoading } = useQuery({
    queryKey: ['sprint-items', projectId, sprints.map(s => s.id).join(',')],
    queryFn: async () => {
      const itemsMap = {}
      for (const sprint of sprints) {
        const res = await sprintsApi.items(sprint.id)
        itemsMap[sprint.id] = res.data.items || res.data || []
      }
      return itemsMap
    },
    enabled: sprints.length > 0,
  })

  // Move items to sprint mutation
  const moveMutation = useMutation({
    mutationFn: async ({ itemIds, sprintId }) => {
      const promises = itemIds.map(id => itemsApi.update(id, { sprint: sprintId }))
      return Promise.all(promises)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['backlog-items', projectId] })
      queryClient.invalidateQueries({ queryKey: ['sprint-items', projectId] })
      queryClient.invalidateQueries({ queryKey: ['sprints', projectId] })
      setSelectedItems([])
      setShowMoveModal(false)
    },
  })

  const toggleSprintExpand = (sprintId) => {
    setExpandedSprints(prev => ({
      ...prev,
      [sprintId]: !prev[sprintId],
    }))
  }

  const toggleItemSelect = (itemId) => {
    setSelectedItems(prev =>
      prev.includes(itemId)
        ? prev.filter(id => id !== itemId)
        : [...prev, itemId]
    )
  }

  const handleMoveToSprint = (sprintId) => {
    if (selectedItems.length === 0) return
    moveMutation.mutate({ itemIds: selectedItems, sprintId })
  }

  const getStatusIcon = (status) => {
    switch (status) {
      case 'completed':
        return <CheckCircle size={16} className="text-green-500" />
      case 'in_progress':
        return <Circle size={16} className="text-blue-500 fill-blue-500" />
      default:
        return <Circle size={16} className="text-gray-400" />
    }
  }

  const getPriorityColor = (priority) => {
    switch (priority) {
      case 'urgent': return 'border-l-red-500'
      case 'high': return 'border-l-orange-500'
      case 'normal': return 'border-l-blue-500'
      case 'low': return 'border-l-gray-400'
      default: return 'border-l-gray-300'
    }
  }

  const isLoading = projectLoading || sprintsLoading || backlogLoading

  if (isLoading) {
    return <PageLoader />
  }

  if (!project) {
    return (
      <div className="text-center py-12">
        <AlertCircle className="mx-auto h-12 w-12 text-red-500" />
        <h3 className="mt-2 text-lg font-medium text-gray-900 dark:text-white">
          Project not found
        </h3>
        <Link to="/projects" className="btn-primary mt-4 inline-flex items-center gap-2">
          <ArrowLeft size={16} />
          Back to Projects
        </Link>
      </div>
    )
  }

  const activeSprints = sprints.filter(s => s.status === 'active' || s.status === 'planning')
  const totalBacklogPoints = backlogItems.reduce((sum, item) => sum + (item.story_points || 0), 0)

  return (
    <div className="max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-center gap-4 mb-6">
        <Link to={`/projects/${projectId}`} className="btn-icon">
          <ArrowLeft size={20} />
        </Link>
        <div className="flex items-center gap-3">
          <div
            className="w-10 h-10 rounded-lg flex items-center justify-center text-white font-bold text-sm"
            style={{ backgroundColor: project.color || '#0052CC' }}
          >
            {project.key}
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-900 dark:text-white">
              Backlog
            </h1>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {project.name}
            </p>
          </div>
        </div>
        <div className="flex-1" />
        {selectedItems.length > 0 && (
          <div className="flex items-center gap-2 bg-brand-50 dark:bg-brand-900/20 px-3 py-2 rounded-lg">
            <span className="text-sm text-brand-700 dark:text-brand-300">
              {selectedItems.length} selected
            </span>
            <button
              onClick={() => setShowMoveModal(true)}
              className="btn-sm btn-primary flex items-center gap-1"
            >
              <MoveRight size={14} />
              Move to Sprint
            </button>
            <button
              onClick={() => setSelectedItems([])}
              className="btn-sm"
            >
              Clear
            </button>
          </div>
        )}
        <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={showDone}
            onChange={e => setShowDone(e.target.checked)}
            className="rounded border-gray-300 text-brand-600 focus:ring-brand-500"
          />
          Show done
        </label>
        <Link to={`/projects/${projectId}/sprints`} className="btn flex items-center gap-2">
          <Zap size={16} />
          Sprints
        </Link>
      </div>

      {/* Active Sprints */}
      {activeSprints.map(sprint => {
        const items = sprintItemsMap[sprint.id] || []
        const isExpanded = expandedSprints[sprint.id] !== false
        const totalPoints = items.reduce((sum, item) => sum + (item.story_points || 0), 0)
        const completedPoints = items
          .filter(item => item.status === 'completed')
          .reduce((sum, item) => sum + (item.story_points || 0), 0)

        return (
          <div key={sprint.id} className="mb-4">
            {/* Sprint Header */}
            <div
              className={cn(
                'flex items-center gap-3 p-3 rounded-lg cursor-pointer transition-colors',
                sprint.status === 'active'
                  ? 'bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800'
                  : 'bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800'
              )}
              onClick={() => toggleSprintExpand(sprint.id)}
            >
              {isExpanded ? <ChevronDown size={20} /> : <ChevronRight size={20} />}
              <Zap size={18} className={sprint.status === 'active' ? 'text-green-600' : 'text-blue-600'} />
              <span className="font-medium text-gray-900 dark:text-white">
                {sprint.name}
              </span>
              <span className={cn(
                'text-xs px-2 py-0.5 rounded',
                sprint.status === 'active'
                  ? 'bg-green-100 text-green-700 dark:bg-green-800 dark:text-green-300'
                  : 'bg-blue-100 text-blue-700 dark:bg-blue-800 dark:text-blue-300'
              )}>
                {sprint.status}
              </span>
              <span className="text-sm text-gray-500 dark:text-gray-400">
                {items.length} items
              </span>
              <span className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-1">
                <Target size={14} />
                {completedPoints}/{totalPoints} pts
              </span>
              {sprint.start_date && sprint.end_date && (
                <span className="text-sm text-gray-500 dark:text-gray-400 ml-auto">
                  {format(new Date(sprint.start_date), 'MMM d')} - {format(new Date(sprint.end_date), 'MMM d')}
                </span>
              )}
            </div>

            {/* Sprint Items */}
            {isExpanded && (
              <div className="mt-2 space-y-1 pl-8">
                {items.length === 0 ? (
                  <div className="text-sm text-gray-500 dark:text-gray-400 py-4 text-center">
                    No items in this sprint
                  </div>
                ) : (
                  items.map(item => (
                    <BacklogItem
                      key={item.id}
                      item={item}
                      isSelected={selectedItems.includes(item.id)}
                      onSelect={() => toggleItemSelect(item.id)}
                      onClick={() => navigate(`/tasks/${item.item_key || item.id}`)}
                      getStatusIcon={getStatusIcon}
                      getPriorityColor={getPriorityColor}
                    />
                  ))
                )}
              </div>
            )}
          </div>
        )
      })}

      {/* Backlog Section */}
      <div className="mt-6">
        <div className="flex items-center gap-3 p-3 bg-gray-100 dark:bg-gray-800 rounded-lg mb-2">
          <span className="font-medium text-gray-900 dark:text-white">
            Backlog
          </span>
          <span className="text-sm text-gray-500 dark:text-gray-400">
            {backlogItems.length} items
          </span>
          <span className="text-sm text-gray-500 dark:text-gray-400 flex items-center gap-1">
            <Target size={14} />
            {totalBacklogPoints} pts
          </span>
        </div>

        <div className="space-y-1">
          {backlogItems.length === 0 ? (
            <div className="text-center py-8 text-gray-500 dark:text-gray-400">
              <p>No items in backlog</p>
              <p className="text-sm mt-1">All items are assigned to sprints</p>
            </div>
          ) : (
            backlogItems.map(item => (
              <BacklogItem
                key={item.id}
                item={item}
                isSelected={selectedItems.includes(item.id)}
                onSelect={() => toggleItemSelect(item.id)}
                onClick={() => navigate(`/tasks/${item.item_key || item.id}`)}
                getStatusIcon={getStatusIcon}
                getPriorityColor={getPriorityColor}
              />
            ))
          )}
        </div>
      </div>

      {/* Move to Sprint Modal */}
      {showMoveModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">
              Move {selectedItems.length} item{selectedItems.length > 1 ? 's' : ''} to Sprint
            </h2>

            <div className="space-y-2 max-h-60 overflow-y-auto">
              {/* Backlog option */}
              <button
                onClick={() => handleMoveToSprint(null)}
                disabled={moveMutation.isPending}
                className="w-full flex items-center gap-3 p-3 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-left"
              >
                <div className="w-8 h-8 rounded bg-gray-200 dark:bg-gray-700 flex items-center justify-center">
                  <Target size={16} className="text-gray-500" />
                </div>
                <span className="text-gray-900 dark:text-white">Backlog</span>
              </button>

              {activeSprints.map(sprint => (
                <button
                  key={sprint.id}
                  onClick={() => handleMoveToSprint(sprint.id)}
                  disabled={moveMutation.isPending}
                  className="w-full flex items-center gap-3 p-3 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-left"
                >
                  <div className={cn(
                    'w-8 h-8 rounded flex items-center justify-center',
                    sprint.status === 'active'
                      ? 'bg-green-100 dark:bg-green-900/30'
                      : 'bg-blue-100 dark:bg-blue-900/30'
                  )}>
                    <Zap size={16} className={
                      sprint.status === 'active'
                        ? 'text-green-600 dark:text-green-400'
                        : 'text-blue-600 dark:text-blue-400'
                    } />
                  </div>
                  <div className="flex-1 min-w-0">
                    <span className="text-gray-900 dark:text-white block truncate">
                      {sprint.name}
                    </span>
                    <span className={cn(
                      'text-xs px-1.5 py-0.5 rounded',
                      sprint.status === 'active'
                        ? 'bg-green-100 text-green-700'
                        : 'bg-blue-100 text-blue-700'
                    )}>
                      {sprint.status}
                    </span>
                  </div>
                </button>
              ))}
            </div>

            <div className="flex justify-end gap-3 mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={() => setShowMoveModal(false)}
                className="btn"
                disabled={moveMutation.isPending}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// Backlog Item Component
function BacklogItem({ item, isSelected, onSelect, onClick, getStatusIcon, getPriorityColor }) {
  return (
    <div
      className={cn(
        'flex items-center gap-3 p-3 rounded-lg border-l-4 transition-colors',
        getPriorityColor(item.priority),
        isSelected
          ? 'bg-brand-50 dark:bg-brand-900/20'
          : 'bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-750'
      )}
    >
      {/* Checkbox */}
      <input
        type="checkbox"
        checked={isSelected}
        onChange={onSelect}
        onClick={(e) => e.stopPropagation()}
        className="w-4 h-4 rounded border-gray-300 text-brand-600 focus:ring-brand-500"
      />

      {/* Drag handle (visual only for now) */}
      <GripVertical size={16} className="text-gray-400 cursor-grab" />

      {/* Status */}
      {getStatusIcon(item.status)}

      {/* Key */}
      <span className="text-xs font-medium text-gray-500 dark:text-gray-400 min-w-[60px]">
        {item.item_key}
      </span>

      {/* Title */}
      <span
        className={cn(
          'flex-1 truncate cursor-pointer hover:text-brand-600 dark:hover:text-brand-400',
          item.status === 'completed' && 'line-through text-gray-500'
        )}
        onClick={onClick}
      >
        {item.title}
      </span>

      {/* Story Points */}
      {item.story_points && (
        <span className="text-xs px-2 py-1 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 font-medium">
          {item.story_points}
        </span>
      )}

      {/* Assignee */}
      {item.assignee_email && (
        <div
          className="w-6 h-6 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center"
          title={item.assignee_name || item.assignee_email}
        >
          <span className="text-xs text-brand-600 dark:text-brand-400 font-medium">
            {(item.assignee_name || item.assignee_email)[0].toUpperCase()}
          </span>
        </div>
      )}

      {/* Due date */}
      {item.due_date && (
        <span className={cn(
          'text-xs flex items-center gap-1',
          item.is_overdue ? 'text-red-500' : 'text-gray-500 dark:text-gray-400'
        )}>
          <Clock size={12} />
          {format(new Date(item.due_date), 'MMM d')}
        </span>
      )}
    </div>
  )
}
