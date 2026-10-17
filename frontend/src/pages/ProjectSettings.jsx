import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Save, Trash2, Archive, Users, UserPlus, X, AlertTriangle,
  Settings, Crown, UserMinus, Layers, Tag, GitBranch, LayoutGrid, Plus, Edit2,
  Link2, Zap, Shield, Bell, Clock
} from 'lucide-react'
import { projectsApi, organizationsApi, componentsApi, versionsApi, projectConfigApi,
  issueTypeSchemesApi, permissionSchemesApi, notificationSchemesApi, securitySchemesApi
} from '../services/api'
import { useOrganization } from '../contexts/OrganizationContext'
import LoadingSpinner from '../components/LoadingSpinner'
import { cn } from '../utils/cn'
import WorkflowEditor from '../components/WorkflowEditor'
import BoardColumnConfig from '../components/BoardColumnConfig'
import UserSearchInput from '../components/UserSearchInput'

export default function ProjectSettings() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const { currentOrganization, refreshProjects } = useOrganization()

  const [project, setProject] = useState(null)
  const [members, setMembers] = useState([])
  const [orgMembers, setOrgMembers] = useState([])
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(null)

  // Form state
  const [formData, setFormData] = useState({
    name: '',
    key: '',
    description: '',
    project_type: 'software',
    access_level: 'limited',
    lead_email: '',
    default_assignee_email: '',
  })

  // Add member modal
  const [showAddMember, setShowAddMember] = useState(false)
  const [newMemberEmail, setNewMemberEmail] = useState('')
  const [newMemberRole, setNewMemberRole] = useState('member')
  const [addingMember, setAddingMember] = useState(false)
  const [addMemberError, setAddMemberError] = useState(null)

  // Delete confirmation
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleteConfirmText, setDeleteConfirmText] = useState('')
  const [isDeleting, setIsDeleting] = useState(false)

  // Settings tab
  const [activeTab, setActiveTab] = useState('general')

  useEffect(() => {
    loadProject()
    loadMembers()
  }, [projectId])

  useEffect(() => {
    if (currentOrganization) {
      loadOrgMembers()
    }
  }, [currentOrganization])

  const loadProject = async () => {
    try {
      const response = await projectsApi.get(projectId)
      const data = response.data
      setProject(data)
      setFormData({
        name: data.name || '',
        key: data.key || '',
        description: data.description || '',
        project_type: data.project_type || 'software',
        access_level: data.access_level || 'limited',
        lead_email: data.lead_email || '',
        default_assignee_email: data.default_assignee_email || '',
      })
    } catch (err) {
      setError('Failed to load project')
      console.error(err)
    } finally {
      setIsLoading(false)
    }
  }

  const loadMembers = async () => {
    try {
      const response = await projectsApi.members(projectId)
      // API returns {count, members} not {results}
      setMembers(response.data.members || response.data.results || response.data || [])
    } catch (err) {
      console.error('Failed to load members:', err)
      setMembers([])
    }
  }

  const loadOrgMembers = async () => {
    try {
      const response = await organizationsApi.members(currentOrganization.slug)
      setOrgMembers(response.data.results || response.data || [])
    } catch (err) {
      console.error('Failed to load org members:', err)
    }
  }

  const handleSave = async (e) => {
    e.preventDefault()
    setIsSaving(true)
    setError(null)
    setSuccess(null)

    try {
      await projectsApi.update(projectId, formData)
      setSuccess('Project settings saved successfully')
      if (refreshProjects) refreshProjects()
      // Reload project to get updated data
      loadProject()
    } catch (err) {
      setError(err.response?.data?.detail || err.response?.data?.key?.[0] || 'Failed to save settings')
    } finally {
      setIsSaving(false)
    }
  }

  const handleArchive = async () => {
    try {
      await projectsApi.archive(projectId)
      if (refreshProjects) refreshProjects()
      navigate('/projects')
    } catch (err) {
      setError('Failed to archive project')
    }
  }

  const handleDelete = async () => {
    if (deleteConfirmText !== project.key) return

    setIsDeleting(true)
    try {
      await projectsApi.delete(projectId)
      if (refreshProjects) refreshProjects()
      navigate('/projects')
    } catch (err) {
      setError('Failed to delete project')
      setIsDeleting(false)
    }
  }

  const handleAddMember = async (e) => {
    e.preventDefault()
    setAddingMember(true)
    setAddMemberError(null)

    try {
      await projectsApi.addMember(projectId, {
        user_email: newMemberEmail,
        role: newMemberRole,
        can_browse: true,
        can_create: newMemberRole !== 'viewer',
        can_edit: newMemberRole !== 'viewer',
        can_delete: newMemberRole === 'admin',
        can_manage_project: newMemberRole === 'admin',
        can_manage_members: newMemberRole === 'admin',
      })
      setShowAddMember(false)
      setNewMemberEmail('')
      setNewMemberRole('member')
      loadMembers()
    } catch (err) {
      setAddMemberError(
        err.response?.data?.detail ||
        err.response?.data?.user_email?.[0] ||
        err.response?.data?.error ||
        'Failed to add member'
      )
    } finally {
      setAddingMember(false)
    }
  }

  const handleRemoveMember = async (memberId) => {
    try {
      await projectsApi.removeMember?.(projectId, memberId) ||
        await fetch(`/pulse/api/projects/${projectId}/members/${memberId}/`, { method: 'DELETE' })
      loadMembers()
    } catch (err) {
      console.error('Failed to remove member:', err)
    }
  }

  const handleKeyChange = (e) => {
    const value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 10)
    setFormData({ ...formData, key: value })
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  if (!project) {
    return (
      <div className="text-center py-12">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white">Project not found</h2>
        <button onClick={() => navigate('/projects')} className="btn-primary mt-4">
          Back to Projects
        </button>
      </div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <button
          onClick={() => navigate(`/projects/${projectId}`)}
          className="btn-icon"
        >
          <ArrowLeft size={20} />
        </button>
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Project Settings
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            {project.name} ({project.key})
          </p>
        </div>
      </div>

      {/* Success/Error Messages */}
      {success && (
        <div className="p-4 bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300 rounded-lg">
          {success}
        </div>
      )}
      {error && (
        <div className="p-4 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300 rounded-lg">
          {error}
        </div>
      )}

      {/* Tab Navigation */}
      <div className="border-b border-gray-200 dark:border-gray-700">
        <nav className="flex space-x-6 overflow-x-auto">
          {[
            { id: 'general', label: 'General', icon: Settings },
            { id: 'members', label: 'Members', icon: Users },
            { id: 'components', label: 'Components', icon: Layers },
            { id: 'versions', label: 'Versions', icon: Tag },
            { id: 'workflow', label: 'Workflow', icon: GitBranch },
            { id: 'board', label: 'Board', icon: LayoutGrid },
            { id: 'configuration', label: 'Schemes', icon: Link2 },
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={cn(
                'flex items-center gap-2 px-1 py-3 text-sm font-medium border-b-2 whitespace-nowrap transition-colors',
                activeTab === tab.id
                  ? 'border-brand-500 text-brand-600 dark:text-brand-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
              )}
            >
              <tab.icon size={16} />
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* General Settings */}
      {activeTab === 'general' && (
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4 flex items-center gap-2">
          <Settings size={20} />
          General Settings
        </h2>
        <form onSubmit={handleSave} className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Project Name
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="input w-full"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Project Key
              </label>
              <input
                type="text"
                value={formData.key}
                onChange={handleKeyChange}
                className="input w-full uppercase"
                maxLength={10}
                required
              />
              <p className="mt-1 text-xs text-gray-500">Used for item IDs (e.g., {formData.key}-123)</p>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Description
            </label>
            <textarea
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              className="input w-full"
              rows={3}
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Project Type
              </label>
              <select
                value={formData.project_type}
                onChange={(e) => setFormData({ ...formData, project_type: e.target.value })}
                className="input w-full"
              >
                <option value="software">Software Development</option>
                <option value="business">Business Project</option>
                <option value="service_management">Service Management</option>
                <option value="operations">Operations</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Access Level
              </label>
              <select
                value={formData.access_level}
                onChange={(e) => setFormData({ ...formData, access_level: e.target.value })}
                className="input w-full"
              >
                <option value="open">Open - All org members can access</option>
                <option value="limited">Limited - Only project members</option>
                <option value="private">Private - Invitation only</option>
              </select>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Project Lead
              </label>
              <select
                value={formData.lead_email}
                onChange={(e) => setFormData({ ...formData, lead_email: e.target.value })}
                className="input w-full"
              >
                <option value="">No lead assigned</option>
                {members.map((member) => (
                  <option key={member.id} value={member.user_email}>
                    {member.user_name || member.user_email?.split('@')[0]} ({member.user_email})
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Default Assignee
              </label>
              <UserSearchInput
                projectId={projectId}
                organizationId={project?.organization ?? project?.org_id}
                organizationSlug={project?.organization_slug}
                organizationName={project?.organization_name}
                onSelect={(member) => setFormData({
                  ...formData,
                  default_assignee_email: member?.user_email || ''
                })}
                currentValue={formData.default_assignee_email ? {
                  email: formData.default_assignee_email,
                  name: formData.default_assignee_email.split('@')[0],
                } : null}
                placeholder="Search for default assignee..."
              />
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                New tasks will be assigned to this user by default
              </p>
            </div>
          </div>

          <div className="flex justify-end pt-4">
            <button
              type="submit"
              disabled={isSaving}
              className="btn-primary flex items-center gap-2"
            >
              <Save size={18} />
              {isSaving ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </form>
      </div>
      )}

      {/* Members Section */}
      {activeTab === 'members' && (
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <Users size={20} />
            Project Members ({members.length})
          </h2>
          <button
            onClick={() => setShowAddMember(true)}
            className="btn-primary flex items-center gap-2"
          >
            <UserPlus size={18} />
            Add Member
          </button>
        </div>

        <div className="divide-y divide-gray-200 dark:divide-gray-700">
          {members.length === 0 ? (
            <p className="py-4 text-gray-500 dark:text-gray-400 text-center">
              No members yet
            </p>
          ) : (
            members.map((member) => (
              <div key={member.id} className="py-3 flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center text-brand-600 dark:text-brand-400 font-medium">
                    {(member.user_name || member.user_email)?.charAt(0).toUpperCase()}
                  </div>
                  <div>
                    <p className="font-medium text-gray-900 dark:text-white">
                      {member.user_name || member.user_email?.split('@')[0]}
                    </p>
                    <p className="text-sm text-gray-500 dark:text-gray-400">
                      {member.user_email}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className={cn(
                    'px-2 py-1 text-xs rounded',
                    member.role === 'admin' && 'bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300',
                    member.role === 'member' && 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300',
                    member.role === 'viewer' && 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
                  )}>
                    {member.role}
                  </span>
                  {members.length > 1 && (
                    <button
                      onClick={() => handleRemoveMember(member.id)}
                      className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                      title="Remove member"
                    >
                      <UserMinus size={16} />
                    </button>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
      )}

      {/* Components Section */}
      {activeTab === 'components' && (
        <ComponentsSection projectId={projectId} />
      )}

      {/* Versions Section */}
      {activeTab === 'versions' && (
        <VersionsSection projectId={projectId} />
      )}

      {/* Workflow Section */}
      {activeTab === 'workflow' && (
        <div className="card">
          <WorkflowEditor projectId={projectId} />
        </div>
      )}

      {/* Board Configuration Section */}
      {activeTab === 'board' && (
        <div className="card">
          <BoardColumnConfig projectId={projectId} />
        </div>
      )}

      {/* Configuration / Schemes Tab */}
      {activeTab === 'configuration' && (
        <ProjectConfigurationSection projectId={projectId} />
      )}

      {/* Danger Zone - only on general tab */}
      {activeTab === 'general' && (
      <div className="card border-red-200 dark:border-red-800">
        <h2 className="text-lg font-semibold text-red-600 dark:text-red-400 mb-4 flex items-center gap-2">
          <AlertTriangle size={20} />
          Danger Zone
        </h2>
        <div className="space-y-4">
          <div className="flex items-center justify-between p-4 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <div>
              <h3 className="font-medium text-gray-900 dark:text-white">Archive Project</h3>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Archive this project. It can be restored later.
              </p>
            </div>
            <button
              onClick={handleArchive}
              className="btn flex items-center gap-2 border-yellow-300 text-yellow-700 hover:bg-yellow-50 dark:border-yellow-700 dark:text-yellow-400 dark:hover:bg-yellow-900/30"
            >
              <Archive size={18} />
              Archive
            </button>
          </div>

          <div className="flex items-center justify-between p-4 bg-red-50 dark:bg-red-900/20 rounded-lg">
            <div>
              <h3 className="font-medium text-red-700 dark:text-red-300">Delete Project</h3>
              <p className="text-sm text-red-600 dark:text-red-400">
                Permanently delete this project and all its items. This cannot be undone.
              </p>
            </div>
            <button
              onClick={() => setShowDeleteConfirm(true)}
              className="btn flex items-center gap-2 border-red-300 text-red-700 hover:bg-red-100 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-900/30"
            >
              <Trash2 size={18} />
              Delete
            </button>
          </div>
        </div>
      </div>
      )}

      {/* Add Member Modal */}
      {showAddMember && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
                Add Member
              </h2>
              <button onClick={() => setShowAddMember(false)} className="btn-icon">
                <X size={20} />
              </button>
            </div>

            <form onSubmit={handleAddMember} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Email Address
                </label>
                <input
                  type="email"
                  value={newMemberEmail}
                  onChange={(e) => setNewMemberEmail(e.target.value)}
                  className="input w-full"
                  placeholder="colleague@example.com"
                  list="org-members"
                  required
                />
                <datalist id="org-members">
                  {orgMembers.map((m) => (
                    <option key={m.id} value={m.user_email} />
                  ))}
                </datalist>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Role
                </label>
                <select
                  value={newMemberRole}
                  onChange={(e) => setNewMemberRole(e.target.value)}
                  className="input w-full"
                >
                  <option value="admin">Admin - Full project control</option>
                  <option value="member">Member - Create and edit items</option>
                  <option value="viewer">Viewer - Read-only access</option>
                </select>
              </div>

              {addMemberError && (
                <div className="p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg">
                  {addMemberError}
                </div>
              )}

              <div className="flex justify-end gap-3 pt-4">
                <button
                  type="button"
                  onClick={() => setShowAddMember(false)}
                  className="btn"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={addingMember}
                  className="btn-primary"
                >
                  {addingMember ? 'Adding...' : 'Add Member'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
            <div className="flex items-center gap-3 mb-4 text-red-600 dark:text-red-400">
              <AlertTriangle size={24} />
              <h2 className="text-xl font-semibold">Delete Project</h2>
            </div>

            <p className="text-gray-600 dark:text-gray-300 mb-4">
              This action cannot be undone. All items, comments, and history will be permanently deleted.
            </p>

            <p className="text-gray-600 dark:text-gray-300 mb-2">
              Type <strong>{project.key}</strong> to confirm:
            </p>
            <input
              type="text"
              value={deleteConfirmText}
              onChange={(e) => setDeleteConfirmText(e.target.value)}
              className="input w-full mb-4"
              placeholder={project.key}
            />

            <div className="flex justify-end gap-3">
              <button
                onClick={() => {
                  setShowDeleteConfirm(false)
                  setDeleteConfirmText('')
                }}
                className="btn"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
                disabled={deleteConfirmText !== project.key || isDeleting}
                className="btn bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isDeleting ? 'Deleting...' : 'Delete Project'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// Components Section Component
function ComponentsSection({ projectId }) {
  const queryClient = useQueryClient()
  const [showAddForm, setShowAddForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [formData, setFormData] = useState({ name: '', description: '', lead_email: '' })

  const { data: components = [], isLoading } = useQuery({
    queryKey: ['components', projectId],
    queryFn: async () => {
      const res = await componentsApi.list({ project: projectId })
      // API returns paginated {results: [...]} or direct array
      return res.data.results || res.data || []
    },
    enabled: !!projectId,
  })

  const createMutation = useMutation({
    mutationFn: (data) => componentsApi.create({ project: projectId, ...data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['components', projectId] })
      setShowAddForm(false)
      setFormData({ name: '', description: '', lead_email: '' })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => componentsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['components', projectId] })
      setEditingId(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => componentsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['components', projectId] })
    },
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    if (editingId) {
      updateMutation.mutate({ id: editingId, ...formData })
    } else {
      createMutation.mutate(formData)
    }
  }

  const startEdit = (component) => {
    setEditingId(component.id)
    setFormData({
      name: component.name,
      description: component.description || '',
      lead_email: component.lead_email || '',
    })
    setShowAddForm(true)
  }

  if (isLoading) {
    return <div className="card p-8 text-center"><LoadingSpinner /></div>
  }

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <Layers size={20} />
            Components ({components.length})
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Organize items by functional areas
          </p>
        </div>
        <button
          onClick={() => {
            setEditingId(null)
            setFormData({ name: '', description: '', lead_email: '' })
            setShowAddForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          Add Component
        </button>
      </div>

      {showAddForm && (
        <form onSubmit={handleSubmit} className="mb-4 p-4 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Name *
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="input w-full"
                placeholder="e.g., Frontend, API, Database"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Lead Email
              </label>
              <input
                type="email"
                value={formData.lead_email}
                onChange={(e) => setFormData({ ...formData, lead_email: e.target.value })}
                className="input w-full"
                placeholder="lead@example.com"
              />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Description
            </label>
            <textarea
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              className="input w-full"
              rows={2}
              placeholder="Brief description of this component"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setShowAddForm(false)
                setEditingId(null)
              }}
              className="btn"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || updateMutation.isPending}
              className="btn-primary"
            >
              {editingId ? 'Update' : 'Create'} Component
            </button>
          </div>
        </form>
      )}

      <div className="divide-y divide-gray-200 dark:divide-gray-700">
        {components.length === 0 ? (
          <p className="py-8 text-gray-500 dark:text-gray-400 text-center">
            No components defined yet
          </p>
        ) : (
          components.map((component) => (
            <div key={component.id} className="py-3 flex items-center justify-between">
              <div>
                <p className="font-medium text-gray-900 dark:text-white">
                  {component.name}
                </p>
                {component.description && (
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    {component.description}
                  </p>
                )}
                {component.lead_email && (
                  <p className="text-xs text-gray-400">
                    Lead: {component.lead_email}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-sm text-gray-500">
                  {component.item_count || 0} items
                </span>
                <button
                  onClick={() => startEdit(component)}
                  className="btn-icon"
                  title="Edit"
                >
                  <Edit2 size={16} />
                </button>
                <button
                  onClick={() => {
                    if (confirm('Delete this component?')) {
                      deleteMutation.mutate(component.id)
                    }
                  }}
                  className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                  title="Delete"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

// Versions Section Component
function VersionsSection({ projectId }) {
  const queryClient = useQueryClient()
  const [showAddForm, setShowAddForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    status: 'planning',
    release_date: '',
  })

  const { data: versions = [], isLoading } = useQuery({
    queryKey: ['versions', projectId],
    queryFn: async () => {
      const res = await versionsApi.list({ project: projectId })
      // API returns paginated {results: [...]} or direct array
      return res.data.results || res.data || []
    },
    enabled: !!projectId,
  })

  const createMutation = useMutation({
    mutationFn: (data) => versionsApi.create({ project: projectId, ...data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['versions', projectId] })
      setShowAddForm(false)
      setFormData({ name: '', description: '', status: 'planning', release_date: '' })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => versionsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['versions', projectId] })
      setEditingId(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => versionsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['versions', projectId] })
    },
  })

  const releaseMutation = useMutation({
    mutationFn: (id) => versionsApi.release(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['versions', projectId] })
    },
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    const data = {
      ...formData,
      release_date: formData.release_date || null,
    }
    if (editingId) {
      updateMutation.mutate({ id: editingId, ...data })
    } else {
      createMutation.mutate(data)
    }
  }

  const startEdit = (version) => {
    setEditingId(version.id)
    setFormData({
      name: version.name,
      description: version.description || '',
      status: version.status,
      release_date: version.release_date?.slice(0, 10) || '',
    })
    setShowAddForm(true)
  }

  const navigate = useNavigate()

  const statusColors = {
    planning: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
    active: 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
    released: 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400',
    archived: 'bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-500',
  }

  if (isLoading) {
    return <div className="card p-8 text-center"><LoadingSpinner /></div>
  }

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <Tag size={20} />
            Versions ({versions.length})
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Track releases and milestones
          </p>
        </div>
        <button
          onClick={() => {
            setEditingId(null)
            setFormData({ name: '', description: '', status: 'planning', release_date: '' })
            setShowAddForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          Add Version
        </button>
      </div>

      {showAddForm && (
        <form onSubmit={handleSubmit} className="mb-4 p-4 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Version Name *
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="input w-full"
                placeholder="e.g., 1.0.0, Sprint 1, Q1 Release"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Status
              </label>
              <select
                value={formData.status}
                onChange={(e) => setFormData({ ...formData, status: e.target.value })}
                className="input w-full"
              >
                <option value="planning">Planning</option>
                <option value="active">Active</option>
                <option value="released">Released</option>
                <option value="archived">Archived</option>
              </select>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Release Date
              </label>
              <input
                type="date"
                value={formData.release_date}
                onChange={(e) => setFormData({ ...formData, release_date: e.target.value })}
                className="input w-full"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Description
              </label>
              <input
                type="text"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                className="input w-full"
                placeholder="Brief description"
              />
            </div>
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setShowAddForm(false)
                setEditingId(null)
              }}
              className="btn"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || updateMutation.isPending}
              className="btn-primary"
            >
              {editingId ? 'Update' : 'Create'} Version
            </button>
          </div>
        </form>
      )}

      <div className="divide-y divide-gray-200 dark:divide-gray-700">
        {versions.length === 0 ? (
          <p className="py-8 text-gray-500 dark:text-gray-400 text-center">
            No versions defined yet
          </p>
        ) : (
          versions.map((version) => (
            <div key={version.id} className="py-3 flex items-center justify-between">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="font-medium text-gray-900 dark:text-white">
                    {version.name}
                  </p>
                  <span className={cn(
                    'px-2 py-0.5 text-xs rounded',
                    statusColors[version.status]
                  )}>
                    {version.status}
                  </span>
                </div>
                {version.description && (
                  <p className="text-sm text-gray-500 dark:text-gray-400 truncate">
                    {version.description}
                  </p>
                )}
                <div className="flex items-center gap-4 text-xs text-gray-400 mt-1">
                  {version.release_date && (
                    <span>Release: {new Date(version.release_date).toLocaleDateString()}</span>
                  )}
                  <span>{version.item_count || 0} items</span>
                  {version.progress_percent !== undefined && (
                    <span>{version.progress_percent}% complete</span>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2">
                {version.status !== 'released' && (
                  <button
                    onClick={() => {
                      if (confirm('Mark this version as released?')) {
                        releaseMutation.mutate(version.id)
                      }
                    }}
                    className="btn-secondary text-sm"
                    title="Release"
                  >
                    Release
                  </button>
                )}
                <button
                  onClick={() => startEdit(version)}
                  className="btn-icon"
                  title="Edit"
                >
                  <Edit2 size={16} />
                </button>
                <button
                  onClick={() => {
                    if (confirm('Delete this version?')) {
                      deleteMutation.mutate(version.id)
                    }
                  }}
                  className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                  title="Delete"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

// ---- Project Configuration Section ----
function ProjectConfigurationSection({ projectId }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const { data: configs = [], isLoading: configLoading } = useQuery({
    queryKey: ['projectConfig', projectId],
    queryFn: async () => {
      const res = await projectConfigApi.list({ project: projectId })
      return res.data.results || res.data || []
    },
    enabled: !!projectId,
  })

  const { data: itsOptions = [] } = useQuery({
    queryKey: ['issueTypeSchemesOpts'],
    queryFn: () => issueTypeSchemesApi.list().then(r => r.data.results || r.data || []),
  })
  const { data: permOptions = [] } = useQuery({
    queryKey: ['permSchemesOpts'],
    queryFn: () => permissionSchemesApi.list().then(r => r.data.results || r.data || []),
  })
  const { data: notifOptions = [] } = useQuery({
    queryKey: ['notifSchemesOpts'],
    queryFn: () => notificationSchemesApi.list().then(r => r.data.results || r.data || []),
  })
  const { data: secOptions = [] } = useQuery({
    queryKey: ['secSchemesOpts'],
    queryFn: () => securitySchemesApi.list().then(r => r.data.results || r.data || []),
  })

  const config = configs[0] || null
  const [formData, setFormData] = useState({
    issue_type_scheme: '', permission_scheme: '', notification_scheme: '', security_scheme: '',
  })
  const [initialized, setInitialized] = useState(false)

  if (config && !initialized) {
    setFormData({
      issue_type_scheme: config.issue_type_scheme || '',
      permission_scheme: config.permission_scheme || '',
      notification_scheme: config.notification_scheme || '',
      security_scheme: config.security_scheme || '',
    })
    setInitialized(true)
  }

  const saveMutation = useMutation({
    mutationFn: (data) => config
      ? projectConfigApi.update(config.id, { project: projectId, ...data })
      : projectConfigApi.create({ project: projectId, ...data }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projectConfig', projectId] }),
  })

  if (configLoading) return <div className="card p-8 text-center"><LoadingSpinner /></div>

  const fields = [
    { key: 'issue_type_scheme', label: 'Issue Type Scheme', icon: Layers, options: itsOptions, desc: 'Which issue types are available', link: '/admin/issue-types' },
    { key: 'permission_scheme', label: 'Permission Scheme', icon: Shield, options: permOptions, desc: 'Who can perform which actions', link: '/admin/permission-schemes' },
    { key: 'notification_scheme', label: 'Notification Scheme', icon: Bell, options: notifOptions, desc: 'Who gets notified for events', link: '/admin/notification-schemes' },
    { key: 'security_scheme', label: 'Security Scheme', icon: Shield, options: secOptions, desc: 'Issue-level security', link: '/admin/security-schemes' },
  ]

  return (
    <div className="card">
      <div className="mb-4">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
          <Link2 size={20} /> Project Schemes
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Link global schemes to control issue types, permissions, notifications, and security
        </p>
      </div>
      <form onSubmit={(e) => { e.preventDefault(); saveMutation.mutate(formData) }} className="space-y-4">
        {fields.map(f => (
          <div key={f.key} className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <div className="flex items-center justify-between mb-2">
              <label className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300">
                <f.icon size={16} /> {f.label}
              </label>
              <button type="button" onClick={() => navigate(f.link)} className="text-xs text-brand-600 dark:text-brand-400 hover:underline">
                Manage
              </button>
            </div>
            <select value={formData[f.key]} onChange={(e) => setFormData(p => ({ ...p, [f.key]: e.target.value }))} className="input w-full">
              <option value="">None (use defaults)</option>
              {f.options.map(o => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{f.desc}</p>
          </div>
        ))}
        <div className="flex justify-end pt-2">
          <button type="submit" disabled={saveMutation.isPending} className="btn-primary flex items-center gap-2">
            <Save size={18} /> {saveMutation.isPending ? 'Saving...' : 'Save Configuration'}
          </button>
        </div>
        {saveMutation.isSuccess && <p className="text-sm text-green-600 dark:text-green-400">Configuration saved.</p>}
        {saveMutation.isError && <p className="text-sm text-red-600 dark:text-red-400">{saveMutation.error?.response?.data?.detail || 'Failed to save.'}</p>}
      </form>
    </div>
  )
}
