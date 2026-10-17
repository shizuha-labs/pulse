import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Shield, Key, Plus, Edit2, Trash2, ChevronDown, ChevronRight, Users } from 'lucide-react'
import { permissionSchemesApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner from '../../components/LoadingSpinner'

const PERMISSIONS = [
  'browse_projects', 'create_issues', 'edit_issues', 'delete_issues', 'assign_issues',
  'assignable_user', 'close_issues', 'resolve_issues', 'transition_issues',
  'manage_sprints', 'schedule_issues', 'move_issues', 'set_security', 'link_issues',
  'create_attachments', 'delete_attachments', 'add_comments', 'edit_comments',
  'delete_comments', 'edit_own_comments', 'delete_own_comments', 'manage_watchers',
  'view_watchers', 'add_watchers', 'remove_watchers', 'work_on_issues', 'edit_own_worklogs',
  'delete_own_worklogs', 'administer_projects', 'manage_components', 'manage_versions',
  'bulk_change', 'view_workflow',
]

const HOLDER_TYPES = [
  { value: 'user', label: 'User' },
  { value: 'group', label: 'Group' },
  { value: 'project_role', label: 'Project Role' },
  { value: 'project_lead', label: 'Project Lead' },
  { value: 'current_assignee', label: 'Current Assignee' },
  { value: 'reporter', label: 'Reporter' },
  { value: 'anyone', label: 'Anyone' },
  { value: 'authenticated', label: 'Authenticated' },
]

const HOLDER_TYPE_COLORS = {
  user: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  group: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  project_role: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400',
  project_lead: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  current_assignee: 'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400',
  reporter: 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-400',
  anyone: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  authenticated: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
}

const NEEDS_VALUE = new Set(['user', 'group', 'project_role'])

function formatPermission(permission) {
  return permission.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function formatHolderType(type) {
  const found = HOLDER_TYPES.find((h) => h.value === type)
  return found ? found.label : type
}

export default function PermissionSchemes() {
  const queryClient = useQueryClient()
  const [expandedSchemes, setExpandedSchemes] = useState({})
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [editingScheme, setEditingScheme] = useState(null)
  const [addingGrantTo, setAddingGrantTo] = useState(null)
  const [schemeForm, setSchemeForm] = useState({ name: '', slug: '', description: '' })
  const [grantForm, setGrantForm] = useState({ permission: PERMISSIONS[0], holder_type: 'user', holder_value: '' })

  const { data, isLoading, isError } = useQuery({
    queryKey: ['permissionSchemes'],
    queryFn: () => permissionSchemesApi.list().then((res) => res.data),
  })

  const schemes = data?.results || data || []

  const createMutation = useMutation({
    mutationFn: (data) => permissionSchemesApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['permissionSchemes'] })
      setShowCreateForm(false)
      setSchemeForm({ name: '', slug: '', description: '' })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => permissionSchemesApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['permissionSchemes'] })
      setEditingScheme(null)
      setSchemeForm({ name: '', slug: '', description: '' })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => permissionSchemesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['permissionSchemes'] })
    },
  })

  const addGrantMutation = useMutation({
    mutationFn: ({ schemeId, ...data }) => permissionSchemesApi.addGrant(schemeId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['permissionSchemes'] })
      setAddingGrantTo(null)
      setGrantForm({ permission: PERMISSIONS[0], holder_type: 'user', holder_value: '' })
    },
  })

  const removeGrantMutation = useMutation({
    mutationFn: ({ schemeId, grantId }) => permissionSchemesApi.removeGrant(schemeId, { grant_id: grantId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['permissionSchemes'] })
    },
  })

  const toggleExpand = (schemeId) => {
    setExpandedSchemes((prev) => ({ ...prev, [schemeId]: !prev[schemeId] }))
  }

  const handleCreateScheme = (e) => {
    e.preventDefault()
    createMutation.mutate(schemeForm)
  }

  const handleUpdateScheme = (e) => {
    e.preventDefault()
    updateMutation.mutate({ id: editingScheme, ...schemeForm })
  }

  const startEditScheme = (scheme) => {
    setEditingScheme(scheme.id)
    setSchemeForm({ name: scheme.name, slug: scheme.slug, description: scheme.description || '' })
    setShowCreateForm(true)
  }

  const handleDeleteScheme = (scheme) => {
    if (confirm(`Delete permission scheme "${scheme.name}"? This cannot be undone.`)) {
      deleteMutation.mutate(scheme.id)
    }
  }

  const handleAddGrant = (e) => {
    e.preventDefault()
    const payload = {
      schemeId: addingGrantTo,
      permission: grantForm.permission,
      holder_type: grantForm.holder_type,
    }
    if (NEEDS_VALUE.has(grantForm.holder_type)) {
      payload.holder_value = grantForm.holder_value
    }
    addGrantMutation.mutate(payload)
  }

  const handleRemoveGrant = (schemeId, grantId) => {
    if (confirm('Remove this permission grant?')) {
      removeGrantMutation.mutate({ schemeId, grantId })
    }
  }

  // Group grants by permission name
  const groupGrantsByPermission = (grants) => {
    const grouped = {}
    for (const grant of grants) {
      const perm = grant.permission
      if (!grouped[perm]) grouped[perm] = []
      grouped[perm].push(grant)
    }
    return Object.entries(grouped).sort(([a], [b]) => a.localeCompare(b))
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  if (isError) {
    return (
      <div className="p-4 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300 rounded-lg">
        Failed to load permission schemes. Please try again.
      </div>
    )
  }

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <Key size={24} />
            Permission Schemes
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            Manage permission schemes and their grants
          </p>
        </div>
        <button
          onClick={() => {
            setEditingScheme(null)
            setSchemeForm({ name: '', slug: '', description: '' })
            setShowCreateForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          New Scheme
        </button>
      </div>

      {/* Create / Edit Form */}
      {showCreateForm && (
        <form
          onSubmit={editingScheme ? handleUpdateScheme : handleCreateScheme}
          className="card p-4 space-y-3"
        >
          <h2 className="font-semibold text-gray-900 dark:text-gray-100">
            {editingScheme ? 'Edit Scheme' : 'Create Permission Scheme'}
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Name *
              </label>
              <input
                type="text"
                value={schemeForm.name}
                onChange={(e) => setSchemeForm({ ...schemeForm, name: e.target.value })}
                className="input w-full"
                placeholder="e.g., Default Permission Scheme"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Slug *
              </label>
              <input
                type="text"
                value={schemeForm.slug}
                onChange={(e) =>
                  setSchemeForm({
                    ...schemeForm,
                    slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '-'),
                  })
                }
                className="input w-full"
                placeholder="e.g., default-permissions"
                required
              />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Description
            </label>
            <textarea
              value={schemeForm.description}
              onChange={(e) => setSchemeForm({ ...schemeForm, description: e.target.value })}
              className="input w-full"
              rows={2}
              placeholder="Describe this permission scheme"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setShowCreateForm(false)
                setEditingScheme(null)
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
              {createMutation.isPending || updateMutation.isPending
                ? 'Saving...'
                : editingScheme
                  ? 'Update Scheme'
                  : 'Create Scheme'}
            </button>
          </div>
        </form>
      )}

      {/* Schemes List */}
      {schemes.length === 0 ? (
        <div className="card p-8 text-center">
          <Key size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-4" />
          <p className="text-gray-500 dark:text-gray-400">No permission schemes defined yet</p>
          <p className="text-sm text-gray-400 dark:text-gray-500 mt-1">
            Create a scheme to define who can do what in your projects
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {schemes.map((scheme) => {
            const isExpanded = expandedSchemes[scheme.id]
            const grants = scheme.grants || []
            const groupedGrants = groupGrantsByPermission(grants)

            return (
              <div key={scheme.id} className="card">
                {/* Scheme Header */}
                <div className="flex items-center justify-between p-4">
                  <button
                    onClick={() => toggleExpand(scheme.id)}
                    className="flex items-center gap-3 flex-1 min-w-0 text-left"
                  >
                    {isExpanded ? (
                      <ChevronDown size={18} className="text-gray-400 shrink-0" />
                    ) : (
                      <ChevronRight size={18} className="text-gray-400 shrink-0" />
                    )}
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Shield size={16} className="text-gray-500 dark:text-gray-400 shrink-0" />
                        <h3 className="font-medium text-gray-900 dark:text-gray-100 truncate">
                          {scheme.name}
                        </h3>
                        <span className="badge badge-gray text-xs shrink-0">
                          {scheme.slug}
                        </span>
                      </div>
                      {scheme.description && (
                        <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5 truncate">
                          {scheme.description}
                        </p>
                      )}
                    </div>
                  </button>
                  <div className="flex items-center gap-2 ml-4 shrink-0">
                    <span className="text-sm text-gray-500 dark:text-gray-400">
                      {grants.length} grant{grants.length !== 1 ? 's' : ''}
                    </span>
                    <button
                      onClick={() => startEditScheme(scheme)}
                      className="btn-icon"
                      title="Edit scheme"
                    >
                      <Edit2 size={16} />
                    </button>
                    <button
                      onClick={() => handleDeleteScheme(scheme)}
                      className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                      title="Delete scheme"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </div>

                {/* Expanded: Grants */}
                {isExpanded && (
                  <div className="border-t border-gray-200 dark:border-gray-700 px-4 pb-4">
                    {/* Add Grant Button */}
                    <div className="flex justify-end mt-3 mb-2">
                      <button
                        onClick={() => {
                          setAddingGrantTo(addingGrantTo === scheme.id ? null : scheme.id)
                          setGrantForm({ permission: PERMISSIONS[0], holder_type: 'user', holder_value: '' })
                        }}
                        className="btn flex items-center gap-1 text-sm"
                      >
                        <Plus size={16} />
                        Add Grant
                      </button>
                    </div>

                    {/* Add Grant Form */}
                    {addingGrantTo === scheme.id && (
                      <form
                        onSubmit={handleAddGrant}
                        className="mb-3 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3"
                      >
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Permission *
                            </label>
                            <select
                              value={grantForm.permission}
                              onChange={(e) => setGrantForm({ ...grantForm, permission: e.target.value })}
                              className="input w-full"
                            >
                              {PERMISSIONS.map((perm) => (
                                <option key={perm} value={perm}>
                                  {formatPermission(perm)}
                                </option>
                              ))}
                            </select>
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Holder Type *
                            </label>
                            <select
                              value={grantForm.holder_type}
                              onChange={(e) =>
                                setGrantForm({ ...grantForm, holder_type: e.target.value, holder_value: '' })
                              }
                              className="input w-full"
                            >
                              {HOLDER_TYPES.map((ht) => (
                                <option key={ht.value} value={ht.value}>
                                  {ht.label}
                                </option>
                              ))}
                            </select>
                          </div>
                          {NEEDS_VALUE.has(grantForm.holder_type) && (
                            <div>
                              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                                Holder Value *
                              </label>
                              <input
                                type="text"
                                value={grantForm.holder_value}
                                onChange={(e) => setGrantForm({ ...grantForm, holder_value: e.target.value })}
                                className="input w-full"
                                placeholder={
                                  grantForm.holder_type === 'user'
                                    ? 'user@example.com'
                                    : grantForm.holder_type === 'group'
                                      ? 'group-name'
                                      : 'role-name'
                                }
                                required
                              />
                            </div>
                          )}
                        </div>
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => setAddingGrantTo(null)}
                            className="btn"
                          >
                            Cancel
                          </button>
                          <button
                            type="submit"
                            disabled={addGrantMutation.isPending}
                            className="btn-primary"
                          >
                            {addGrantMutation.isPending ? 'Adding...' : 'Add Grant'}
                          </button>
                        </div>
                      </form>
                    )}

                    {/* Grants List - Grouped by Permission */}
                    {grants.length === 0 ? (
                      <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
                        No permission grants defined for this scheme
                      </p>
                    ) : (
                      <div className="space-y-3">
                        {groupedGrants.map(([permission, permGrants]) => (
                          <div key={permission} className="rounded-lg border border-gray-200 dark:border-gray-700">
                            <div className="flex items-center gap-2 px-3 py-2 bg-gray-50 dark:bg-gray-800/50 rounded-t-lg border-b border-gray-200 dark:border-gray-700">
                              <Key size={14} className="text-gray-500 dark:text-gray-400 shrink-0" />
                              <span className="font-medium text-sm text-gray-900 dark:text-gray-100">
                                {formatPermission(permission)}
                              </span>
                              <span className="text-xs text-gray-400">
                                ({permGrants.length})
                              </span>
                            </div>
                            <div className="divide-y divide-gray-100 dark:divide-gray-800">
                              {permGrants.map((grant) => (
                                <div
                                  key={grant.id}
                                  className="flex items-center justify-between px-3 py-2"
                                >
                                  <div className="flex items-center gap-2 min-w-0">
                                    <span
                                      className={cn(
                                        'px-2 py-0.5 text-xs font-medium rounded shrink-0',
                                        HOLDER_TYPE_COLORS[grant.holder_type] || HOLDER_TYPE_COLORS.authenticated
                                      )}
                                    >
                                      {formatHolderType(grant.holder_type)}
                                    </span>
                                    {grant.holder_value && (
                                      <span className="text-sm text-gray-600 dark:text-gray-300 truncate flex items-center gap-1">
                                        <Users size={12} className="shrink-0" />
                                        {grant.holder_value}
                                      </span>
                                    )}
                                  </div>
                                  <button
                                    onClick={() => handleRemoveGrant(scheme.id, grant.id)}
                                    className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30 shrink-0"
                                    title="Remove grant"
                                  >
                                    <Trash2 size={14} />
                                  </button>
                                </div>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
