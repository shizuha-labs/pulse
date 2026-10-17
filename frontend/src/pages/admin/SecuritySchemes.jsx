import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Shield, Lock, Plus, Edit2, Trash2, ChevronDown, ChevronRight, Users } from 'lucide-react'
import { securitySchemesApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner from '../../components/LoadingSpinner'

const LEVEL_COLORS = [
  'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
  'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400',
  'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
]

function getLevelColor(position) {
  if (position >= LEVEL_COLORS.length) return LEVEL_COLORS[LEVEL_COLORS.length - 1]
  return LEVEL_COLORS[position]
}

export default function SecuritySchemes() {
  const queryClient = useQueryClient()
  const [expandedSchemes, setExpandedSchemes] = useState({})
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [editingScheme, setEditingScheme] = useState(null)
  const [addingLevelTo, setAddingLevelTo] = useState(null)
  const [schemeForm, setSchemeForm] = useState({ name: '', slug: '', description: '' })
  const [levelForm, setLevelForm] = useState({ name: '', slug: '', description: '', position: 0, members: '' })

  const { data, isLoading, isError } = useQuery({
    queryKey: ['securitySchemes'],
    queryFn: () => securitySchemesApi.list().then((res) => res.data),
  })

  const schemes = data?.results || data || []

  const createMutation = useMutation({
    mutationFn: (data) => securitySchemesApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['securitySchemes'] })
      setShowCreateForm(false)
      setSchemeForm({ name: '', slug: '', description: '' })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => securitySchemesApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['securitySchemes'] })
      setEditingScheme(null)
      setSchemeForm({ name: '', slug: '', description: '' })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => securitySchemesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['securitySchemes'] })
    },
  })

  const addLevelMutation = useMutation({
    mutationFn: ({ schemeId, ...data }) => securitySchemesApi.addLevel(schemeId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['securitySchemes'] })
      setAddingLevelTo(null)
      setLevelForm({ name: '', slug: '', description: '', position: 0, members: '' })
    },
  })

  const removeLevelMutation = useMutation({
    mutationFn: ({ schemeId, levelId }) => securitySchemesApi.removeLevel(schemeId, { level_id: levelId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['securitySchemes'] })
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
    if (confirm(`Delete security scheme "${scheme.name}"? This cannot be undone.`)) {
      deleteMutation.mutate(scheme.id)
    }
  }

  const handleAddLevel = (e) => {
    e.preventDefault()
    const members = levelForm.members
      .split(',')
      .map((m) => m.trim())
      .filter(Boolean)
    addLevelMutation.mutate({
      schemeId: addingLevelTo,
      name: levelForm.name,
      slug: levelForm.slug,
      description: levelForm.description,
      position: parseInt(levelForm.position, 10) || 0,
      members,
    })
  }

  const handleRemoveLevel = (schemeId, levelId, levelName) => {
    if (confirm(`Remove security level "${levelName}"?`)) {
      removeLevelMutation.mutate({ schemeId, levelId })
    }
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
        Failed to load security schemes. Please try again.
      </div>
    )
  }

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <Shield size={24} />
            Security Schemes
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            Manage security schemes and their access levels
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
            {editingScheme ? 'Edit Scheme' : 'Create Security Scheme'}
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
                placeholder="e.g., Default Security Scheme"
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
                placeholder="e.g., default-security"
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
              placeholder="Describe this security scheme"
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
          <Shield size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-4" />
          <p className="text-gray-500 dark:text-gray-400">No security schemes defined yet</p>
          <p className="text-sm text-gray-400 dark:text-gray-500 mt-1">
            Create a scheme to define access levels for your projects
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {schemes.map((scheme) => {
            const isExpanded = expandedSchemes[scheme.id]
            const levels = scheme.levels || []

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
                        <Lock size={16} className="text-gray-500 dark:text-gray-400 shrink-0" />
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
                      {levels.length} level{levels.length !== 1 ? 's' : ''}
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

                {/* Expanded: Levels */}
                {isExpanded && (
                  <div className="border-t border-gray-200 dark:border-gray-700 px-4 pb-4">
                    {/* Add Level Button */}
                    <div className="flex justify-end mt-3 mb-2">
                      <button
                        onClick={() => {
                          setAddingLevelTo(addingLevelTo === scheme.id ? null : scheme.id)
                          setLevelForm({ name: '', slug: '', description: '', position: levels.length, members: '' })
                        }}
                        className="btn flex items-center gap-1 text-sm"
                      >
                        <Plus size={16} />
                        Add Level
                      </button>
                    </div>

                    {/* Add Level Form */}
                    {addingLevelTo === scheme.id && (
                      <form
                        onSubmit={handleAddLevel}
                        className="mb-3 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3"
                      >
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Name *
                            </label>
                            <input
                              type="text"
                              value={levelForm.name}
                              onChange={(e) => setLevelForm({ ...levelForm, name: e.target.value })}
                              className="input w-full"
                              placeholder="e.g., Confidential"
                              required
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Slug *
                            </label>
                            <input
                              type="text"
                              value={levelForm.slug}
                              onChange={(e) =>
                                setLevelForm({
                                  ...levelForm,
                                  slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '-'),
                                })
                              }
                              className="input w-full"
                              placeholder="e.g., confidential"
                              required
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Position
                            </label>
                            <input
                              type="number"
                              value={levelForm.position}
                              onChange={(e) => setLevelForm({ ...levelForm, position: e.target.value })}
                              className="input w-full"
                              min={0}
                            />
                            <p className="text-xs text-gray-400 mt-0.5">0 = most restricted</p>
                          </div>
                        </div>
                        <div>
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            Description
                          </label>
                          <input
                            type="text"
                            value={levelForm.description}
                            onChange={(e) => setLevelForm({ ...levelForm, description: e.target.value })}
                            className="input w-full"
                            placeholder="Description for this level"
                          />
                        </div>
                        <div>
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            Members
                          </label>
                          <input
                            type="text"
                            value={levelForm.members}
                            onChange={(e) => setLevelForm({ ...levelForm, members: e.target.value })}
                            className="input w-full"
                            placeholder="user1@example.com, user2@example.com"
                          />
                          <p className="text-xs text-gray-400 mt-0.5">Comma-separated list of member identifiers</p>
                        </div>
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => setAddingLevelTo(null)}
                            className="btn"
                          >
                            Cancel
                          </button>
                          <button
                            type="submit"
                            disabled={addLevelMutation.isPending}
                            className="btn-primary"
                          >
                            {addLevelMutation.isPending ? 'Adding...' : 'Add Level'}
                          </button>
                        </div>
                      </form>
                    )}

                    {/* Levels List */}
                    {levels.length === 0 ? (
                      <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
                        No security levels defined for this scheme
                      </p>
                    ) : (
                      <div className="space-y-2">
                        {levels
                          .slice()
                          .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
                          .map((level) => (
                            <div
                              key={level.id}
                              className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-700"
                            >
                              <div className="flex items-center gap-3 min-w-0">
                                <span
                                  className={cn(
                                    'px-2 py-0.5 text-xs font-medium rounded shrink-0',
                                    getLevelColor(level.position ?? 0)
                                  )}
                                >
                                  {level.position ?? 0}
                                </span>
                                <div className="min-w-0">
                                  <p className="font-medium text-gray-900 dark:text-gray-100 text-sm">
                                    {level.name}
                                  </p>
                                  {level.description && (
                                    <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                                      {level.description}
                                    </p>
                                  )}
                                  {level.members && level.members.length > 0 && (
                                    <div className="flex items-center gap-1 mt-1">
                                      <Users size={12} className="text-gray-400 shrink-0" />
                                      <span className="text-xs text-gray-500 dark:text-gray-400 truncate">
                                        {level.members.join(', ')}
                                      </span>
                                    </div>
                                  )}
                                </div>
                              </div>
                              <button
                                onClick={() => handleRemoveLevel(scheme.id, level.id, level.name)}
                                className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30 shrink-0"
                                title="Remove level"
                              >
                                <Trash2 size={14} />
                              </button>
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
