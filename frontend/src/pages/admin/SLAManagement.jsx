import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Clock, Calendar, Timer, Plus, Edit2, Trash2, AlertTriangle } from 'lucide-react'
import { slaDefinitionsApi, businessCalendarsApi, slaInstancesApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner from '../../components/LoadingSpinner'

const TABS = [
  { key: 'definitions', label: 'SLA Definitions', icon: Clock },
  { key: 'calendars', label: 'Business Calendars', icon: Calendar },
  { key: 'instances', label: 'SLA Instances', icon: Timer },
]

const STATUS_COLORS = {
  active: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  paused: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-300',
  met: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  breached: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
}

export default function SLAManagement() {
  const [activeTab, setActiveTab] = useState('definitions')

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">SLA Management</h1>
        <p className="text-gray-500 dark:text-gray-400">Define service level agreements and business hours</p>
      </div>

      {/* Tab Navigation */}
      <div className="flex flex-wrap gap-2 border-b border-gray-200 dark:border-gray-700 pb-2">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={cn(
              'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors',
              activeTab === tab.key
                ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300'
                : 'text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'
            )}
          >
            <tab.icon size={18} />
            <span className="hidden sm:inline">{tab.label}</span>
          </button>
        ))}
      </div>

      {/* Tab Content */}
      {activeTab === 'definitions' && <SLADefinitionsTab />}
      {activeTab === 'calendars' && <BusinessCalendarsTab />}
      {activeTab === 'instances' && <SLAInstancesTab />}
    </div>
  )
}

