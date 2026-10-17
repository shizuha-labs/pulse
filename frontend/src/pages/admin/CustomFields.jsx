import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Plus, Edit2, Trash2, Settings, List, Type, Hash, Calendar,
  CheckSquare, Link, Mail, User, Users, Star, Palette, X,
} from 'lucide-react'
import { customFieldsApi, customFieldContextsApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner from '../../components/LoadingSpinner'

const FIELD_TYPE_OPTIONS = [
  'text', 'textarea', 'number', 'decimal', 'date', 'datetime',
  'checkbox', 'select', 'multiselect', 'radio', 'url', 'email',
  'user_picker', 'multi_user_picker', 'version_picker', 'component_picker',
  'label', 'cascading_select', 'color', 'rating',
]

const SELECT_TYPES = ['select', 'multiselect', 'radio', 'cascading_select']

const fieldTypeColors = {
  text: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  textarea: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  number: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  decimal: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  date: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  datetime: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  select: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  multiselect: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  radio: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  cascading_select: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  checkbox: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300',
  url: 'bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-300',
  email: 'bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-300',
  user_picker: 'bg-pink-100 text-pink-800 dark:bg-pink-900/30 dark:text-pink-300',
  multi_user_picker: 'bg-pink-100 text-pink-800 dark:bg-pink-900/30 dark:text-pink-300',
  version_picker: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300',
  component_picker: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300',
  label: 'bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300',
  color: 'bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-300',
  rating: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
}

function getFieldTypeColor(type) {
  return fieldTypeColors[type] || 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300'
}

function getFieldTypeIcon(type) {
  switch (type) {
    case 'text':
    case 'textarea':
      return Type
    case 'number':
    case 'decimal':
      return Hash
    case 'date':
    case 'datetime':
      return Calendar
    case 'checkbox':
      return CheckSquare
    case 'select':
    case 'multiselect':
    case 'radio':
    case 'cascading_select':
      return List
    case 'url':
      return Link
    case 'email':
      return Mail
    case 'user_picker':
      return User
    case 'multi_user_picker':
      return Users
    case 'color':
      return Palette
    case 'rating':
      return Star
    default:
      return Settings
  }
}

const EMPTY_FIELD_FORM = {
  name: '',
  field_type: 'text',
  description: '',
  default_value: '',
  is_required: false,
  options: '',
  validation_rules: '',
}

const EMPTY_CONTEXT_FORM = {
  custom_field: '',
  project: '',
  issue_type: '',
  is_required: false,
  default_value: '',
}

export default function CustomFields() {
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState('fields')

  // ----- Custom Fields state -----
  const [showFieldForm, setShowFieldForm] = useState(false)
  const [editingFieldId, setEditingFieldId] = useState(null)
  const [fieldForm, setFieldForm] = useState(EMPTY_FIELD_FORM)
  const [fieldError, setFieldError] = useState(null)

  // ----- Field Contexts state -----
  const [showContextForm, setShowContextForm] = useState(false)
  const [editingContextId, setEditingContextId] = useState(null)
  const [contextForm, setContextForm] = useState(EMPTY_CONTEXT_FORM)
  const [contextError, setContextError] = useState(null)

  // ----- Data fetching -----
  const { data: fields = [], isLoading: fieldsLoading } = useQuery({
    queryKey: ['customFields'],
    queryFn: () => customFieldsApi.list().then(r => r.data.results || r.data || []),
  })

  const { data: contexts = [], isLoading: contextsLoading } = useQuery({
    queryKey: ['customFieldContexts'],
    queryFn: () => customFieldContextsApi.list().then(r => r.data.results || r.data || []),
  })

  // ----- Field mutations -----
  const createFieldMutation = useMutation({
    mutationFn: (data) => customFieldsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customFields'] })
      resetFieldForm()
    },
    onError: (err) => {
      setFieldError(err.response?.data?.detail || err.response?.data?.name?.[0] || 'Failed to create field')
    },
  })

  const updateFieldMutation = useMutation({
    mutationFn: ({ id, ...data }) => customFieldsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customFields'] })
      resetFieldForm()
    },
    onError: (err) => {
      setFieldError(err.response?.data?.detail || 'Failed to update field')
    },
  })

  const deleteFieldMutation = useMutation({
    mutationFn: (id) => customFieldsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customFields'] })
      queryClient.invalidateQueries({ queryKey: ['customFieldContexts'] })
    },
  })

  // ----- Context mutations -----
  const createContextMutation = useMutation({
    mutationFn: (data) => customFieldContextsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customFieldContexts'] })
      resetContextForm()
    },
    onError: (err) => {
      setContextError(err.response?.data?.detail || 'Failed to create context')
    },
  })

  const updateContextMutation = useMutation({
    mutationFn: ({ id, ...data }) => customFieldContextsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customFieldContexts'] })
      resetContextForm()
    },
    onError: (err) => {
      setContextError(err.response?.data?.detail || 'Failed to update context')
    },
  })

  const deleteContextMutation = useMutation({
    mutationFn: (id) => customFieldContextsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['customFieldContexts'] })
    },
  })

  // ----- Field helpers -----
  function resetFieldForm() {
    setShowFieldForm(false)
    setEditingFieldId(null)
    setFieldForm(EMPTY_FIELD_FORM)
    setFieldError(null)
  }

  function startEditField(field) {
    setEditingFieldId(field.id)
    setFieldForm({
      name: field.name || '',
      field_type: field.field_type || 'text',
      description: field.description || '',
      default_value: field.default_value || '',
      is_required: field.is_required || false,
      options: field.options ? JSON.stringify(field.options, null, 2) : '',
      validation_rules: field.validation_rules ? JSON.stringify(field.validation_rules, null, 2) : '',
    })
    setFieldError(null)
    setShowFieldForm(true)
  }

  function handleFieldSubmit(e) {
    e.preventDefault()
    setFieldError(null)

    const payload = {
      name: fieldForm.name,
      field_type: fieldForm.field_type,
      description: fieldForm.description,
      default_value: fieldForm.default_value,
      is_required: fieldForm.is_required,
    }

    // Parse options JSON if provided
    if (fieldForm.options.trim()) {
      try {
        payload.options = JSON.parse(fieldForm.options)
      } catch {
        setFieldError('Options must be a valid JSON array')
        return
      }
    }

    // Parse validation_rules JSON if provided
    if (fieldForm.validation_rules.trim()) {
      try {
        payload.validation_rules = JSON.parse(fieldForm.validation_rules)
      } catch {
        setFieldError('Validation rules must be valid JSON')
        return
      }
    }

    if (editingFieldId) {
      updateFieldMutation.mutate({ id: editingFieldId, ...payload })
    } else {
      createFieldMutation.mutate(payload)
    }
  }

  // ----- Context helpers -----
  function resetContextForm() {
    setShowContextForm(false)
    setEditingContextId(null)
    setContextForm(EMPTY_CONTEXT_FORM)
    setContextError(null)
  }

  function startEditContext(ctx) {
    setEditingContextId(ctx.id)
    setContextForm({
      custom_field: ctx.custom_field?.id || ctx.custom_field || '',
      project: ctx.project?.id || ctx.project || '',
      issue_type: ctx.issue_type?.id || ctx.issue_type || '',
      is_required: ctx.is_required || false,
      default_value: ctx.default_value || '',
    })
    setContextError(null)
    setShowContextForm(true)
  }

  function handleContextSubmit(e) {
    e.preventDefault()
    setContextError(null)

    const payload = {
      custom_field: contextForm.custom_field,
      is_required: contextForm.is_required,
      default_value: contextForm.default_value,
    }

    if (contextForm.project) {
      payload.project = contextForm.project
    }
    if (contextForm.issue_type) {
      payload.issue_type = contextForm.issue_type
    }

    if (editingContextId) {
      updateContextMutation.mutate({ id: editingContextId, ...payload })
    } else {
      createContextMutation.mutate(payload)
    }
  }

  // ----- Helpers -----
  function getFieldName(fieldId) {
    const field = fields.find(f => f.id === fieldId || f.id === fieldId?.id)
    return field?.name || (typeof fieldId === 'object' ? fieldId?.name : fieldId) || 'Unknown'
  }

  function getContextFieldName(ctx) {
    if (ctx.custom_field_name) return ctx.custom_field_name
    if (typeof ctx.custom_field === 'object' && ctx.custom_field?.name) return ctx.custom_field.name
    return getFieldName(ctx.custom_field)
  }

  function getContextProjectName(ctx) {
    if (ctx.project_name) return ctx.project_name
    if (typeof ctx.project === 'object' && ctx.project?.name) return ctx.project.name
    return ctx.project ? String(ctx.project) : null
  }

  function getContextIssueTypeName(ctx) {
    if (ctx.issue_type_name) return ctx.issue_type_name
    if (typeof ctx.issue_type === 'object' && ctx.issue_type?.name) return ctx.issue_type.name
    return ctx.issue_type ? String(ctx.issue_type) : null
  }

  // ----- Loading -----
  if (fieldsLoading || contextsLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Custom Fields</h1>
        <p className="text-gray-500 dark:text-gray-400">Define custom fields and field contexts for your issues</p>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200 dark:border-gray-700">
        <nav className="flex space-x-6">
          {[
            { id: 'fields', label: 'Custom Fields', icon: Settings, count: fields.length },
            { id: 'contexts', label: 'Field Contexts', icon: List, count: contexts.length },
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
              <span className={cn(
                'text-xs px-1.5 py-0.5 rounded-full',
                activeTab === tab.id
                  ? 'bg-brand-100 text-brand-700 dark:bg-brand-900/30 dark:text-brand-300'
                  : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
              )}>
                {tab.count}
              </span>
            </button>
          ))}
        </nav>
      </div>

      {/* ==================== TAB 1: Custom Fields ==================== */}
      {activeTab === 'fields' && (
        <div className="space-y-4">
          {/* Create button */}
          <div className="flex justify-end">
            <button
              onClick={() => {
                resetFieldForm()
                setShowFieldForm(true)
              }}
              className="btn-primary flex items-center gap-2"
            >
              <Plus size={18} />
              Create Custom Field
            </button>
          </div>

          {/* Field form */}
          {showFieldForm && (
            <div className="card p-4">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                  {editingFieldId ? 'Edit Custom Field' : 'Create Custom Field'}
                </h2>
                <button onClick={resetFieldForm} className="btn-icon">
                  <X size={20} />
                </button>
              </div>

              {fieldError && (
                <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg">
                  {fieldError}
                </div>
              )}

              <form onSubmit={handleFieldSubmit} className="space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Name *
                    </label>
                    <input
                      type="text"
                      value={fieldForm.name}
                      onChange={(e) => setFieldForm({ ...fieldForm, name: e.target.value })}
                      className="input w-full"
                      placeholder="e.g., Story Points, Sprint Goal"
                      required
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Field Type *
                    </label>
                    <select
                      value={fieldForm.field_type}
                      onChange={(e) => setFieldForm({ ...fieldForm, field_type: e.target.value })}
                      className="input w-full"
                    >
                      {FIELD_TYPE_OPTIONS.map(type => (
                        <option key={type} value={type}>
                          {type.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Description
                  </label>
                  <textarea
                    value={fieldForm.description}
                    onChange={(e) => setFieldForm({ ...fieldForm, description: e.target.value })}
                    className="input w-full"
                    rows={2}
                    placeholder="Brief description of this custom field"
                  />
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Default Value
                    </label>
                    <input
                      type="text"
                      value={fieldForm.default_value}
                      onChange={(e) => setFieldForm({ ...fieldForm, default_value: e.target.value })}
                      className="input w-full"
                      placeholder="Default value (optional)"
                    />
                  </div>
                  <div className="flex items-center pt-6">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={fieldForm.is_required}
                        onChange={(e) => setFieldForm({ ...fieldForm, is_required: e.target.checked })}
                        className="w-4 h-4 rounded border-gray-300 dark:border-gray-600 text-brand-600 focus:ring-brand-500"
                      />
                      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                        Required field
                      </span>
                    </label>
                  </div>
                </div>

                {/* Options field - only for select-like types */}
                {SELECT_TYPES.includes(fieldForm.field_type) && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Options (JSON array)
                    </label>
                    <textarea
                      value={fieldForm.options}
                      onChange={(e) => setFieldForm({ ...fieldForm, options: e.target.value })}
                      className="input w-full font-mono text-sm"
                      rows={4}
                      placeholder='["Option 1", "Option 2", "Option 3"]'
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Provide a JSON array of option values
                    </p>
                  </div>
                )}

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Validation Rules (JSON)
                  </label>
                  <textarea
                    value={fieldForm.validation_rules}
                    onChange={(e) => setFieldForm({ ...fieldForm, validation_rules: e.target.value })}
                    className="input w-full font-mono text-sm"
                    rows={3}
                    placeholder='{"min": 0, "max": 100}'
                  />
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    Optional JSON object with validation constraints
                  </p>
                </div>

                <div className="flex justify-end gap-3 pt-2">
                  <button type="button" onClick={resetFieldForm} className="btn">
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={createFieldMutation.isPending || updateFieldMutation.isPending}
                    className="btn-primary"
                  >
                    {createFieldMutation.isPending || updateFieldMutation.isPending
                      ? 'Saving...'
                      : editingFieldId ? 'Update Field' : 'Create Field'}
                  </button>
                </div>
              </form>
            </div>
          )}

          {/* Fields list */}
          <div className="card">
            {fields.length === 0 ? (
              <div className="p-8 text-center">
                <Settings className="mx-auto mb-3 text-gray-400" size={40} />
                <p className="text-gray-500 dark:text-gray-400">No custom fields defined yet</p>
                <p className="text-sm text-gray-400 dark:text-gray-500 mt-1">
                  Create your first custom field to extend issue tracking
                </p>
              </div>
            ) : (
              <div className="divide-y divide-gray-200 dark:divide-gray-700">
                {fields.map((field) => {
                  const IconComponent = getFieldTypeIcon(field.field_type)
                  return (
                    <div key={field.id} className="p-4 flex items-start justify-between gap-4">
                      <div className="flex items-start gap-3 min-w-0 flex-1">
                        <div className="mt-0.5">
                          <IconComponent size={18} className="text-gray-400 dark:text-gray-500" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <p className="font-medium text-gray-900 dark:text-gray-100">
                              {field.name}
                            </p>
                            <span className={cn(
                              'px-2 py-0.5 text-xs rounded-full font-medium',
                              getFieldTypeColor(field.field_type)
                            )}>
                              {field.field_type.replace(/_/g, ' ')}
                            </span>
                            {field.is_required && (
                              <span className="px-2 py-0.5 text-xs rounded-full bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300">
                                required
                              </span>
                            )}
                            {field.is_system && (
                              <span className="px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400">
                                system
                              </span>
                            )}
                          </div>
                          {field.description && (
                            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                              {field.description}
                            </p>
                          )}
                        </div>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        <button
                          onClick={() => startEditField(field)}
                          className="btn-icon"
                          title="Edit field"
                        >
                          <Edit2 size={16} />
                        </button>
                        <button
                          onClick={() => {
                            if (confirm(`Delete custom field "${field.name}"? This cannot be undone.`)) {
                              deleteFieldMutation.mutate(field.id)
                            }
                          }}
                          className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                          title="Delete field"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ==================== TAB 2: Field Contexts ==================== */}
      {activeTab === 'contexts' && (
        <div className="space-y-4">
          {/* Create button */}
          <div className="flex justify-end">
            <button
              onClick={() => {
                resetContextForm()
                setShowContextForm(true)
              }}
              className="btn-primary flex items-center gap-2"
            >
              <Plus size={18} />
              Create Context
            </button>
          </div>

          {/* Context form */}
          {showContextForm && (
            <div className="card p-4">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                  {editingContextId ? 'Edit Field Context' : 'Create Field Context'}
                </h2>
                <button onClick={resetContextForm} className="btn-icon">
                  <X size={20} />
                </button>
              </div>

              {contextError && (
                <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg">
                  {contextError}
                </div>
              )}

              <form onSubmit={handleContextSubmit} className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Custom Field *
                  </label>
                  <select
                    value={contextForm.custom_field}
                    onChange={(e) => setContextForm({ ...contextForm, custom_field: e.target.value })}
                    className="input w-full"
                    required
                  >
                    <option value="">Select a custom field...</option>
                    {fields.map(field => (
                      <option key={field.id} value={field.id}>
                        {field.name} ({field.field_type.replace(/_/g, ' ')})
                      </option>
                    ))}
                  </select>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Project
                    </label>
                    <input
                      type="text"
                      value={contextForm.project}
                      onChange={(e) => setContextForm({ ...contextForm, project: e.target.value })}
                      className="input w-full"
                      placeholder="Project ID (leave empty for global)"
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Leave empty to apply to all projects
                    </p>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Issue Type
                    </label>
                    <input
                      type="text"
                      value={contextForm.issue_type}
                      onChange={(e) => setContextForm({ ...contextForm, issue_type: e.target.value })}
                      className="input w-full"
                      placeholder="Issue type ID (leave empty for all)"
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Leave empty to apply to all issue types
                    </p>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Default Value
                    </label>
                    <input
                      type="text"
                      value={contextForm.default_value}
                      onChange={(e) => setContextForm({ ...contextForm, default_value: e.target.value })}
                      className="input w-full"
                      placeholder="Context-specific default (optional)"
                    />
                  </div>
                  <div className="flex items-center pt-6">
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={contextForm.is_required}
                        onChange={(e) => setContextForm({ ...contextForm, is_required: e.target.checked })}
                        className="w-4 h-4 rounded border-gray-300 dark:border-gray-600 text-brand-600 focus:ring-brand-500"
                      />
                      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                        Required in this context
                      </span>
                    </label>
                  </div>
                </div>

                <div className="flex justify-end gap-3 pt-2">
                  <button type="button" onClick={resetContextForm} className="btn">
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={createContextMutation.isPending || updateContextMutation.isPending}
                    className="btn-primary"
                  >
                    {createContextMutation.isPending || updateContextMutation.isPending
                      ? 'Saving...'
                      : editingContextId ? 'Update Context' : 'Create Context'}
                  </button>
                </div>
              </form>
            </div>
          )}

          {/* Contexts list */}
          <div className="card">
            {contexts.length === 0 ? (
              <div className="p-8 text-center">
                <List className="mx-auto mb-3 text-gray-400" size={40} />
                <p className="text-gray-500 dark:text-gray-400">No field contexts defined yet</p>
                <p className="text-sm text-gray-400 dark:text-gray-500 mt-1">
                  Contexts control where and how custom fields appear
                </p>
              </div>
            ) : (
              <div className="divide-y divide-gray-200 dark:divide-gray-700">
                {contexts.map((ctx) => {
                  const projectName = getContextProjectName(ctx)
                  const issueTypeName = getContextIssueTypeName(ctx)
                  return (
                    <div key={ctx.id} className="p-4 flex items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <p className="font-medium text-gray-900 dark:text-gray-100">
                            {getContextFieldName(ctx)}
                          </p>
                          {ctx.is_required && (
                            <span className="px-2 py-0.5 text-xs rounded-full bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300">
                              required
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-3 mt-1 text-sm text-gray-500 dark:text-gray-400">
                          <span>
                            Project: {projectName || (
                              <span className="italic text-gray-400 dark:text-gray-500">Global</span>
                            )}
                          </span>
                          <span className="text-gray-300 dark:text-gray-600">|</span>
                          <span>
                            Issue Type: {issueTypeName || (
                              <span className="italic text-gray-400 dark:text-gray-500">All</span>
                            )}
                          </span>
                        </div>
                        {ctx.default_value && (
                          <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                            Default: {ctx.default_value}
                          </p>
                        )}
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        <button
                          onClick={() => startEditContext(ctx)}
                          className="btn-icon"
                          title="Edit context"
                        >
                          <Edit2 size={16} />
                        </button>
                        <button
                          onClick={() => {
                            if (confirm('Delete this field context?')) {
                              deleteContextMutation.mutate(ctx.id)
                            }
                          }}
                          className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                          title="Delete context"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
