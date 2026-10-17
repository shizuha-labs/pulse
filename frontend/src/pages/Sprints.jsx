import { useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Plus,
  Zap,
  Calendar,
  Target,
  Play,
  CheckCircle2,
  MoreVertical,
  Trash2,
  Edit3,
  Clock,
  AlertCircle,
  ChevronRight,
  TrendingUp,
} from 'lucide-react'
import { sprintsApi, projectsApi } from '../services/api'
import { cn } from '../utils/cn'
import { format, differenceInDays, isPast, isFuture } from 'date-fns'
import LoadingSpinner, { PageLoader } from '../components/LoadingSpinner'

export default function Sprints() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [showCreateModal, setShowCreateModal] = useState(false)
  const [editingSprint, setEditingSprint] = useState(null)
  const [formData, setFormData] = useState({
    name: '',
    goal: '',
    start_date: '',
    end_date: '',
  })
  const [openMenuId, setOpenMenuId] = useState(null)

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

  // Fetch velocity data
  const { data: velocityData } = useQuery({
    queryKey: ['velocity', projectId],
    queryFn: async () => {
      const res = await sprintsApi.velocity({ project: projectId })
      return res.data
    },
  })

  // Create sprint mutation
  const createMutation = useMutation({
    mutationFn: (data) => sprintsApi.create({ ...data, project: parseInt(projectId) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sprints', projectId] })
      setShowCreateModal(false)
      resetForm()
    },
  })

  // Update sprint mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => sprintsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sprints', projectId] })
      setEditingSprint(null)
      resetForm()
    },
  })

  // Delete sprint mutation
  const deleteMutation = useMutation({
    mutationFn: (id) => sprintsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sprints', projectId] })
    },
  })

  // Start sprint mutation
  const startMutation = useMutation({
    mutationFn: (id) => sprintsApi.start(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sprints', projectId] })
    },
  })

  // Complete sprint mutation
  const completeMutation = useMutation({
    mutationFn: (id) => sprintsApi.complete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sprints', projectId] })
      queryClient.invalidateQueries({ queryKey: ['velocity', projectId] })
    },
  })

  const resetForm = () => {
    setFormData({ name: '', goal: '', start_date: '', end_date: '' })
  }

  const handleOpenCreate = () => {
    resetForm()
    setEditingSprint(null)
    setShowCreateModal(true)
  }

  const handleOpenEdit = (sprint) => {
    setFormData({
      name: sprint.name,
      goal: sprint.goal || '',
      start_date: sprint.start_date || '',
      end_date: sprint.end_date || '',
    })
    setEditingSprint(sprint)
    setShowCreateModal(true)
    setOpenMenuId(null)
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    if (editingSprint) {
      updateMutation.mutate({ id: editingSprint.id, ...formData })
    } else {
      createMutation.mutate(formData)
    }
  }

  const handleDelete = (sprint) => {
    if (confirm(`Delete sprint "${sprint.name}"? Items will be moved to backlog.`)) {
      deleteMutation.mutate(sprint.id)
    }
    setOpenMenuId(null)
  }

  const getStatusColor = (status) => {
    switch (status) {
      case 'active':
        return 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
      case 'completed':
        return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
      default:
        return 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400'
    }
  }

  const getProgressColor = (percent) => {
    if (percent >= 80) return 'bg-green-500'
    if (percent >= 50) return 'bg-blue-500'
    if (percent >= 25) return 'bg-yellow-500'
    return 'bg-gray-400'
  }

  if (projectLoading || sprintsLoading) {
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

  const activeSprint = sprints.find(s => s.status === 'active')
  const planningSprints = sprints.filter(s => s.status === 'planning')
  const completedSprints = sprints.filter(s => s.status === 'completed')

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
              Sprints
            </h1>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {project.name}
            </p>
          </div>
        </div>
        <div className="flex-1" />
        <button onClick={handleOpenCreate} className="btn-primary flex items-center gap-2">
          <Plus size={18} />
          Create Sprint
        </button>
      </div>

      {/* Velocity Summary */}
      {velocityData && velocityData.sprints_completed > 0 && (
        <div className="card p-4 mb-6">
          <div className="flex items-center gap-3 mb-3">
            <TrendingUp size={20} className="text-brand-500" />
            <h2 className="font-semibold text-gray-900 dark:text-white">Velocity</h2>
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">
                {velocityData.average_velocity?.toFixed(1) || 0}
              </p>
              <p className="text-sm text-gray-500 dark:text-gray-400">Avg. Points/Sprint</p>
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">
                {velocityData.total_points_completed || 0}
              </p>
              <p className="text-sm text-gray-500 dark:text-gray-400">Total Points</p>
            </div>
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">
                {velocityData.sprints_completed || 0}
              </p>
              <p className="text-sm text-gray-500 dark:text-gray-400">Sprints Completed</p>
            </div>
          </div>
        </div>
      )}

      {/* Active Sprint */}
      {activeSprint && (
        <div className="mb-6">
          <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
            Active Sprint
          </h2>
          <SprintCard
            sprint={activeSprint}
            onEdit={() => handleOpenEdit(activeSprint)}
            onDelete={() => handleDelete(activeSprint)}
            onComplete={() => completeMutation.mutate(activeSprint.id)}
            isCompleting={completeMutation.isPending}
            openMenuId={openMenuId}
            setOpenMenuId={setOpenMenuId}
            getStatusColor={getStatusColor}
            getProgressColor={getProgressColor}
          />
        </div>
      )}

      {/* Planning Sprints */}
      {planningSprints.length > 0 && (
        <div className="mb-6">
          <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
            Planning ({planningSprints.length})
          </h2>
          <div className="space-y-3">
            {planningSprints.map(sprint => (
              <SprintCard
                key={sprint.id}
                sprint={sprint}
                onEdit={() => handleOpenEdit(sprint)}
                onDelete={() => handleDelete(sprint)}
                onStart={() => startMutation.mutate(sprint.id)}
                isStarting={startMutation.isPending}
                canStart={!activeSprint}
                openMenuId={openMenuId}
                setOpenMenuId={setOpenMenuId}
                getStatusColor={getStatusColor}
                getProgressColor={getProgressColor}
              />
            ))}
          </div>
        </div>
      )}

      {/* Completed Sprints */}
      {completedSprints.length > 0 && (
        <div className="mb-6">
          <h2 className="text-sm font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
            Completed ({completedSprints.length})
          </h2>
          <div className="space-y-3">
            {completedSprints.map(sprint => (
              <SprintCard
                key={sprint.id}
                sprint={sprint}
                onEdit={() => handleOpenEdit(sprint)}
                onDelete={() => handleDelete(sprint)}
                openMenuId={openMenuId}
                setOpenMenuId={setOpenMenuId}
                getStatusColor={getStatusColor}
                getProgressColor={getProgressColor}
                collapsed
              />
            ))}
          </div>
        </div>
      )}

      {/* Empty State */}
      {sprints.length === 0 && (
        <div className="text-center py-12">
          <Zap className="mx-auto h-12 w-12 text-gray-400" />
          <h3 className="mt-4 text-lg font-medium text-gray-900 dark:text-white">
            No sprints yet
          </h3>
          <p className="mt-2 text-gray-500 dark:text-gray-400">
            Create your first sprint to start planning.
          </p>
          <button onClick={handleOpenCreate} className="btn-primary mt-4">
            Create Sprint
          </button>
        </div>
      )}

      {/* Create/Edit Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">
              {editingSprint ? 'Edit Sprint' : 'Create Sprint'}
            </h2>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Sprint Name *
                </label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="input w-full"
                  placeholder="Sprint 1"
                  required
                  autoFocus
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Goal
                </label>
                <textarea
                  value={formData.goal}
                  onChange={(e) => setFormData({ ...formData, goal: e.target.value })}
                  className="input w-full"
                  rows={2}
                  placeholder="What do you want to achieve this sprint?"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Start Date
                  </label>
                  <input
                    type="date"
                    value={formData.start_date}
                    onChange={(e) => setFormData({ ...formData, start_date: e.target.value })}
                    className="input w-full"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    End Date
                  </label>
                  <input
                    type="date"
                    value={formData.end_date}
                    onChange={(e) => setFormData({ ...formData, end_date: e.target.value })}
                    className="input w-full"
                  />
                </div>
              </div>

              <div className="flex justify-end gap-3 pt-4">
                <button
                  type="button"
                  onClick={() => {
                    setShowCreateModal(false)
                    setEditingSprint(null)
                    resetForm()
                  }}
                  className="btn"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={createMutation.isPending || updateMutation.isPending || !formData.name.trim()}
                  className="btn-primary"
                >
                  {createMutation.isPending || updateMutation.isPending
                    ? 'Saving...'
                    : editingSprint ? 'Update' : 'Create'
                  }
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

// Sprint Card Component
function SprintCard({
  sprint,
  onEdit,
  onDelete,
  onStart,
  onComplete,
  isStarting,
  isCompleting,
  canStart = true,
  openMenuId,
  setOpenMenuId,
  getStatusColor,
  getProgressColor,
  collapsed = false,
}) {
  const navigate = useNavigate()
  const [isExpanded, setIsExpanded] = useState(!collapsed)

  const progress = sprint.committed_points > 0
    ? Math.round((sprint.completed_points / sprint.committed_points) * 100)
    : 0

  const daysRemaining = sprint.end_date
    ? differenceInDays(new Date(sprint.end_date), new Date())
    : null

  return (
    <div className="card">
      {/* Header */}
      <div
        className="p-4 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50"
        onClick={() => navigate(`/projects/${sprint.project}/sprints/${sprint.id}`)}
      >
        <div className="flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-purple-100 dark:bg-purple-900/30 flex items-center justify-center">
            <Zap size={20} className="text-purple-600 dark:text-purple-400" />
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="font-semibold text-gray-900 dark:text-white truncate">
                {sprint.name}
              </h3>
              <span className={cn('text-xs px-2 py-0.5 rounded', getStatusColor(sprint.status))}>
                {sprint.status}
              </span>
            </div>
            {sprint.goal && (
              <p className="text-sm text-gray-500 dark:text-gray-400 truncate mt-0.5">
                {sprint.goal}
              </p>
            )}
          </div>

          <div className="flex items-center gap-4 text-sm text-gray-500 dark:text-gray-400">
            {sprint.start_date && sprint.end_date && (
              <span className="flex items-center gap-1">
                <Calendar size={14} />
                {format(new Date(sprint.start_date), 'MMM d')} - {format(new Date(sprint.end_date), 'MMM d')}
              </span>
            )}
            {sprint.status === 'active' && daysRemaining !== null && (
              <span className={cn(
                'flex items-center gap-1',
                daysRemaining < 0 && 'text-red-500',
                daysRemaining <= 2 && daysRemaining >= 0 && 'text-amber-500'
              )}>
                <Clock size={14} />
                {daysRemaining < 0 ? 'Overdue' : `${daysRemaining}d left`}
              </span>
            )}
            <span className="flex items-center gap-1">
              <Target size={14} />
              {sprint.completed_points}/{sprint.committed_points || sprint.total_points || 0} pts
            </span>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
            {sprint.status === 'planning' && (
              <button
                onClick={onStart}
                disabled={isStarting || !canStart}
                className={cn(
                  'btn-sm flex items-center gap-1',
                  canStart ? 'btn-primary' : 'btn opacity-50 cursor-not-allowed'
                )}
                title={!canStart ? 'Complete active sprint first' : 'Start sprint'}
              >
                <Play size={14} />
                Start
              </button>
            )}
            {sprint.status === 'active' && (
              <button
                onClick={onComplete}
                disabled={isCompleting}
                className="btn-sm btn-primary flex items-center gap-1"
              >
                <CheckCircle2 size={14} />
                Complete
              </button>
            )}

            <div className="relative">
              <button
                onClick={() => setOpenMenuId(openMenuId === sprint.id ? null : sprint.id)}
                className="btn-icon"
              >
                <MoreVertical size={16} />
              </button>
              {openMenuId === sprint.id && (
                <div className="dropdown-menu absolute right-0 top-full mt-1 min-w-[120px] z-50">
                  <button className="dropdown-item" onClick={onEdit}>
                    <Edit3 size={14} /> Edit
                  </button>
                  <button className="dropdown-item-danger" onClick={onDelete}>
                    <Trash2 size={14} /> Delete
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Progress Bar */}
        {(sprint.status === 'active' || sprint.status === 'completed') && sprint.committed_points > 0 && (
          <div className="mt-3">
            <div className="h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
              <div
                className={cn('h-full transition-all', getProgressColor(progress))}
                style={{ width: `${Math.min(progress, 100)}%` }}
              />
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {progress}% complete
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