// =============================================================================
// SLA Definitions Tab
// =============================================================================
function SLADefinitionsTab() {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [formData, setFormData] = useState({
    name: '', description: '', target_hours: '', warning_threshold_percent: '',
    start_condition: '{}', stop_condition: '{}', pause_condition: '{}', calendar: '',
  })

  const { data, isLoading } = useQuery({
    queryKey: ['slaDefinitions'],
    queryFn: () => slaDefinitionsApi.list().then((res) => res.data),
  })

  const { data: calendarsData } = useQuery({
    queryKey: ['businessCalendars'],
    queryFn: () => businessCalendarsApi.list().then((res) => res.data),
  })

  const definitions = data?.results || data || []
  const calendars = calendarsData?.results || calendarsData || []

  const createMutation = useMutation({
    mutationFn: (data) => slaDefinitionsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['slaDefinitions'] })
      resetForm()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => slaDefinitionsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['slaDefinitions'] })
      resetForm()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => slaDefinitionsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['slaDefinitions'] })
    },
  })

  const resetForm = () => {
    setShowForm(false)
    setEditingId(null)
    setFormData({
      name: '', description: '', target_hours: '', warning_threshold_percent: '',
      start_condition: '{}', stop_condition: '{}', pause_condition: '{}', calendar: '',
    })
  }

  const startEdit = (def) => {
    setEditingId(def.id)
    setFormData({
      name: def.name,
      description: def.description || '',
      target_hours: def.target_hours ?? '',
      warning_threshold_percent: def.warning_threshold_percent ?? '',
      start_condition: typeof def.start_condition === 'object' ? JSON.stringify(def.start_condition, null, 2) : (def.start_condition || '{}'),
      stop_condition: typeof def.stop_condition === 'object' ? JSON.stringify(def.stop_condition, null, 2) : (def.stop_condition || '{}'),
      pause_condition: typeof def.pause_condition === 'object' ? JSON.stringify(def.pause_condition, null, 2) : (def.pause_condition || '{}'),
      calendar: def.calendar || '',
    })
    setShowForm(true)
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    let payload
    try {
      payload = {
        name: formData.name,
        description: formData.description || undefined,
        target_hours: parseFloat(formData.target_hours),
        warning_threshold_percent: formData.warning_threshold_percent ? parseInt(formData.warning_threshold_percent, 10) : undefined,
        start_condition: JSON.parse(formData.start_condition),
        stop_condition: JSON.parse(formData.stop_condition),
        pause_condition: JSON.parse(formData.pause_condition),
        calendar: formData.calendar || undefined,
      }
    } catch {
      alert('Invalid JSON in condition fields. Please check the syntax.')
      return
    }

    if (editingId) {
      updateMutation.mutate({ id: editingId, ...payload })
    } else {
      createMutation.mutate(payload)
    }
  }

  if (isLoading) {
    return <div className="flex items-center justify-center min-h-[200px]"><LoadingSpinner size="lg" /></div>
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
          <Clock size={20} />
          SLA Definitions ({definitions.length})
        </h2>
        <button
          onClick={() => {
            setEditingId(null)
            setFormData({ name: '', description: '', target_hours: '', warning_threshold_percent: '', start_condition: '{}', stop_condition: '{}', pause_condition: '{}', calendar: '' })
            setShowForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          New Definition
        </button>
      </div>

      {/* Create/Edit Form */}
      {showForm && (
        <div className="card p-4">
          <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">
            {editingId ? 'Edit SLA Definition' : 'Create SLA Definition'}
          </h3>
          <form onSubmit={handleSubmit} className="space-y-4">
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
                  placeholder="e.g., Critical Issue Response"
                  required
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
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Target Hours *
                </label>
                <input
                  type="number"
                  value={formData.target_hours}
                  onChange={(e) => setFormData({ ...formData, target_hours: e.target.value })}
                  className="input w-full"
                  placeholder="e.g., 4"
                  min="0"
                  step="0.5"
                  required
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Warning Threshold (%)
                </label>
                <input
                  type="number"
                  value={formData.warning_threshold_percent}
                  onChange={(e) => setFormData({ ...formData, warning_threshold_percent: e.target.value })}
                  className="input w-full"
                  placeholder="e.g., 75"
                  min="0"
                  max="100"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Business Calendar
                </label>
                <select
                  value={formData.calendar}
                  onChange={(e) => setFormData({ ...formData, calendar: e.target.value })}
                  className="input w-full"
                >
                  <option value="">None (24/7)</option>
                  {calendars.map((cal) => (
                    <option key={cal.id} value={cal.id}>{cal.name}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Start Condition (JSON)
                </label>
                <textarea
                  value={formData.start_condition}
                  onChange={(e) => setFormData({ ...formData, start_condition: e.target.value })}
                  className="input w-full font-mono text-xs"
                  rows={3}
                  placeholder='{"status": "open"}'
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Stop Condition (JSON)
                </label>
                <textarea
                  value={formData.stop_condition}
                  onChange={(e) => setFormData({ ...formData, stop_condition: e.target.value })}
                  className="input w-full font-mono text-xs"
                  rows={3}
                  placeholder='{"status": "resolved"}'
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Pause Condition (JSON)
                </label>
                <textarea
                  value={formData.pause_condition}
                  onChange={(e) => setFormData({ ...formData, pause_condition: e.target.value })}
                  className="input w-full font-mono text-xs"
                  rows={3}
                  placeholder='{"status": "waiting"}'
                />
              </div>
            </div>
            {(createMutation.isError || updateMutation.isError) && (
              <div className="p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg flex items-center gap-2">
                <AlertTriangle size={16} />
                {createMutation.error?.response?.data?.detail || updateMutation.error?.response?.data?.detail || 'Failed to save definition'}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button type="button" onClick={resetForm} className="btn">Cancel</button>
              <button
                type="submit"
                disabled={createMutation.isPending || updateMutation.isPending}
                className="btn-primary"
              >
                {createMutation.isPending || updateMutation.isPending ? 'Saving...' : editingId ? 'Update' : 'Create'}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Definitions List */}
      {definitions.length === 0 && !showForm ? (
        <div className="card p-8 text-center">
          <Clock size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">No SLA definitions</h3>
          <p className="text-gray-500 dark:text-gray-400">Create SLA definitions to track service level commitments</p>
        </div>
      ) : (
        <div className="space-y-3">
          {definitions.map((def) => (
            <div key={def.id} className="card p-4">
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <h3 className="font-medium text-gray-900 dark:text-gray-100">{def.name}</h3>
                  {def.description && (
                    <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{def.description}</p>
                  )}
                  <div className="flex flex-wrap items-center gap-3 mt-2 text-xs text-gray-500 dark:text-gray-400">
                    <span className="flex items-center gap-1">
                      <Clock size={12} />
                      Target: {def.target_hours}h
                    </span>
                    {def.warning_threshold_percent != null && (
                      <span className="flex items-center gap-1">
                        <AlertTriangle size={12} />
                        Warning at {def.warning_threshold_percent}%
                      </span>
                    )}
                    {def.start_condition && Object.keys(def.start_condition).length > 0 && (
                      <span>Start: {JSON.stringify(def.start_condition)}</span>
                    )}
                    {def.stop_condition && Object.keys(def.stop_condition).length > 0 && (
                      <span>Stop: {JSON.stringify(def.stop_condition)}</span>
                    )}
                    {def.pause_condition && Object.keys(def.pause_condition).length > 0 && (
                      <span>Pause: {JSON.stringify(def.pause_condition)}</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    onClick={() => startEdit(def)}
                    className="btn-icon"
                    title="Edit"
                  >
                    <Edit2 size={16} />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Delete SLA definition "${def.name}"?`)) {
                        deleteMutation.mutate(def.id)
                      }
                    }}
                    className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                    title="Delete"
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// =============================================================================
// Business Calendars Tab
// =============================================================================
function BusinessCalendarsTab() {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [formData, setFormData] = useState({
    name: '', timezone: '', working_hours: '{\n  "monday": {"start": "09:00", "end": "17:00"},\n  "tuesday": {"start": "09:00", "end": "17:00"},\n  "wednesday": {"start": "09:00", "end": "17:00"},\n  "thursday": {"start": "09:00", "end": "17:00"},\n  "friday": {"start": "09:00", "end": "17:00"}\n}', holidays: '[]',
  })

  const { data, isLoading } = useQuery({
    queryKey: ['businessCalendars'],
    queryFn: () => businessCalendarsApi.list().then((res) => res.data),
  })

  const calendars = data?.results || data || []

  const createMutation = useMutation({
    mutationFn: (data) => businessCalendarsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['businessCalendars'] })
      resetForm()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => businessCalendarsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['businessCalendars'] })
      resetForm()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => businessCalendarsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['businessCalendars'] })
    },
  })

  const resetForm = () => {
    setShowForm(false)
    setEditingId(null)
    setFormData({
      name: '', timezone: '', working_hours: '{\n  "monday": {"start": "09:00", "end": "17:00"},\n  "tuesday": {"start": "09:00", "end": "17:00"},\n  "wednesday": {"start": "09:00", "end": "17:00"},\n  "thursday": {"start": "09:00", "end": "17:00"},\n  "friday": {"start": "09:00", "end": "17:00"}\n}', holidays: '[]',
    })
  }

  const startEdit = (cal) => {
    setEditingId(cal.id)
    setFormData({
      name: cal.name,
      timezone: cal.timezone || '',
      working_hours: typeof cal.working_hours === 'object' ? JSON.stringify(cal.working_hours, null, 2) : (cal.working_hours || '{}'),
      holidays: typeof cal.holidays === 'object' ? JSON.stringify(cal.holidays, null, 2) : (cal.holidays || '[]'),
    })
    setShowForm(true)
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    let payload
    try {
      payload = {
        name: formData.name,
        timezone: formData.timezone,
        working_hours: JSON.parse(formData.working_hours),
        holidays: JSON.parse(formData.holidays),
      }
    } catch {
      alert('Invalid JSON in working hours or holidays fields.')
      return
    }

    if (editingId) {
      updateMutation.mutate({ id: editingId, ...payload })
    } else {
      createMutation.mutate(payload)
    }
  }

  const formatWorkingHours = (hours) => {
    if (!hours || typeof hours !== 'object') return 'Not configured'
    const days = Object.entries(hours)
    if (days.length === 0) return 'Not configured'
    return days.map(([day, times]) => {
      const dayLabel = day.charAt(0).toUpperCase() + day.slice(1, 3)
      return `${dayLabel} ${times.start}-${times.end}`
    }).join(', ')
  }

  const countHolidays = (holidays) => {
    if (Array.isArray(holidays)) return holidays.length
    return 0
  }

  if (isLoading) {
    return <div className="flex items-center justify-center min-h-[200px]"><LoadingSpinner size="lg" /></div>
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
          <Calendar size={20} />
          Business Calendars ({calendars.length})
        </h2>
        <button
          onClick={() => {
            resetForm()
            setShowForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          New Calendar
        </button>
      </div>

      {/* Create/Edit Form */}
      {showForm && (
        <div className="card p-4">
          <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">
            {editingId ? 'Edit Business Calendar' : 'Create Business Calendar'}
          </h3>
          <form onSubmit={handleSubmit} className="space-y-4">
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
                  placeholder="e.g., India Business Hours"
                  required
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Timezone *
                </label>
                <input
                  type="text"
                  value={formData.timezone}
                  onChange={(e) => setFormData({ ...formData, timezone: e.target.value })}
                  className="input w-full"
                  placeholder="e.g., Asia/Kolkata"
                  required
                />
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Working Hours (JSON)
              </label>
              <textarea
                value={formData.working_hours}
                onChange={(e) => setFormData({ ...formData, working_hours: e.target.value })}
                className="input w-full font-mono text-xs"
                rows={6}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Holidays (JSON array)
              </label>
              <textarea
                value={formData.holidays}
                onChange={(e) => setFormData({ ...formData, holidays: e.target.value })}
                className="input w-full font-mono text-xs"
                rows={3}
                placeholder='[{"date": "2026-01-26", "name": "Republic Day"}]'
              />
            </div>
            {(createMutation.isError || updateMutation.isError) && (
              <div className="p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg flex items-center gap-2">
                <AlertTriangle size={16} />
                {createMutation.error?.response?.data?.detail || updateMutation.error?.response?.data?.detail || 'Failed to save calendar'}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button type="button" onClick={resetForm} className="btn">Cancel</button>
              <button
                type="submit"
                disabled={createMutation.isPending || updateMutation.isPending}
                className="btn-primary"
              >
                {createMutation.isPending || updateMutation.isPending ? 'Saving...' : editingId ? 'Update' : 'Create'}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Calendars List */}
      {calendars.length === 0 && !showForm ? (
        <div className="card p-8 text-center">
          <Calendar size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">No business calendars</h3>
          <p className="text-gray-500 dark:text-gray-400">Create calendars to define working hours for SLA tracking</p>
        </div>
      ) : (
        <div className="space-y-3">
          {calendars.map((cal) => (
            <div key={cal.id} className="card p-4">
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <h3 className="font-medium text-gray-900 dark:text-gray-100">{cal.name}</h3>
                  <div className="flex flex-wrap items-center gap-3 mt-2 text-xs text-gray-500 dark:text-gray-400">
                    {cal.timezone && (
                      <span className="flex items-center gap-1">
                        <Clock size={12} />
                        {cal.timezone}
                      </span>
                    )}
                    <span className="truncate max-w-md">
                      {formatWorkingHours(cal.working_hours)}
                    </span>
                    <span>
                      {countHolidays(cal.holidays)} holiday{countHolidays(cal.holidays) !== 1 ? 's' : ''}
                    </span>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    onClick={() => startEdit(cal)}
                    className="btn-icon"
                    title="Edit"
                  >
                    <Edit2 size={16} />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Delete calendar "${cal.name}"?`)) {
                        deleteMutation.mutate(cal.id)
                      }
                    }}
                    className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                    title="Delete"
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// =============================================================================
// SLA Instances Tab (Read-only)
// =============================================================================
function SLAInstancesTab() {
  const [statusFilter, setStatusFilter] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['slaInstances', statusFilter],
    queryFn: () => {
      const params = statusFilter ? { status: statusFilter } : {}
      return slaInstancesApi.list(params).then((res) => res.data)
    },
  })

  const instances = data?.results || data || []

  const formatDateTime = (dt) => {
    if (!dt) return '-'
    return new Date(dt).toLocaleString()
  }

  const formatDuration = (seconds) => {
    if (seconds == null) return '-'
    const hrs = Math.floor(seconds / 3600)
    const mins = Math.floor((seconds % 3600) / 60)
    if (hrs > 0) return `${hrs}h ${mins}m`
    return `${mins}m`
  }

  if (isLoading) {
    return <div className="flex items-center justify-center min-h-[200px]"><LoadingSpinner size="lg" /></div>
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center gap-2">
          <Timer size={20} />
          SLA Instances ({instances.length})
        </h2>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="input text-sm"
        >
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="paused">Paused</option>
          <option value="met">Met</option>
          <option value="breached">Breached</option>
        </select>
      </div>

      {instances.length === 0 ? (
        <div className="card p-8 text-center">
          <Timer size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">No SLA instances</h3>
          <p className="text-gray-500 dark:text-gray-400">
            {statusFilter ? `No instances with status "${statusFilter}"` : 'SLA instances are created automatically when issues match SLA definitions'}
          </p>
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800">
                  <th className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300">Item</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300">SLA Definition</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300">Status</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300">Started</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300">Target</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300">Elapsed</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-700 dark:text-gray-300">Breached</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                {instances.map((instance) => (
                  <tr key={instance.id} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                    <td className="px-4 py-3 text-gray-900 dark:text-gray-100 font-medium">
                      {instance.item_key || instance.item_reference || instance.item || '-'}
                    </td>
                    <td className="px-4 py-3 text-gray-600 dark:text-gray-400">
                      {instance.sla_definition_name || instance.sla_definition || '-'}
                    </td>
                    <td className="px-4 py-3">
                      <span className={cn(
                        'px-2 py-0.5 text-xs rounded font-medium',
                        STATUS_COLORS[instance.status] || 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
                      )}>
                        {instance.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-500 dark:text-gray-400 whitespace-nowrap">
                      {formatDateTime(instance.started_at)}
                    </td>
                    <td className="px-4 py-3 text-gray-500 dark:text-gray-400 whitespace-nowrap">
                      {formatDateTime(instance.target_time)}
                    </td>
                    <td className="px-4 py-3 text-gray-500 dark:text-gray-400 whitespace-nowrap">
                      {instance.elapsed_time != null ? formatDuration(instance.elapsed_time) : (instance.elapsed_seconds != null ? formatDuration(instance.elapsed_seconds) : '-')}
                    </td>
                    <td className="px-4 py-3">
                      {instance.is_breached ? (
                        <span className="px-2 py-0.5 text-xs rounded font-medium bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300">
                          Breached
                        </span>
                      ) : (
                        <span className="px-2 py-0.5 text-xs rounded font-medium bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300">
                          On Track
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
