import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Bell, Plus, Edit2, Trash2, ChevronDown, AlertTriangle } from 'lucide-react'
import { notificationSchemesApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner from '../../components/LoadingSpinner'

const EVENT_TYPES = [
  'issue_created', 'issue_updated', 'issue_assigned', 'issue_resolved', 'issue_closed',
  'issue_reopened', 'issue_deleted', 'comment_added', 'comment_edited', 'comment_deleted',
  'attachment_added', 'attachment_deleted', 'worklog_added', 'worklog_updated',
  'worklog_deleted', 'sprint_started', 'sprint_completed', 'version_released', 'sla_breached',
]

const RECIPIENT_TYPES = [
  { value: 'current_assignee', label: 'Current Assignee' },
  { value: 'reporter', label: 'Reporter' },
  { value: 'project_lead', label: 'Project Lead' },
  { value: 'all_watchers', label: 'All Watchers' },
  { value: 'user', label: 'Specific User' },
  { value: 'group', label: 'Group' },
  { value: 'project_role', label: 'Project Role' },
]

const NEEDS_VALUE = ['user', 'group', 'project_role']

export default function NotificationSchemes() {
  const queryClient = useQueryClient()
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [expandedId, setExpandedId] = useState(null)
  const [formData, setFormData] = useState({ name: '', slug: '', description: '' })
  const [eventForm, setEventForm] = useState({ event_type: EVENT_TYPES[0], recipient_type: 'current_assignee', recipient_value: '' })
  const [addingEventFor, setAddingEventFor] = useState(null)

  // Fetch schemes
  const { data, isLoading } = useQuery({
    queryKey: ['notificationSchemes'],
    queryFn: () => notificationSchemesApi.list().then((res) => res.data),
  })

  const schemes = data?.results || data || []

  // Create scheme
  const createMutation = useMutation({
    mutationFn: (data) => notificationSchemesApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationSchemes'] })
      setShowCreateForm(false)
      setFormData({ name: '', slug: '', description: '' })
    },
  })

  // Update scheme
  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => notificationSchemesApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationSchemes'] })
      setEditingId(null)
      setFormData({ name: '', slug: '', description: '' })
    },
  })

  // Delete scheme
  const deleteMutation = useMutation({
    mutationFn: (id) => notificationSchemesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationSchemes'] })
    },
  })

  // Add event
  const addEventMutation = useMutation({
    mutationFn: ({ schemeId, data }) => notificationSchemesApi.addEvent(schemeId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationSchemes'] })
      setAddingEventFor(null)
      setEventForm({ event_type: EVENT_TYPES[0], recipient_type: 'current_assignee', recipient_value: '' })
    },
  })

  // Remove event
  const removeEventMutation = useMutation({
    mutationFn: ({ schemeId, eventId }) => notificationSchemesApi.removeEvent(schemeId, { event_id: eventId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationSchemes'] })
    },
  })

  const handleSchemeSubmit = (e) => {
    e.preventDefault()
    if (editingId) {
      updateMutation.mutate({ id: editingId, ...formData })
    } else {
      createMutation.mutate(formData)
    }
  }

  const startEdit = (scheme) => {
    setEditingId(scheme.id)
    setFormData({ name: scheme.name, slug: scheme.slug || '', description: scheme.description || '' })
    setShowCreateForm(true)
  }

  const handleAddEvent = (e, schemeId) => {
    e.preventDefault()
    const data = {
      event_type: eventForm.event_type,
      recipient_type: eventForm.recipient_type,
    }
    if (NEEDS_VALUE.includes(eventForm.recipient_type) && eventForm.recipient_value) {
      data.recipient_value = eventForm.recipient_value
    }
    addEventMutation.mutate({ schemeId, data })
  }

  const toggleExpand = (id) => {
    setExpandedId(expandedId === id ? null : id)
    setAddingEventFor(null)
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Notification Schemes</h1>
          <p className="text-gray-500 dark:text-gray-400">Configure which events trigger notifications and who receives them</p>
        </div>
        <button
          onClick={() => {
            setEditingId(null)
            setFormData({ name: '', slug: '', description: '' })
            setShowCreateForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          New Scheme
        </button>
      </div>

      {/* Create/Edit Form */}
      {showCreateForm && (
        <div className="card p-4">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
            <Bell size={20} />
            {editingId ? 'Edit Scheme' : 'Create Notification Scheme'}
          </h2>
          <form onSubmit={handleSchemeSubmit} className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Name *
                </label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="input w-full"
                  placeholder="e.g., Default Notification Scheme"
                  required
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Slug *
                </label>
                <input
                  type="text"
                  value={formData.slug}
                  onChange={(e) => setFormData({ ...formData, slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '-') })}
                  className="input w-full"
                  placeholder="e.g., default-notification-scheme"
                  required
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
                placeholder="Brief description of this notification scheme"
              />
            </div>
            {(createMutation.isError || updateMutation.isError) && (
              <div className="p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg flex items-center gap-2">
                <AlertTriangle size={16} />
                {createMutation.error?.response?.data?.detail || updateMutation.error?.response?.data?.detail || 'Failed to save scheme'}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setShowCreateForm(false)
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
                {createMutation.isPending || updateMutation.isPending ? 'Saving...' : editingId ? 'Update Scheme' : 'Create Scheme'}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Schemes List */}
      {schemes.length === 0 && !showCreateForm ? (
        <div className="card p-8 text-center">
          <Bell size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">No notification schemes</h3>
          <p className="text-gray-500 dark:text-gray-400 mb-4">Create a scheme to define notification rules for your projects</p>
          <button
            onClick={() => setShowCreateForm(true)}
            className="btn-primary inline-flex items-center gap-2"
          >
            <Plus size={18} />
            Create First Scheme
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {schemes.map((scheme) => {
            const events = scheme.events || scheme.notification_events || []
            const isExpanded = expandedId === scheme.id

            return (
              <div key={scheme.id} className="card">
                {/* Scheme Header */}
                <div
                  className="flex items-center justify-between p-4 cursor-pointer"
                  onClick={() => toggleExpand(scheme.id)}
                >
                  <div className="flex items-center gap-3 flex-1 min-w-0">
                    <Bell size={20} className="text-indigo-500 dark:text-indigo-400 shrink-0" />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <h3 className="font-medium text-gray-900 dark:text-gray-100 truncate">{scheme.name}</h3>
                        {scheme.slug && (
                          <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">{scheme.slug}</span>
                        )}
                      </div>
                      {scheme.description && (
                        <p className="text-sm text-gray-500 dark:text-gray-400 truncate">{scheme.description}</p>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className="text-sm text-gray-500 dark:text-gray-400">
                      {events.length} event{events.length !== 1 ? 's' : ''}
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        startEdit(scheme)
                      }}
                      className="btn-icon"
                      title="Edit scheme"
                    >
                      <Edit2 size={16} />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        if (confirm(`Delete scheme "${scheme.name}"? This cannot be undone.`)) {
                          deleteMutation.mutate(scheme.id)
                        }
                      }}
                      className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                      title="Delete scheme"
                    >
                      <Trash2 size={16} />
                    </button>
                    <ChevronDown
                      size={18}
                      className={cn(
                        'text-gray-400 transition-transform',
                        isExpanded && 'rotate-180'
                      )}
                    />
                  </div>
                </div>

                {/* Expanded Events */}
                {isExpanded && (
                  <div className="border-t border-gray-200 dark:border-gray-700 p-4">
                    {/* Events List */}
                    {events.length > 0 ? (
                      <div className="space-y-2 mb-4">
                        <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Notification Events</h4>
                        <div className="divide-y divide-gray-100 dark:divide-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
                          {events.map((event) => (
                            <div
                              key={event.id}
                              className="flex items-center justify-between px-3 py-2"
                            >
                              <div className="flex items-center gap-4 text-sm">
                                <span className="font-mono text-xs px-2 py-0.5 rounded bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300">
                                  {event.event_type}
                                </span>
                                <span className="text-gray-500 dark:text-gray-400">
                                  {RECIPIENT_TYPES.find((r) => r.value === event.recipient_type)?.label || event.recipient_type}
                                </span>
                                {event.recipient_value && (
                                  <span className="text-gray-600 dark:text-gray-300 font-medium">
                                    {event.recipient_value}
                                  </span>
                                )}
                              </div>
                              <button
                                onClick={() => {
                                  if (confirm('Remove this event?')) {
                                    removeEventMutation.mutate({ schemeId: scheme.id, eventId: event.id })
                                  }
                                }}
                                disabled={removeEventMutation.isPending}
                                className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                                title="Remove event"
                              >
                                <Trash2 size={14} />
                              </button>
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : (
                      <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
                        No events configured. Add events to define when notifications are sent.
                      </p>
                    )}

                    {/* Add Event Form */}
                    {addingEventFor === scheme.id ? (
                      <form
                        onSubmit={(e) => handleAddEvent(e, scheme.id)}
                        className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3"
                      >
                        <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">Add Event</h4>
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                          <div>
                            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                              Event Type
                            </label>
                            <select
                              value={eventForm.event_type}
                              onChange={(e) => setEventForm({ ...eventForm, event_type: e.target.value })}
                              className="input w-full text-sm"
                            >
                              {EVENT_TYPES.map((type) => (
                                <option key={type} value={type}>
                                  {type.replace(/_/g, ' ')}
                                </option>
                              ))}
                            </select>
                          </div>
                          <div>
                            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                              Recipient Type
                            </label>
                            <select
                              value={eventForm.recipient_type}
                              onChange={(e) => setEventForm({ ...eventForm, recipient_type: e.target.value, recipient_value: '' })}
                              className="input w-full text-sm"
                            >
                              {RECIPIENT_TYPES.map((type) => (
                                <option key={type.value} value={type.value}>
                                  {type.label}
                                </option>
                              ))}
                            </select>
                          </div>
                          {NEEDS_VALUE.includes(eventForm.recipient_type) && (
                            <div>
                              <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                                Recipient Value
                              </label>
                              <input
                                type="text"
                                value={eventForm.recipient_value}
                                onChange={(e) => setEventForm({ ...eventForm, recipient_value: e.target.value })}
                                className="input w-full text-sm"
                                placeholder={
                                  eventForm.recipient_type === 'user' ? 'user@example.com' :
                                  eventForm.recipient_type === 'group' ? 'group-name' :
                                  'role-name'
                                }
                                required
                              />
                            </div>
                          )}
                        </div>
                        {addEventMutation.isError && (
                          <div className="p-2 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-xs rounded">
                            {addEventMutation.error?.response?.data?.detail || 'Failed to add event'}
                          </div>
                        )}
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => setAddingEventFor(null)}
                            className="btn text-sm"
                          >
                            Cancel
                          </button>
                          <button
                            type="submit"
                            disabled={addEventMutation.isPending}
                            className="btn-primary text-sm"
                          >
                            {addEventMutation.isPending ? 'Adding...' : 'Add Event'}
                          </button>
                        </div>
                      </form>
                    ) : (
                      <button
                        onClick={() => {
                          setAddingEventFor(scheme.id)
                          setEventForm({ event_type: EVENT_TYPES[0], recipient_type: 'current_assignee', recipient_value: '' })
                        }}
                        className="btn flex items-center gap-2 text-sm"
                      >
                        <Plus size={16} />
                        Add Event
                      </button>
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
