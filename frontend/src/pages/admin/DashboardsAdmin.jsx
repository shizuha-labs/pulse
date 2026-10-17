import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  LayoutDashboard, BarChart3, Plus, Edit2, Trash2, ChevronDown, Share2, Eye,
  Grid3X3,
} from 'lucide-react'
import { dashboardsApi, dashboardGadgetsApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner from '../../components/LoadingSpinner'

const GADGET_TYPES = [
  // Charts
  { value: 'pie_chart', label: 'Pie Chart', category: 'Charts' },
  { value: 'bar_chart', label: 'Bar Chart', category: 'Charts' },
  { value: 'line_chart', label: 'Line Chart', category: 'Charts' },
  { value: 'heat_map', label: 'Heat Map', category: 'Charts' },
  { value: 'custom_chart', label: 'Custom Chart', category: 'Charts' },
  // Sprint
  { value: 'sprint_burndown', label: 'Sprint Burndown', category: 'Sprint' },
  { value: 'sprint_health', label: 'Sprint Health', category: 'Sprint' },
  { value: 'velocity_chart', label: 'Velocity Chart', category: 'Sprint' },
  { value: 'cumulative_flow', label: 'Cumulative Flow', category: 'Sprint' },
  // Reports
  { value: 'filter_results', label: 'Filter Results', category: 'Reports' },
  { value: 'created_vs_resolved', label: 'Created vs Resolved', category: 'Reports' },
  { value: 'time_tracking_report', label: 'Time Tracking Report', category: 'Reports' },
  { value: 'workload_pie', label: 'Workload Pie', category: 'Reports' },
  { value: 'resolution_time', label: 'Resolution Time', category: 'Reports' },
  { value: 'sla_status', label: 'SLA Status', category: 'Reports' },
  // Personal
  { value: 'assigned_to_me', label: 'Assigned to Me', category: 'Personal' },
  { value: 'watched_issues', label: 'Watched Issues', category: 'Personal' },
  { value: 'activity_stream', label: 'Activity Stream', category: 'Personal' },
  { value: 'calendar', label: 'Calendar', category: 'Personal' },
  // Other
  { value: 'project_summary', label: 'Project Summary', category: 'Other' },
  { value: 'text_note', label: 'Text Note', category: 'Other' },
]

const CATEGORY_COLORS = {
  Charts: 'text-blue-600 dark:text-blue-400',
  Sprint: 'text-green-600 dark:text-green-400',
  Reports: 'text-amber-600 dark:text-amber-400',
  Personal: 'text-purple-600 dark:text-purple-400',
  Other: 'text-gray-600 dark:text-gray-400',
}

const GADGET_CATEGORY_MAP = Object.fromEntries(
  GADGET_TYPES.map((g) => [g.value, g.category])
)

const GADGET_LABEL_MAP = Object.fromEntries(
  GADGET_TYPES.map((g) => [g.value, g.label])
)

const GADGET_CATEGORY_BADGE = {
  Charts: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  Sprint: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  Reports: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  Personal: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  Other: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
}

const emptyDashboardForm = {
  name: '',
  description: '',
  is_shared: false,
  is_wallboard: false,
  layout: '',
}

const emptyGadgetForm = {
  title: '',
  gadget_type: 'filter_results',
  config: '',
  position_x: 0,
  position_y: 0,
  width: 6,
  height: 2,
}

export default function DashboardsAdmin() {
  const queryClient = useQueryClient()

  // UI state
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [editingDashboardId, setEditingDashboardId] = useState(null)
  const [expandedDashboardId, setExpandedDashboardId] = useState(null)
  const [dashboardForm, setDashboardForm] = useState(emptyDashboardForm)
  const [gadgetForm, setGadgetForm] = useState(emptyGadgetForm)
  const [showAddGadget, setShowAddGadget] = useState(null)

  // Queries
  const { data: dashboards, isLoading, isError, error } = useQuery({
    queryKey: ['dashboards-admin'],
    queryFn: async () => {
      const res = await dashboardsApi.list()
      return res.data.results || res.data || []
    },
  })

  const { data: allGadgets } = useQuery({
    queryKey: ['dashboard-gadgets-admin'],
    queryFn: async () => {
      const res = await dashboardGadgetsApi.list()
      return res.data.results || res.data || []
    },
  })

  // Mutations
  const createDashboardMutation = useMutation({
    mutationFn: (data) => {
      const payload = { ...data }
      if (payload.layout) {
        try {
          payload.layout = JSON.parse(payload.layout)
        } catch {
          // leave as string
        }
      } else {
        delete payload.layout
      }
      return dashboardsApi.create(payload)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboards-admin'] })
      setShowCreateForm(false)
      setDashboardForm(emptyDashboardForm)
    },
  })

  const updateDashboardMutation = useMutation({
    mutationFn: ({ id, ...data }) => {
      const payload = { ...data }
      if (payload.layout) {
        try {
          payload.layout = JSON.parse(payload.layout)
        } catch {
          // leave as string
        }
      } else {
        delete payload.layout
      }
      return dashboardsApi.update(id, payload)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboards-admin'] })
      setEditingDashboardId(null)
      setDashboardForm(emptyDashboardForm)
    },
  })

  const deleteDashboardMutation = useMutation({
    mutationFn: (id) => dashboardsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboards-admin'] })
    },
  })

  const createGadgetMutation = useMutation({
    mutationFn: (data) => {
      const payload = { ...data }
      if (payload.config) {
        try {
          payload.config = JSON.parse(payload.config)
        } catch {
          // leave as string
        }
      } else {
        delete payload.config
      }
      payload.position_x = parseInt(payload.position_x, 10) || 0
      payload.position_y = parseInt(payload.position_y, 10) || 0
      payload.width = parseInt(payload.width, 10) || 6
      payload.height = parseInt(payload.height, 10) || 2
      return dashboardGadgetsApi.create(payload)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboards-admin'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-gadgets-admin'] })
      setShowAddGadget(null)
      setGadgetForm(emptyGadgetForm)
    },
  })

  const deleteGadgetMutation = useMutation({
    mutationFn: (id) => dashboardGadgetsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboards-admin'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard-gadgets-admin'] })
    },
  })

  // Handlers
  const handleDashboardSubmit = (e) => {
    e.preventDefault()
    if (editingDashboardId) {
      updateDashboardMutation.mutate({ id: editingDashboardId, ...dashboardForm })
    } else {
      createDashboardMutation.mutate(dashboardForm)
    }
  }

  const startEditDashboard = (dashboard) => {
    setEditingDashboardId(dashboard.id)
    setDashboardForm({
      name: dashboard.name || '',
      description: dashboard.description || '',
      is_shared: dashboard.is_shared || false,
      is_wallboard: dashboard.is_wallboard || false,
      layout: dashboard.layout ? JSON.stringify(dashboard.layout, null, 2) : '',
    })
    setShowCreateForm(true)
  }

  const handleDeleteDashboard = (dashboard) => {
    if (confirm(`Delete dashboard "${dashboard.name}"? All gadgets will also be removed.`)) {
      deleteDashboardMutation.mutate(dashboard.id)
    }
  }

  const handleAddGadgetSubmit = (e, dashboardId) => {
    e.preventDefault()
    createGadgetMutation.mutate({
      dashboard: dashboardId,
      ...gadgetForm,
    })
  }

  const handleDeleteGadget = (gadgetId) => {
    if (confirm('Delete this gadget?')) {
      deleteGadgetMutation.mutate(gadgetId)
    }
  }

  const getGadgetsForDashboard = (dashboardId) => {
    if (!allGadgets) return []
    return allGadgets.filter((g) => g.dashboard === dashboardId)
  }

  const toggleExpand = (dashboardId) => {
    setExpandedDashboardId(expandedDashboardId === dashboardId ? null : dashboardId)
    setShowAddGadget(null)
  }

  // Group gadget type options by category for the select
  const groupedGadgetOptions = () => {
    const groups = {}
    for (const gt of GADGET_TYPES) {
      if (!groups[gt.category]) groups[gt.category] = []
      groups[gt.category].push(gt)
    }
    return groups
  }

  // Loading state
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  // Error state
  if (isError) {
    return (
      <div className="p-4 bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300 rounded-lg">
        Failed to load dashboards: {error?.message || 'Unknown error'}
      </div>
    )
  }

  const dashboardList = dashboards || []
  const gadgetGroups = groupedGadgetOptions()

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <LayoutDashboard size={24} />
            Dashboards Administration
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            Manage dashboards and gadgets
          </p>
        </div>
        <button
          onClick={() => {
            setEditingDashboardId(null)
            setDashboardForm(emptyDashboardForm)
            setShowCreateForm(!showCreateForm)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          Create Dashboard
        </button>
      </div>

      {/* Create / Edit Dashboard Form */}
      {showCreateForm && (
        <form
          onSubmit={handleDashboardSubmit}
          className="card p-4 space-y-4"
        >
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <LayoutDashboard size={20} />
            {editingDashboardId ? 'Edit Dashboard' : 'Create Dashboard'}
          </h2>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Name *
            </label>
            <input
              type="text"
              value={dashboardForm.name}
              onChange={(e) => setDashboardForm({ ...dashboardForm, name: e.target.value })}
              className="input w-full"
              placeholder="e.g., Project Overview, Sprint Dashboard"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Description
            </label>
            <textarea
              value={dashboardForm.description}
              onChange={(e) => setDashboardForm({ ...dashboardForm, description: e.target.value })}
              className="input w-full"
              rows={2}
              placeholder="Brief description of this dashboard"
            />
          </div>

          <div className="flex items-center gap-6">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={dashboardForm.is_shared}
                onChange={(e) => setDashboardForm({ ...dashboardForm, is_shared: e.target.checked })}
                className="rounded border-gray-300 dark:border-gray-600 text-brand-600 focus:ring-brand-500"
              />
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                Shared
              </span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={dashboardForm.is_wallboard}
                onChange={(e) => setDashboardForm({ ...dashboardForm, is_wallboard: e.target.checked })}
                className="rounded border-gray-300 dark:border-gray-600 text-brand-600 focus:ring-brand-500"
              />
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                Wallboard
              </span>
            </label>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Layout (JSON)
            </label>
            <textarea
              value={dashboardForm.layout}
              onChange={(e) => setDashboardForm({ ...dashboardForm, layout: e.target.value })}
              className="input w-full font-mono text-sm"
              rows={4}
              placeholder='{"columns": 12, "rowHeight": 80}'
            />
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={() => {
                setShowCreateForm(false)
                setEditingDashboardId(null)
                setDashboardForm(emptyDashboardForm)
              }}
              className="btn"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createDashboardMutation.isPending || updateDashboardMutation.isPending}
              className="btn-primary"
            >
              {createDashboardMutation.isPending || updateDashboardMutation.isPending
                ? 'Saving...'
                : editingDashboardId
                  ? 'Update Dashboard'
                  : 'Create Dashboard'}
            </button>
          </div>
        </form>
      )}

      {/* Dashboards List */}
      {dashboardList.length === 0 ? (
        <div className="card p-8 text-center">
          <LayoutDashboard size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-3" />
          <p className="text-gray-500 dark:text-gray-400">No dashboards configured yet</p>
          <p className="text-sm text-gray-400 dark:text-gray-500 mt-1">
            Create a dashboard to get started
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {dashboardList.map((dashboard) => {
            const isExpanded = expandedDashboardId === dashboard.id
            const gadgets = getGadgetsForDashboard(dashboard.id)

            return (
              <div key={dashboard.id} className="card overflow-hidden">
                {/* Dashboard Row */}
                <div className="p-4 flex items-center justify-between">
                  <div className="flex items-center gap-3 flex-1 min-w-0">
                    <button
                      onClick={() => toggleExpand(dashboard.id)}
                      className="btn-icon flex-shrink-0"
                    >
                      <ChevronDown
                        size={16}
                        className={cn(
                          'transition-transform',
                          isExpanded && 'rotate-180'
                        )}
                      />
                    </button>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className="font-medium text-gray-900 dark:text-white">
                          {dashboard.name}
                        </p>
                        {dashboard.is_shared && (
                          <span className="px-2 py-0.5 text-xs rounded font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 flex items-center gap-1">
                            <Share2 size={10} />
                            shared
                          </span>
                        )}
                        {dashboard.is_wallboard && (
                          <span className="px-2 py-0.5 text-xs rounded font-medium bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400 flex items-center gap-1">
                            <Eye size={10} />
                            wallboard
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 text-xs text-gray-400 dark:text-gray-500 mt-1">
                        {dashboard.description && (
                          <span className="truncate max-w-xs">{dashboard.description}</span>
                        )}
                        {dashboard.owner_name && (
                          <span>Owner: {dashboard.owner_name}</span>
                        )}
                        <span className="flex items-center gap-1">
                          <Grid3X3 size={12} />
                          {gadgets.length} gadgets
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 flex-shrink-0">
                    <button
                      onClick={() => startEditDashboard(dashboard)}
                      className="btn-icon"
                      title="Edit dashboard"
                    >
                      <Edit2 size={16} />
                    </button>
                    <button
                      onClick={() => handleDeleteDashboard(dashboard)}
                      className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                      title="Delete dashboard"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </div>

                {/* Expanded: Gadgets */}
                {isExpanded && (
                  <div className="border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 p-4">
                    {/* Grid Preview */}
                    {gadgets.length > 0 && (
                      <div className="mb-4">
                        <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2 uppercase tracking-wider">
                          Grid Preview
                        </h4>
                        <div className="relative bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-2 overflow-hidden"
                          style={{ height: '120px' }}
                        >
                          {gadgets.map((gadget) => {
                            const x = (gadget.position_x || 0) / 12 * 100
                            const w = (gadget.width || 6) / 12 * 100
                            const y = (gadget.position_y || 0) * 30
                            const h = (gadget.height || 2) * 30
                            const category = GADGET_CATEGORY_MAP[gadget.gadget_type] || 'Other'
                            const badgeClass = GADGET_CATEGORY_BADGE[category] || GADGET_CATEGORY_BADGE.Other
                            return (
                              <div
                                key={gadget.id}
                                className={cn(
                                  'absolute rounded text-xs flex items-center justify-center font-medium border',
                                  badgeClass
                                )}
                                style={{
                                  left: `${x}%`,
                                  top: `${y}px`,
                                  width: `${w}%`,
                                  height: `${Math.min(h, 60)}px`,
                                }}
                                title={`${gadget.title} (${gadget.gadget_type})`}
                              >
                                <span className="truncate px-1">
                                  {gadget.title || gadget.gadget_type}
                                </span>
                              </div>
                            )
                          })}
                        </div>
                      </div>
                    )}

                    <div className="flex items-center justify-between mb-3">
                      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 flex items-center gap-2">
                        <BarChart3 size={14} />
                        Gadgets ({gadgets.length})
                      </h3>
                      <button
                        onClick={() => {
                          setGadgetForm(emptyGadgetForm)
                          setShowAddGadget(showAddGadget === dashboard.id ? null : dashboard.id)
                        }}
                        className="btn-primary text-xs flex items-center gap-1 px-3 py-1.5"
                      >
                        <Plus size={14} />
                        Add Gadget
                      </button>
                    </div>

                    {/* Add Gadget Form */}
                    {showAddGadget === dashboard.id && (
                      <form
                        onSubmit={(e) => handleAddGadgetSubmit(e, dashboard.id)}
                        className="mb-4 p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 space-y-3"
                      >
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Title *
                            </label>
                            <input
                              type="text"
                              value={gadgetForm.title}
                              onChange={(e) => setGadgetForm({ ...gadgetForm, title: e.target.value })}
                              className="input w-full"
                              placeholder="e.g., Sprint Burndown, My Tasks"
                              required
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Gadget Type
                            </label>
                            <select
                              value={gadgetForm.gadget_type}
                              onChange={(e) => setGadgetForm({ ...gadgetForm, gadget_type: e.target.value })}
                              className="input w-full"
                            >
                              {Object.entries(gadgetGroups).map(([category, types]) => (
                                <optgroup key={category} label={category}>
                                  {types.map((gt) => (
                                    <option key={gt.value} value={gt.value}>
                                      {gt.label}
                                    </option>
                                  ))}
                                </optgroup>
                              ))}
                            </select>
                          </div>
                        </div>

                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Position X (0-11)
                            </label>
                            <input
                              type="number"
                              value={gadgetForm.position_x}
                              onChange={(e) => setGadgetForm({ ...gadgetForm, position_x: e.target.value })}
                              className="input w-full"
                              min={0}
                              max={11}
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Position Y
                            </label>
                            <input
                              type="number"
                              value={gadgetForm.position_y}
                              onChange={(e) => setGadgetForm({ ...gadgetForm, position_y: e.target.value })}
                              className="input w-full"
                              min={0}
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Width (1-12)
                            </label>
                            <input
                              type="number"
                              value={gadgetForm.width}
                              onChange={(e) => setGadgetForm({ ...gadgetForm, width: e.target.value })}
                              className="input w-full"
                              min={1}
                              max={12}
                            />
                          </div>
                          <div>
                            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                              Height (1-4)
                            </label>
                            <input
                              type="number"
                              value={gadgetForm.height}
                              onChange={(e) => setGadgetForm({ ...gadgetForm, height: e.target.value })}
                              className="input w-full"
                              min={1}
                              max={4}
                            />
                          </div>
                        </div>

                        <div>
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            Config (JSON)
                          </label>
                          <textarea
                            value={gadgetForm.config}
                            onChange={(e) => setGadgetForm({ ...gadgetForm, config: e.target.value })}
                            className="input w-full font-mono text-sm"
                            rows={3}
                            placeholder='{"filterId": "abc-123", "maxResults": 25}'
                          />
                        </div>

                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => {
                              setShowAddGadget(null)
                              setGadgetForm(emptyGadgetForm)
                            }}
                            className="btn text-xs"
                          >
                            Cancel
                          </button>
                          <button
                            type="submit"
                            disabled={createGadgetMutation.isPending}
                            className="btn-primary text-xs"
                          >
                            {createGadgetMutation.isPending ? 'Adding...' : 'Add Gadget'}
                          </button>
                        </div>
                      </form>
                    )}

                    {/* Gadgets List */}
                    {gadgets.length === 0 ? (
                      <p className="text-sm text-gray-400 dark:text-gray-500 text-center py-4">
                        No gadgets on this dashboard
                      </p>
                    ) : (
                      <div className="space-y-2">
                        {gadgets.map((gadget) => {
                          const category = GADGET_CATEGORY_MAP[gadget.gadget_type] || 'Other'
                          const badgeClass = GADGET_CATEGORY_BADGE[category] || GADGET_CATEGORY_BADGE.Other
                          return (
                            <div
                              key={gadget.id}
                              className="flex items-center justify-between p-3 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700"
                            >
                              <div className="min-w-0 flex-1">
                                <div className="flex items-center gap-2 flex-wrap">
                                  <BarChart3 size={14} className="text-gray-400 flex-shrink-0" />
                                  <p className="font-medium text-sm text-gray-900 dark:text-white">
                                    {gadget.title || GADGET_LABEL_MAP[gadget.gadget_type] || gadget.gadget_type}
                                  </p>
                                  <span className={cn(
                                    'px-2 py-0.5 text-xs rounded',
                                    badgeClass
                                  )}>
                                    {GADGET_LABEL_MAP[gadget.gadget_type] || gadget.gadget_type}
                                  </span>
                                  <span className="text-xs text-gray-400">
                                    {category}
                                  </span>
                                </div>
                                <div className="flex items-center gap-3 text-xs text-gray-400 dark:text-gray-500 mt-1 ml-6">
                                  <span>
                                    pos: ({gadget.position_x}, {gadget.position_y})
                                  </span>
                                  <span>
                                    size: {gadget.width}x{gadget.height}
                                  </span>
                                </div>
                              </div>
                              <button
                                onClick={() => handleDeleteGadget(gadget.id)}
                                className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30 flex-shrink-0"
                                title="Delete gadget"
                              >
                                <Trash2 size={14} />
                              </button>
                            </div>
                          )
                        })}
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
