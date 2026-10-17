import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import {
  ArrowLeft,
  Plus,
  MoreVertical,
  Users,
  Settings,
  Clock,
  AlertCircle,
  CheckCircle2,
  Circle,
  Loader2,
  X,
  UserPlus,
  Shield,
  Eye,
  Edit3,
  Trash2,
  Crown,
  User,
  Zap,
  List,
} from 'lucide-react'
import { projectsApi, itemsApi, organizationsApi } from '../services/api'
import { useOrganization } from '../contexts/OrganizationContext'
import LoadingSpinner from '../components/LoadingSpinner'
import { cn } from '../utils/cn'
import { formatApiError } from '../utils/createTaskRequest'
import { moveTaskToColumn } from '../utils/moveTaskOnBoard'
import { formatDistanceToNow } from 'date-fns'

// Kanban columns configuration
const COLUMNS = [
  { id: 'pending', label: 'To Do', icon: Circle, color: 'text-gray-500' },
  { id: 'in_progress', label: 'In Progress', icon: Loader2, color: 'text-blue-500' },
  { id: 'completed', label: 'Done', icon: CheckCircle2, color: 'text-green-500' },
]

export default function ProjectBoard() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const { currentOrganization, refreshProjects } = useOrganization()

  const [project, setProject] = useState(null)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [draggedItem, setDraggedItem] = useState(null)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [newTask, setNewTask] = useState({ title: '', description: '', priority: 'normal' })
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const [boardMoveError, setBoardMoveError] = useState('')

  // Members panel state
  const [showMembersPanel, setShowMembersPanel] = useState(false)
  const [members, setMembers] = useState([])
  const [loadingMembers, setLoadingMembers] = useState(false)

  // Invite modal state
  const [showInviteModal, setShowInviteModal] = useState(false)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState('member')
  const [inviting, setInviting] = useState(false)
  const [inviteError, setInviteError] = useState(null)
  const [inviteSuccess, setInviteSuccess] = useState(false)
  const [addToOrgFirst, setAddToOrgFirst] = useState(false)
  const [addingToOrg, setAddingToOrg] = useState(false)

  // Task card dropdown menu state
  const [openMenuId, setOpenMenuId] = useState(null)
  const menuRef = useRef(null)

  // Assignment modal state
  const [showAssignModal, setShowAssignModal] = useState(false)
  const [assigningTask, setAssigningTask] = useState(null)
  const [assigning, setAssigning] = useState(false)

  useEffect(() => {
    loadProject()
  }, [projectId])

  // Click outside handler for dropdown menu
  useEffect(() => {
    const handleClickOutside = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setOpenMenuId(null)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const loadProject = async () => {
    try {
      setLoading(true)
      const [projectRes, itemsRes] = await Promise.all([
        projectsApi.get(projectId),
        projectsApi.items(projectId, { mode: 'task' }),
      ])
      setProject(projectRes.data)
      setItems(itemsRes.data.items || [])
    } catch (err) {
      console.error('Failed to load project:', err)
      setError('Failed to load project')
    } finally {
      setLoading(false)
    }
  }

  const loadMembers = async () => {
    try {
      setLoadingMembers(true)
      const response = await projectsApi.members(projectId)
      setMembers(response.data.members || response.data || [])
    } catch (err) {
      console.error('Failed to load members:', err)
    } finally {
      setLoadingMembers(false)
    }
  }

  const handleOpenMembersPanel = () => {
    setShowMembersPanel(true)
    loadMembers()
  }

  const handleOpenInviteModal = () => {
    setInviteEmail('')
    setInviteRole('member')
    setInviteError(null)
    setInviteSuccess(false)
    setAddToOrgFirst(false)
    setShowInviteModal(true)
  }

  const handleAddToOrgAndProject = async () => {
    setInviteError(null)
    setAddingToOrg(true)

    try {
      // First add to organization
      await organizationsApi.addMember(currentOrganization.slug, {
        user_email: inviteEmail,
        role: 'member',
      })

      // Then add to project
      await projectsApi.addMember(projectId, {
        user_email: inviteEmail,
        role: inviteRole,
        can_browse: true,
        can_create: inviteRole !== 'viewer',
        can_edit: inviteRole !== 'viewer',
        can_delete: inviteRole === 'admin',
        can_manage_project: inviteRole === 'admin',
        can_manage_members: inviteRole === 'admin',
      })

      setInviteSuccess(true)
      setAddToOrgFirst(false)
      setInviteEmail('')
      loadMembers()
      loadProject() // Refresh member count
      if (refreshProjects) refreshProjects()
    } catch (err) {
      setInviteError(
        err.response?.data?.detail ||
        err.response?.data?.user_email?.[0] ||
        err.response?.data?.error ||
        'Failed to add member'
      )
    } finally {
      setAddingToOrg(false)
    }
  }

  const handleInviteMember = async (e) => {
    e.preventDefault()
    setInviteError(null)
    setInviting(true)

    try {
      await projectsApi.addMember(projectId, {
        user_email: inviteEmail,
        role: inviteRole,
        can_browse: true,
        can_create: inviteRole !== 'viewer',
        can_edit: inviteRole !== 'viewer',
        can_delete: inviteRole === 'admin',
        can_manage_project: inviteRole === 'admin',
        can_manage_members: inviteRole === 'admin',
      })
      setInviteSuccess(true)
      setInviteEmail('')
      loadMembers()
      loadProject() // Refresh member count
      if (refreshProjects) refreshProjects()
    } catch (err) {
      const errorMsg = err.response?.data?.error || err.response?.data?.detail || ''

      if (errorMsg.toLowerCase().includes('organization member')) {
        setAddToOrgFirst(true)
        setInviteError(null)
      } else {
        setInviteError(
          err.response?.data?.detail ||
          err.response?.data?.user_email?.[0] ||
          err.response?.data?.error ||
          'Failed to invite member'
        )
      }
    } finally {
      setInviting(false)
    }
  }

  const getRoleIcon = (role) => {
    switch (role) {
      case 'admin': return Crown
      case 'member': return Edit3
      case 'viewer': return Eye
      default: return Users
    }
  }

  const getRoleBadgeColor = (role) => {
    switch (role) {
      case 'admin': return 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400'
      case 'member': return 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400'
      case 'viewer': return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
      default: return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
    }
  }

  // Task card action handlers
  const handleOpenTask = (item) => {
    // Navigate to task detail page
    const taskKey = item.item_key || item.id
    navigate(`/tasks/${taskKey}`)
    setOpenMenuId(null)
  }

  const handleDeleteTask = async (item) => {
    if (!confirm(`Delete task "${item.title}"?`)) return
    setOpenMenuId(null)
    try {
      await itemsApi.delete(item.id)
      setItems(items.filter(i => i.id !== item.id))
    } catch (err) {
      console.error('Failed to delete task:', err)
    }
  }

  const handleOpenAssignModal = (item) => {
    setAssigningTask(item)
    setShowAssignModal(true)
    setOpenMenuId(null)
    loadMembers() // Ensure members are loaded
  }

  const handleAssignTask = async (assigneeId, assigneeEmail) => {
    if (!assigningTask) return
    setAssigning(true)
    try {
      await itemsApi.assign(assigningTask.id, {
        assignee_id: assigneeId,
        assignee_email: assigneeEmail,
      })
      await loadProject() // Refresh items to show updated assignee
      setShowAssignModal(false)
      setAssigningTask(null)
    } catch (err) {
      console.error('Failed to assign task:', err)
    } finally {
      setAssigning(false)
    }
  }

  const handleUnassignTask = async () => {
    if (!assigningTask) return
    setAssigning(true)
    try {
      await itemsApi.assign(assigningTask.id, {
        assignee_id: null,
        assignee_email: null,
      })
      await loadProject()
      setShowAssignModal(false)
      setAssigningTask(null)
    } catch (err) {
      console.error('Failed to unassign task:', err)
    } finally {
      setAssigning(false)
    }
  }

  const handleDragStart = (e, item) => {
    setDraggedItem(item)
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', item.id)
    // Add a visual effect
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
      if (result.moved) await loadProject()
    } catch (err) {
      setBoardMoveError(formatApiError(err, 'This task could not move.'))
    }
  }

  const handleCreateTask = async (e) => {
    e.preventDefault()
    setCreating(true)
    setCreateError('')

    try {
      const response = await itemsApi.create({
        ...newTask,
        mode: 'task',
        project: parseInt(projectId),
      })
      setItems([response.data, ...items])
      setShowCreateModal(false)
      setNewTask({ title: '', description: '', priority: 'normal' })
    } catch (err) {
      setCreateError(formatApiError(err, 'The task could not be created.'))
    } finally {
      setCreating(false)
    }
  }

  const getItemsByStatus = (status) => {
    return items.filter(item => item.status === status)
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

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  if (error || !project) {
    return (
      <div className="text-center py-12">
        <AlertCircle className="mx-auto h-12 w-12 text-red-500" />
        <h3 className="mt-2 text-lg font-medium text-gray-900 dark:text-white">
          {error || 'Project not found'}
        </h3>
        <Link to="/projects" className="mt-4 btn-primary inline-flex items-center gap-2">
          <ArrowLeft size={16} />
          Back to Projects
        </Link>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-6">
        <div className="flex items-center gap-4">
          <Link
            to="/projects"
            className="btn-icon"
          >
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
                {project.name}
              </h1>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {project.organization_name}
              </p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Link
            to={`/projects/${projectId}/backlog`}
            className="btn flex items-center gap-2"
          >
            <List size={16} />
            <span className="hidden sm:inline">Backlog</span>
          </Link>
          <Link
            to={`/projects/${projectId}/sprints`}
            className="btn flex items-center gap-2"
          >
            <Zap size={16} />
            <span className="hidden sm:inline">Sprints</span>
          </Link>
          <button
            onClick={handleOpenMembersPanel}
            className="btn flex items-center gap-2"
          >
            <Users size={16} />
            <span>{project.member_count || 1}</span>
          </button>
          <button
            onClick={() => setShowCreateModal(true)}
            className="btn-primary flex items-center gap-2"
          >
            <Plus size={18} />
            <span>Add Task</span>
          </button>
        </div>
      </div>

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
                  <button
                    onClick={() => {
                      setNewTask({ ...newTask, status: column.id })
                      setShowCreateModal(true)
                    }}
                    className="btn-icon text-gray-400 hover:text-gray-600"
                  >
                    <Plus size={16} />
                  </button>
                </div>

                {/* Column Content */}
                <div className="flex-1 overflow-y-auto p-2 space-y-2">
                  {columnItems.map((item) => (
                    <div
                      key={item.id}
                      draggable
                      onDragStart={(e) => handleDragStart(e, item)}
                      onDragEnd={handleDragEnd}
                      onClick={() => handleOpenTask(item)}
                      className={cn(
                        'bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-3',
                        'border-l-4',
                        getPriorityColor(item.priority),
                        'hover:shadow-md transition-shadow cursor-pointer',
                        draggedItem?.id === item.id && 'opacity-50 cursor-grabbing'
                      )}
                    >
                      {/* Item Key & Menu */}
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
                          {item.item_key}
                        </span>
                        <div
                          className="relative"
                          ref={openMenuId === item.id ? menuRef : null}
                        >
                          <button
                            className="btn-icon text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                            onClick={(e) => {
                              e.stopPropagation()
                              setOpenMenuId(openMenuId === item.id ? null : item.id)
                            }}
                          >
                            <MoreVertical size={14} />
                          </button>
                          {openMenuId === item.id && (
                            <div className="dropdown-menu absolute right-0 top-full mt-1 min-w-[140px] z-50">
                              <button
                                className="dropdown-item"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  handleOpenTask(item)
                                }}
                              >
                                <Edit3 size={14} /> Edit
                              </button>
                              <button
                                className="dropdown-item"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  handleOpenAssignModal(item)
                                }}
                              >
                                <UserPlus size={14} /> Assign
                              </button>
                              <button
                                className="dropdown-item-danger"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  handleDeleteTask(item)
                                }}
                              >
                                <Trash2 size={14} /> Delete
                              </button>
                            </div>
                          )}
                        </div>
                      </div>

                      {/* Title */}
                      <h4 className="text-sm font-medium text-gray-900 dark:text-white mb-2">
                        {item.title}
                      </h4>

                      {/* Meta */}
                      <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
                        <div className="flex items-center gap-2">
                          {item.due_date && (
                            <span className={cn(
                              'flex items-center gap-1',
                              item.is_overdue && 'text-red-500'
                            )}>
                              <Clock size={12} />
                              {formatDistanceToNow(new Date(item.due_date), { addSuffix: true })}
                            </span>
                          )}
                        </div>
                        <span className={cn(
                          'px-1.5 py-0.5 rounded text-xs',
                          item.priority === 'urgent' && 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
                          item.priority === 'high' && 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
                          item.priority === 'normal' && 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
                          item.priority === 'low' && 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
                        )}>
                          {item.priority}
                        </span>
                      </div>

                      {/* Assignee Avatar */}
                      {item.assignee_email && (
                        <div className="mt-2 flex items-center gap-2">
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
                      Drop tasks here
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Create Task Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">
              Create Task in {project.key}
            </h2>

            <form onSubmit={handleCreateTask} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Title
                </label>
                <input
                  type="text"
                  value={newTask.title}
                  onChange={(e) => setNewTask({ ...newTask, title: e.target.value })}
                  className="input w-full"
                  placeholder="What needs to be done?"
                  required
                  autoFocus
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Description
                </label>
                <textarea
                  value={newTask.description}
                  onChange={(e) => setNewTask({ ...newTask, description: e.target.value })}
                  className="input w-full"
                  rows={3}
                  placeholder="Add details..."
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Priority
                </label>
                <select
                  value={newTask.priority}
                  onChange={(e) => setNewTask({ ...newTask, priority: e.target.value })}
                  className="input w-full"
                >
                  <option value="low">Low</option>
                  <option value="normal">Normal</option>
                  <option value="high">High</option>
                  <option value="urgent">Urgent</option>
                </select>
              </div>

              {createError && (
                <p role="alert" className="text-sm text-red-600 dark:text-red-400">
                  {createError}
                </p>
              )}

              <div className="flex justify-end gap-3 pt-4">
                <button
                  type="button"
                  onClick={() => { setShowCreateModal(false); setCreateError('') }}
                  className="btn"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={creating}
                  className="btn-primary"
                >
                  {creating ? 'Creating...' : 'Create Task'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Members Panel (Slide-over) */}
      {showMembersPanel && (
        <div className="fixed inset-0 z-50 overflow-hidden">
          <div className="absolute inset-0 bg-black/50" onClick={() => setShowMembersPanel(false)} />
          <div className="absolute inset-y-0 right-0 max-w-md w-full bg-white dark:bg-gray-900 shadow-xl flex flex-col">
            {/* Panel Header */}
            <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-800">
              <div>
                <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                  Project Members
                </h2>
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  {project?.name}
                </p>
              </div>
              <button
                onClick={() => setShowMembersPanel(false)}
                className="btn-icon"
              >
                <X size={20} />
              </button>
            </div>

            {/* Add Member Button */}
            <div className="p-4 border-b border-gray-200 dark:border-gray-800">
              <button
                onClick={handleOpenInviteModal}
                className="btn-primary w-full flex items-center justify-center gap-2"
              >
                <UserPlus size={18} />
                Invite Member
              </button>
            </div>

            {/* Members List */}
            <div className="flex-1 overflow-y-auto p-4">
              {loadingMembers ? (
                <div className="flex items-center justify-center py-8">
                  <LoadingSpinner />
                </div>
              ) : members.length === 0 ? (
                <div className="text-center py-8 text-gray-500 dark:text-gray-400">
                  <Users className="mx-auto h-12 w-12 mb-2 opacity-50" />
                  <p>No members yet</p>
                </div>
              ) : (
                <div className="space-y-3">
                  {members.map((member) => {
                    const RoleIcon = getRoleIcon(member.role)
                    return (
                      <div
                        key={member.id}
                        className="flex items-center justify-between p-3 rounded-lg bg-gray-50 dark:bg-gray-800"
                      >
                        <div className="flex items-center gap-3">
                          <div className="w-10 h-10 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center">
                            <span className="text-brand-600 dark:text-brand-400 font-medium">
                              {(member.user_email || member.user_name || '?')[0].toUpperCase()}
                            </span>
                          </div>
                          <div>
                            <p className="font-medium text-gray-900 dark:text-white">
                              {member.user_name || member.user_email?.split('@')[0] || 'Unknown'}
                            </p>
                            <p className="text-sm text-gray-500 dark:text-gray-400">
                              {member.user_email}
                            </p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className={cn(
                            'flex items-center gap-1 px-2 py-1 rounded text-xs font-medium',
                            getRoleBadgeColor(member.role)
                          )}>
                            <RoleIcon size={12} />
                            {member.role}
                          </span>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            {/* Permission Legend */}
            <div className="p-4 border-t border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/50">
              <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">Roles</p>
              <div className="flex flex-wrap gap-2 text-xs">
                <span className="flex items-center gap-1 text-purple-600 dark:text-purple-400">
                  <Crown size={12} /> Admin - Full control
                </span>
                <span className="flex items-center gap-1 text-blue-600 dark:text-blue-400">
                  <Edit3 size={12} /> Member - Edit items
                </span>
                <span className="flex items-center gap-1 text-gray-500 dark:text-gray-400">
                  <Eye size={12} /> Viewer - Read only
                </span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Invite Member Modal */}
      {showInviteModal && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
                Invite to {project?.name}
              </h2>
              <button
                onClick={() => setShowInviteModal(false)}
                className="btn-icon"
              >
                <X size={20} />
              </button>
            </div>

            <form onSubmit={handleInviteMember} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Email Address
                </label>
                <input
                  type="email"
                  value={inviteEmail}
                  onChange={(e) => {
                    setInviteEmail(e.target.value)
                    setAddToOrgFirst(false)
                    setInviteError(null)
                  }}
                  className="input w-full"
                  placeholder="colleague@example.com"
                  required
                  disabled={addToOrgFirst}
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Project Role
                </label>
                <select
                  value={inviteRole}
                  onChange={(e) => setInviteRole(e.target.value)}
                  className="input w-full"
                  disabled={addToOrgFirst}
                >
                  <option value="admin">Admin - Full project control</option>
                  <option value="member">Member - Create and edit items</option>
                  <option value="viewer">Viewer - Read-only access</option>
                </select>
              </div>

              {inviteError && (
                <div className="p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg">
                  {inviteError}
                </div>
              )}

              {addToOrgFirst && (
                <div className="p-4 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg">
                  <p className="text-sm text-amber-800 dark:text-amber-200 mb-3">
                    <strong>{inviteEmail}</strong> is not a member of <strong>{currentOrganization?.name}</strong>.
                  </p>
                  <p className="text-sm text-amber-700 dark:text-amber-300 mb-3">
                    Would you like to invite them to the organization and add them to this project?
                  </p>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={handleAddToOrgAndProject}
                      disabled={addingToOrg}
                      className="btn-primary text-sm"
                    >
                      {addingToOrg ? 'Adding...' : 'Yes, invite to organization & project'}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setAddToOrgFirst(false)
                        setInviteEmail('')
                      }}
                      className="btn text-sm"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {inviteSuccess && (
                <div className="p-3 bg-green-50 dark:bg-green-900/30 text-green-600 dark:text-green-400 text-sm rounded-lg">
                  Member added successfully!
                </div>
              )}

              {!addToOrgFirst && (
                <div className="flex justify-end gap-3 pt-4">
                  <button
                    type="button"
                    onClick={() => setShowInviteModal(false)}
                    className="btn"
                  >
                    {inviteSuccess ? 'Done' : 'Cancel'}
                  </button>
                  {!inviteSuccess && (
                    <button
                      type="submit"
                      disabled={inviting || !inviteEmail}
                      className="btn-primary"
                    >
                      {inviting ? 'Adding...' : 'Add to Project'}
                    </button>
                  )}
                </div>
              )}
            </form>
          </div>
        </div>
      )}

      {/* Assignment Modal */}
      {showAssignModal && assigningTask && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
                Assign Task
              </h2>
              <button
                onClick={() => {
                  setShowAssignModal(false)
                  setAssigningTask(null)
                }}
                className="btn-icon"
              >
                <X size={20} />
              </button>
            </div>

            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              {assigningTask.item_key}: {assigningTask.title}
            </p>

            {assigningTask.assignee_email && (
              <div className="mb-4 p-3 bg-gray-50 dark:bg-gray-700 rounded-lg">
                <p className="text-xs text-gray-500 dark:text-gray-400 mb-1">Currently assigned to:</p>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <div className="w-8 h-8 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center">
                      <span className="text-brand-600 dark:text-brand-400 font-medium">
                        {(assigningTask.assignee_name || assigningTask.assignee_email)[0].toUpperCase()}
                      </span>
                    </div>
                    <span className="font-medium text-gray-900 dark:text-white">
                      {assigningTask.assignee_name || assigningTask.assignee_email.split('@')[0]}
                    </span>
                  </div>
                  <button
                    onClick={handleUnassignTask}
                    disabled={assigning}
                    className="text-sm text-red-600 dark:text-red-400 hover:underline"
                  >
                    Remove
                  </button>
                </div>
              </div>
            )}

            <div className="space-y-2 max-h-60 overflow-y-auto">
              <p className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                {assigningTask.assignee_email ? 'Reassign to:' : 'Assign to:'}
              </p>
              {loadingMembers ? (
                <div className="flex items-center justify-center py-4">
                  <LoadingSpinner />
                </div>
              ) : members.length === 0 ? (
                <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
                  No project members found
                </p>
              ) : (
                members.map((member) => (
                  <button
                    key={member.id}
                    onClick={() => handleAssignTask(member.user_id, member.user_email)}
                    disabled={assigning || member.user_email === assigningTask.assignee_email}
                    className={cn(
                      'w-full flex items-center gap-3 p-3 rounded-lg transition-colors',
                      member.user_email === assigningTask.assignee_email
                        ? 'bg-brand-50 dark:bg-brand-900/20 cursor-default'
                        : 'hover:bg-gray-100 dark:hover:bg-gray-700'
                    )}
                  >
                    <div className="w-8 h-8 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center">
                      <span className="text-brand-600 dark:text-brand-400 font-medium">
                        {(member.user_name || member.user_email || '?')[0].toUpperCase()}
                      </span>
                    </div>
                    <div className="text-left flex-1">
                      <p className="font-medium text-gray-900 dark:text-white">
                        {member.user_name || member.user_email?.split('@')[0] || 'Unknown'}
                      </p>
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        {member.user_email}
                      </p>
                    </div>
                    {member.user_email === assigningTask.assignee_email && (
                      <span className="text-xs text-brand-600 dark:text-brand-400">
                        Current
                      </span>
                    )}
                  </button>
                ))
              )}
            </div>

            <div className="flex justify-end gap-3 mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
              <button
                onClick={() => {
                  setShowAssignModal(false)
                  setAssigningTask(null)
                }}
                className="btn"
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
