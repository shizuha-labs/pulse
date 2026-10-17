import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Plus, Trash2, Edit2, ChevronDown, ChevronRight, X, Check,
  Layers, ListTree, Tag, AlertCircle,
} from 'lucide-react'
import { issueTypesApi, issueTypeSchemesApi } from '../../services/api'
import { cn } from '../../utils/cn'
import { PageLoader } from '../../components/LoadingSpinner'
import EmptyState from '../../components/EmptyState'

const HIERARCHY_LEVELS = [
  { value: 0, label: 'Epic', color: 'purple' },
  { value: 1, label: 'Story / Bug', color: 'blue' },
  { value: 2, label: 'Task', color: 'green' },
  { value: 3, label: 'Sub-task', color: 'orange' },
]

function hierarchyBadge(level) {
  const entry = HIERARCHY_LEVELS.find((h) => h.value === level)
  if (!entry) return null
  const colorMap = {
    purple: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
    blue: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
    green: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
    orange: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300',
  }
  return (
    <span className={cn('px-2.5 py-0.5 rounded-full text-xs font-medium', colorMap[entry.color])}>
      {entry.label}
    </span>
  )
}

function slugify(str) {
  return str
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

const EMPTY_TYPE_FORM = {
  name: '',
  slug: '',
  item_type_prefix: '',
  hierarchy_level: 1,
  icon: '',
  color: '#3b82f6',
  is_subtask: false,
  description: '',
}

const EMPTY_SCHEME_FORM = {
  name: '',
  slug: '',
  description: '',
}

// ---------------------------------------------------------------------------
// Issue Type Form (inline)
// ---------------------------------------------------------------------------
function IssueTypeForm({ initial, onSubmit, onCancel, isSubmitting }) {
  const [form, setForm] = useState(initial)
  const [slugTouched, setSlugTouched] = useState(!!initial.slug)

  const set = (field, value) => {
    setForm((prev) => {
      const next = { ...prev, [field]: value }
      if (field === 'name' && !slugTouched) {
        next.slug = slugify(value)
      }
      if (field === 'hierarchy_level') {
        next.is_subtask = Number(value) === 3
      }
      return next
    })
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit({ ...form, hierarchy_level: Number(form.hierarchy_level) })
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 p-4 border border-gray-200 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-800/50">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {/* Name */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Name <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            required
            value={form.name}
            onChange={(e) => set('name', e.target.value)}
            className="input w-full"
            placeholder="e.g. Bug"
          />
        </div>

        {/* Slug */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Slug</label>
          <input
            type="text"
            value={form.slug}
            onChange={(e) => { setSlugTouched(true); set('slug', e.target.value) }}
            className="input w-full"
            placeholder="auto-generated"
          />
        </div>

        {/* Item Type Prefix */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Item Type Prefix</label>
          <input
            type="text"
            value={form.item_type_prefix}
            onChange={(e) => set('item_type_prefix', e.target.value)}
            className="input w-full"
            placeholder="e.g. task.spike"
          />
        </div>

        {/* Hierarchy Level */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Hierarchy Level</label>
          <select
            value={form.hierarchy_level}
            onChange={(e) => set('hierarchy_level', e.target.value)}
            className="input w-full"
          >
            {HIERARCHY_LEVELS.map((h) => (
              <option key={h.value} value={h.value}>
                {h.value} &ndash; {h.label}
              </option>
            ))}
          </select>
        </div>

        {/* Icon */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Icon</label>
          <input
            type="text"
            value={form.icon}
            onChange={(e) => set('icon', e.target.value)}
            className="input w-full"
            placeholder="e.g. bug, check-circle"
          />
        </div>

        {/* Color */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Color</label>
          <div className="flex items-center gap-2">
            <input
              type="color"
              value={form.color}
              onChange={(e) => set('color', e.target.value)}
              className="w-10 h-10 rounded border border-gray-300 dark:border-gray-600 cursor-pointer p-0.5"
            />
            <input
              type="text"
              value={form.color}
              onChange={(e) => set('color', e.target.value)}
              className="input flex-1"
              placeholder="#3b82f6"
            />
          </div>
        </div>
      </div>

      {/* Is Subtask */}
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={form.is_subtask}
          onChange={(e) => set('is_subtask', e.target.checked)}
          className="rounded border-gray-300 dark:border-gray-600 text-brand-600 focus:ring-brand-500"
        />
        <span className="text-sm text-gray-700 dark:text-gray-300">Is sub-task type</span>
      </label>

      {/* Description */}
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Description</label>
        <textarea
          value={form.description}
          onChange={(e) => set('description', e.target.value)}
          className="input w-full"
          rows={2}
          placeholder="Optional description..."
        />
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 pt-1">
        <button type="submit" disabled={isSubmitting || !form.name} className="btn-primary">
          {isSubmitting ? 'Saving...' : 'Save'}
        </button>
        <button type="button" onClick={onCancel} className="btn">
          Cancel
        </button>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// Scheme Form (inline)
// ---------------------------------------------------------------------------
function SchemeForm({ initial, onSubmit, onCancel, isSubmitting }) {
  const [form, setForm] = useState(initial)
  const [slugTouched, setSlugTouched] = useState(!!initial.slug)

  const set = (field, value) => {
    setForm((prev) => {
      const next = { ...prev, [field]: value }
      if (field === 'name' && !slugTouched) {
        next.slug = slugify(value)
      }
      return next
    })
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit(form)
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 p-4 border border-gray-200 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-800/50">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Name <span className="text-red-500">*</span>
          </label>
          <input
            type="text"
            required
            value={form.name}
            onChange={(e) => set('name', e.target.value)}
            className="input w-full"
            placeholder="e.g. Default Scheme"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Slug</label>
          <input
            type="text"
            value={form.slug}
            onChange={(e) => { setSlugTouched(true); set('slug', e.target.value) }}
            className="input w-full"
            placeholder="auto-generated"
          />
        </div>
      </div>
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Description</label>
        <textarea
          value={form.description}
          onChange={(e) => set('description', e.target.value)}
          className="input w-full"
          rows={2}
          placeholder="Optional description..."
        />
      </div>
      <div className="flex items-center gap-2 pt-1">
        <button type="submit" disabled={isSubmitting || !form.name} className="btn-primary">
          {isSubmitting ? 'Saving...' : 'Save'}
        </button>
        <button type="button" onClick={onCancel} className="btn">
          Cancel
        </button>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// Add Mapping Form
// ---------------------------------------------------------------------------
function AddMappingForm({ issueTypes, existingMappings, onSubmit, onCancel, isSubmitting }) {
  const [issueTypeId, setIssueTypeId] = useState('')
  const [position, setPosition] = useState(0)

  const mappedIds = new Set((existingMappings || []).map((m) => m.issue_type?.id || m.issue_type))
  const available = (issueTypes || []).filter((t) => !mappedIds.has(t.id))

  const handleSubmit = (e) => {
    e.preventDefault()
    if (!issueTypeId) return
    onSubmit({ issue_type: issueTypeId, position: Number(position) })
  }

  return (
    <form onSubmit={handleSubmit} className="flex items-end gap-3 p-3 border border-dashed border-gray-300 dark:border-gray-600 rounded-lg bg-gray-50 dark:bg-gray-800/40">
      <div className="flex-1">
        <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Issue Type</label>
        <select
          value={issueTypeId}
          onChange={(e) => setIssueTypeId(e.target.value)}
          className="input w-full"
          required
        >
          <option value="">Select issue type...</option>
          {available.map((t) => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </select>
      </div>
      <div className="w-24">
        <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Position</label>
        <input
          type="number"
          min={0}
          value={position}
          onChange={(e) => setPosition(e.target.value)}
          className="input w-full"
        />
      </div>
      <button type="submit" disabled={isSubmitting || !issueTypeId} className="btn-primary whitespace-nowrap">
        {isSubmitting ? 'Adding...' : 'Add'}
      </button>
      <button type="button" onClick={onCancel} className="btn-icon">
        <X size={16} />
      </button>
    </form>
  )
}

// ===========================================================================
// Main Component
// ===========================================================================
export default function IssueTypes() {
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState('types')

  // ---- Issue Types state ----
  const [showCreateType, setShowCreateType] = useState(false)
  const [editingTypeId, setEditingTypeId] = useState(null)
  const [deletingTypeId, setDeletingTypeId] = useState(null)
  const [typeError, setTypeError] = useState(null)

  // ---- Schemes state ----
  const [showCreateScheme, setShowCreateScheme] = useState(false)
  const [editingSchemeId, setEditingSchemeId] = useState(null)
  const [deletingSchemeId, setDeletingSchemeId] = useState(null)
  const [expandedSchemeId, setExpandedSchemeId] = useState(null)
  const [addingMappingSchemeId, setAddingMappingSchemeId] = useState(null)
  const [schemeError, setSchemeError] = useState(null)

  // ---- Queries ----
  const { data: issueTypes, isLoading: typesLoading } = useQuery({
    queryKey: ['issueTypes'],
    queryFn: () => issueTypesApi.list().then((r) => {
      const d = r.data
      return d.results || d
    }),
  })

  const { data: schemes, isLoading: schemesLoading } = useQuery({
    queryKey: ['issueTypeSchemes'],
    queryFn: () => issueTypeSchemesApi.list().then((r) => {
      const d = r.data
      return d.results || d
    }),
  })

  // ---- Issue Type Mutations ----
  const createTypeMutation = useMutation({
    mutationFn: (data) => issueTypesApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issueTypes'] })
      setShowCreateType(false)
      setTypeError(null)
    },
    onError: (err) => {
      setTypeError(err.response?.data?.detail || err.response?.data?.name?.[0] || 'Failed to create issue type')
    },
  })

  const updateTypeMutation = useMutation({
    mutationFn: ({ id, data }) => issueTypesApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issueTypes'] })
      setEditingTypeId(null)
      setTypeError(null)
    },
    onError: (err) => {
      setTypeError(err.response?.data?.detail || err.response?.data?.name?.[0] || 'Failed to update issue type')
    },
  })

  const deleteTypeMutation = useMutation({
    mutationFn: (id) => issueTypesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issueTypes'] })
      setDeletingTypeId(null)
      setTypeError(null)
    },
    onError: (err) => {
      setTypeError(err.response?.data?.detail || 'Failed to delete issue type')
    },
  })

  // ---- Scheme Mutations ----
  const createSchemeMutation = useMutation({
    mutationFn: (data) => issueTypeSchemesApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issueTypeSchemes'] })
      setShowCreateScheme(false)
      setSchemeError(null)
    },
    onError: (err) => {
      setSchemeError(err.response?.data?.detail || err.response?.data?.name?.[0] || 'Failed to create scheme')
    },
  })

  const updateSchemeMutation = useMutation({
    mutationFn: ({ id, data }) => issueTypeSchemesApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issueTypeSchemes'] })
      setEditingSchemeId(null)
      setSchemeError(null)
    },
    onError: (err) => {
      setSchemeError(err.response?.data?.detail || err.response?.data?.name?.[0] || 'Failed to update scheme')
    },
  })

  const deleteSchemeMutation = useMutation({
    mutationFn: (id) => issueTypeSchemesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issueTypeSchemes'] })
      setDeletingSchemeId(null)
      setSchemeError(null)
    },
    onError: (err) => {
      setSchemeError(err.response?.data?.detail || 'Failed to delete scheme')
    },
  })

  const addMappingMutation = useMutation({
    mutationFn: ({ schemeId, data }) => issueTypeSchemesApi.addMapping(schemeId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issueTypeSchemes'] })
      setAddingMappingSchemeId(null)
    },
    onError: (err) => {
      setSchemeError(err.response?.data?.detail || 'Failed to add mapping')
    },
  })

  const removeMappingMutation = useMutation({
    mutationFn: ({ schemeId, data }) => issueTypeSchemesApi.removeMapping(schemeId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issueTypeSchemes'] })
    },
    onError: (err) => {
      setSchemeError(err.response?.data?.detail || 'Failed to remove mapping')
    },
  })

  // ---- Loading ----
  if (typesLoading || schemesLoading) {
    return <PageLoader />
  }

  const types = issueTypes || []
  const schemesList = schemes || []

  // ---- Render ----
  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Issue Types</h1>
        <p className="text-gray-500 dark:text-gray-400">Manage issue types and schemes</p>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200 dark:border-gray-700">
        <nav className="flex space-x-6">
          <button
            onClick={() => setActiveTab('types')}
            className={cn(
              'px-1 py-3 text-sm font-medium border-b-2 whitespace-nowrap transition-colors',
              activeTab === 'types'
                ? 'border-brand-500 text-brand-600 dark:text-brand-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <span className="flex items-center gap-1.5">
              <Tag size={15} />
              Issue Types
              {types.length > 0 && (
                <span className="ml-1 px-1.5 py-0.5 rounded-full text-xs bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">
                  {types.length}
                </span>
              )}
            </span>
          </button>
          <button
            onClick={() => setActiveTab('schemes')}
            className={cn(
              'px-1 py-3 text-sm font-medium border-b-2 whitespace-nowrap transition-colors',
              activeTab === 'schemes'
                ? 'border-brand-500 text-brand-600 dark:text-brand-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <span className="flex items-center gap-1.5">
              <Layers size={15} />
              Issue Type Schemes
              {schemesList.length > 0 && (
                <span className="ml-1 px-1.5 py-0.5 rounded-full text-xs bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">
                  {schemesList.length}
                </span>
              )}
            </span>
          </button>
        </nav>
      </div>

      {/* Error banners */}
      {activeTab === 'types' && typeError && (
        <div className="flex items-center gap-2 p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 text-sm">
          <AlertCircle size={16} className="shrink-0" />
          <span>{typeError}</span>
          <button onClick={() => setTypeError(null)} className="ml-auto">
            <X size={14} />
          </button>
        </div>
      )}
      {activeTab === 'schemes' && schemeError && (
        <div className="flex items-center gap-2 p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 text-sm">
          <AlertCircle size={16} className="shrink-0" />
          <span>{schemeError}</span>
          <button onClick={() => setSchemeError(null)} className="ml-auto">
            <X size={14} />
          </button>
        </div>
      )}

      {/* ================================================================= */}
      {/* TAB 1: Issue Types                                                */}
      {/* ================================================================= */}
      {activeTab === 'types' && (
        <div className="space-y-4">
          {/* Create button */}
          {!showCreateType && (
            <button onClick={() => setShowCreateType(true)} className="btn-primary">
              <Plus size={16} className="mr-1.5" />
              Create Issue Type
            </button>
          )}

          {/* Create form */}
          {showCreateType && (
            <IssueTypeForm
              initial={EMPTY_TYPE_FORM}
              onSubmit={(data) => createTypeMutation.mutate(data)}
              onCancel={() => { setShowCreateType(false); setTypeError(null) }}
              isSubmitting={createTypeMutation.isPending}
            />
          )}

          {/* Issue Types list */}
          {types.length === 0 && !showCreateType ? (
            <EmptyState
              icon={Tag}
              title="No issue types"
              description="Create your first issue type to categorize work items."
              action={
                <button onClick={() => setShowCreateType(true)} className="btn-primary">
                  <Plus size={16} className="mr-1.5" />
                  Create Issue Type
                </button>
              }
            />
          ) : (
            <div className="card divide-y divide-gray-100 dark:divide-gray-800">
              {types.map((type) => (
                <div key={type.id}>
                  {editingTypeId === type.id ? (
                    <div className="p-4">
                      <IssueTypeForm
                        initial={{
                          name: type.name || '',
                          slug: type.slug || '',
                          item_type_prefix: type.item_type_prefix || '',
                          hierarchy_level: type.hierarchy_level ?? 1,
                          icon: type.icon || '',
                          color: type.color || '#3b82f6',
                          is_subtask: type.is_subtask || false,
                          description: type.description || '',
                        }}
                        onSubmit={(data) => updateTypeMutation.mutate({ id: type.id, data })}
                        onCancel={() => { setEditingTypeId(null); setTypeError(null) }}
                        isSubmitting={updateTypeMutation.isPending}
                      />
                    </div>
                  ) : (
                    <div className="flex items-center justify-between p-4 group">
                      <div className="flex items-center gap-3 min-w-0">
                        {/* Color dot */}
                        <div
                          className="w-4 h-4 rounded-full shrink-0 ring-1 ring-black/10 dark:ring-white/10"
                          style={{ backgroundColor: type.color || '#6b7280' }}
                        />
                        {/* Name and meta */}
                        <div className="min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-medium text-gray-900 dark:text-gray-100">{type.name}</span>
                            <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">{type.slug}</span>
                            {type.icon && (
                              <span className="text-xs text-gray-400 dark:text-gray-500">{type.icon}</span>
                            )}
                          </div>
                          <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                            {hierarchyBadge(type.hierarchy_level)}
                            {type.is_subtask && (
                              <span className="px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                                Sub-task
                              </span>
                            )}
                            {type.item_type_prefix && (
                              <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">
                                {type.item_type_prefix}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>

                      {/* Actions */}
                      <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        <button
                          onClick={() => { setEditingTypeId(type.id); setTypeError(null) }}
                          className="btn-icon"
                          title="Edit"
                        >
                          <Edit2 size={15} />
                        </button>
                        {deletingTypeId === type.id ? (
                          <div className="flex items-center gap-1">
                            <span className="text-xs text-red-600 dark:text-red-400 mr-1">Delete?</span>
                            <button
                              onClick={() => deleteTypeMutation.mutate(type.id)}
                              disabled={deleteTypeMutation.isPending}
                              className="btn-icon text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20"
                              title="Confirm delete"
                            >
                              <Check size={15} />
                            </button>
                            <button
                              onClick={() => setDeletingTypeId(null)}
                              className="btn-icon"
                              title="Cancel"
                            >
                              <X size={15} />
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => setDeletingTypeId(type.id)}
                            className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20"
                            title="Delete"
                          >
                            <Trash2 size={15} />
                          </button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ================================================================= */}
      {/* TAB 2: Issue Type Schemes                                         */}
      {/* ================================================================= */}
      {activeTab === 'schemes' && (
        <div className="space-y-4">
          {/* Create button */}
          {!showCreateScheme && (
            <button onClick={() => setShowCreateScheme(true)} className="btn-primary">
              <Plus size={16} className="mr-1.5" />
              Create Scheme
            </button>
          )}

          {/* Create form */}
          {showCreateScheme && (
            <SchemeForm
              initial={EMPTY_SCHEME_FORM}
              onSubmit={(data) => createSchemeMutation.mutate(data)}
              onCancel={() => { setShowCreateScheme(false); setSchemeError(null) }}
              isSubmitting={createSchemeMutation.isPending}
            />
          )}

          {/* Schemes list */}
          {schemesList.length === 0 && !showCreateScheme ? (
            <EmptyState
              icon={Layers}
              title="No issue type schemes"
              description="Create a scheme to group issue types for projects."
              action={
                <button onClick={() => setShowCreateScheme(true)} className="btn-primary">
                  <Plus size={16} className="mr-1.5" />
                  Create Scheme
                </button>
              }
            />
          ) : (
            <div className="space-y-3">
              {schemesList.map((scheme) => {
                const isExpanded = expandedSchemeId === scheme.id
                const mappings = scheme.mappings || scheme.issue_type_mappings || []

                return (
                  <div key={scheme.id} className="card overflow-hidden">
                    {editingSchemeId === scheme.id ? (
                      <div className="p-4">
                        <SchemeForm
                          initial={{
                            name: scheme.name || '',
                            slug: scheme.slug || '',
                            description: scheme.description || '',
                          }}
                          onSubmit={(data) => updateSchemeMutation.mutate({ id: scheme.id, data })}
                          onCancel={() => { setEditingSchemeId(null); setSchemeError(null) }}
                          isSubmitting={updateSchemeMutation.isPending}
                        />
                      </div>
                    ) : (
                      <>
                        {/* Scheme header */}
                        <div
                          className="flex items-center justify-between p-4 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
                          onClick={() => setExpandedSchemeId(isExpanded ? null : scheme.id)}
                        >
                          <div className="flex items-center gap-3 min-w-0">
                            {isExpanded ? <ChevronDown size={16} className="text-gray-400 shrink-0" /> : <ChevronRight size={16} className="text-gray-400 shrink-0" />}
                            <div className="min-w-0">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="font-medium text-gray-900 dark:text-gray-100">{scheme.name}</span>
                                <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">{scheme.slug}</span>
                              </div>
                              <div className="flex items-center gap-3 mt-0.5 text-sm text-gray-500 dark:text-gray-400">
                                {scheme.description && (
                                  <span className="truncate max-w-xs">{scheme.description}</span>
                                )}
                                {scheme.default_issue_type_name && (
                                  <span>Default: <span className="font-medium text-gray-700 dark:text-gray-300">{scheme.default_issue_type_name}</span></span>
                                )}
                                <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400">
                                  {mappings.length} {mappings.length === 1 ? 'type' : 'types'}
                                </span>
                              </div>
                            </div>
                          </div>

                          {/* Actions (stop propagation to prevent toggle) */}
                          <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                            <button
                              onClick={() => { setEditingSchemeId(scheme.id); setSchemeError(null) }}
                              className="btn-icon"
                              title="Edit scheme"
                            >
                              <Edit2 size={15} />
                            </button>
                            {deletingSchemeId === scheme.id ? (
                              <div className="flex items-center gap-1">
                                <span className="text-xs text-red-600 dark:text-red-400 mr-1">Delete?</span>
                                <button
                                  onClick={() => deleteSchemeMutation.mutate(scheme.id)}
                                  disabled={deleteSchemeMutation.isPending}
                                  className="btn-icon text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20"
                                  title="Confirm delete"
                                >
                                  <Check size={15} />
                                </button>
                                <button
                                  onClick={() => setDeletingSchemeId(null)}
                                  className="btn-icon"
                                  title="Cancel"
                                >
                                  <X size={15} />
                                </button>
                              </div>
                            ) : (
                              <button
                                onClick={() => setDeletingSchemeId(scheme.id)}
                                className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20"
                                title="Delete scheme"
                              >
                                <Trash2 size={15} />
                              </button>
                            )}
                          </div>
                        </div>

                        {/* Expanded: mappings */}
                        {isExpanded && (
                          <div className="border-t border-gray-100 dark:border-gray-800">
                            {mappings.length > 0 ? (
                              <div className="divide-y divide-gray-50 dark:divide-gray-800/50">
                                {mappings.map((mapping, idx) => {
                                  const mappingType = mapping.issue_type_detail || mapping.issue_type_name
                                    ? { name: mapping.issue_type_name || mapping.issue_type_detail?.name, color: mapping.issue_type_detail?.color }
                                    : types.find((t) => t.id === (mapping.issue_type?.id || mapping.issue_type))
                                  const mappingId = mapping.id || mapping.issue_type?.id || mapping.issue_type

                                  return (
                                    <div key={mappingId || idx} className="flex items-center justify-between px-4 py-2.5 pl-12">
                                      <div className="flex items-center gap-2.5">
                                        <span className="text-xs text-gray-400 dark:text-gray-500 font-mono w-6 text-right">
                                          {mapping.position ?? idx}
                                        </span>
                                        {mappingType?.color && (
                                          <div
                                            className="w-3 h-3 rounded-full shrink-0 ring-1 ring-black/10 dark:ring-white/10"
                                            style={{ backgroundColor: mappingType.color }}
                                          />
                                        )}
                                        <span className="text-sm text-gray-900 dark:text-gray-100">
                                          {mappingType?.name || 'Unknown type'}
                                        </span>
                                      </div>
                                      <button
                                        onClick={() => removeMappingMutation.mutate({
                                          schemeId: scheme.id,
                                          data: { issue_type: mapping.issue_type?.id || mapping.issue_type || mappingId },
                                        })}
                                        disabled={removeMappingMutation.isPending}
                                        className="btn-icon text-red-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 opacity-0 group-hover:opacity-100"
                                        title="Remove from scheme"
                                      >
                                        <X size={14} />
                                      </button>
                                    </div>
                                  )
                                })}
                              </div>
                            ) : (
                              <p className="px-4 py-3 pl-12 text-sm text-gray-400 dark:text-gray-500">
                                No issue types in this scheme yet.
                              </p>
                            )}

                            {/* Add mapping form or button */}
                            <div className="px-4 py-3 pl-12">
                              {addingMappingSchemeId === scheme.id ? (
                                <AddMappingForm
                                  issueTypes={types}
                                  existingMappings={mappings}
                                  onSubmit={(data) => addMappingMutation.mutate({ schemeId: scheme.id, data })}
                                  onCancel={() => setAddingMappingSchemeId(null)}
                                  isSubmitting={addMappingMutation.isPending}
                                />
                              ) : (
                                <button
                                  onClick={() => setAddingMappingSchemeId(scheme.id)}
                                  className="btn text-sm"
                                >
                                  <Plus size={14} className="mr-1" />
                                  Add Issue Type
                                </button>
                              )}
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
