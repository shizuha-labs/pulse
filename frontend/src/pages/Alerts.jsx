import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bell, BellOff, CheckCircle, RefreshCw, Settings, Key, Copy, Trash2, Plus, ExternalLink, ChevronDown, ChevronUp, Check, X, Square, CheckSquare } from 'lucide-react'
import { itemsApi, alertsApi, webhookKeysApi, projectsApi } from '../services/api'
import ItemCard from '../components/ItemCard'
import AlertDetailModal from '../components/AlertDetailModal'
import EmptyState from '../components/EmptyState'
import { PageLoader } from '../components/LoadingSpinner'
import { useMultiSelect } from '../hooks/useMultiSelect'
import { cn } from '../utils/cn'
import { formatOrganizationName, formatScopedTeamName, formatTeamName } from '../utils/teamLabels'

const STATUS_FILTERS = [
  { value: 'firing', label: 'Firing', color: 'red' },
  { value: 'acknowledged', label: 'Acknowledged', color: 'yellow' },
  { value: 'resolved', label: 'Resolved', color: 'green' },
  { value: '', label: 'All', color: 'gray' },
]

const SEVERITY_FILTERS = [
  { value: '', label: 'All Severities' },
  { value: 'critical', label: 'Critical' },
  { value: 'error', label: 'Error' },
  { value: 'warning', label: 'Warning' },
  { value: 'info', label: 'Info' },
]

export default function Alerts() {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState('firing')
  const [severityFilter, setSeverityFilter] = useState('')
  const [silenceModal, setSilenceModal] = useState(null)
  const [silenceDuration, setSilenceDuration] = useState(60)
  const [showWebhookConfig, setShowWebhookConfig] = useState(false)
  const [createWebhookModal, setCreateWebhookModal] = useState(false)
  const [newWebhookName, setNewWebhookName] = useState('')
  const [newWebhookType, setNewWebhookType] = useState('grafana')
  const [newWebhookProject, setNewWebhookProject] = useState('')
  const [newWebhookGroup, setNewWebhookGroup] = useState('')
  const [copiedUrl, setCopiedUrl] = useState(null)
  const [selectedAlert, setSelectedAlert] = useState(null)
  const [selectionMode, setSelectionMode] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)

  // Build query params
  const queryParams = { mode: 'alert' }
  if (statusFilter) queryParams.status = statusFilter
  if (severityFilter) queryParams.severity = severityFilter

  // Fetch alerts
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['alerts', queryParams],
    queryFn: () => itemsApi.list(queryParams).then((res) => res.data),
    refetchInterval: 30000, // Auto-refresh every 30 seconds
  })

  const alerts = data?.results || data || []

  // Multi-select hook
  const {
    toggle,
    toggleWithShift,
    selectAll,
    deselectAll,
    isSelected,
    selectedIds,
    selectedCount,
    allSelected,
    someSelected,
  } = useMultiSelect(alerts)

  const invalidateAlerts = () => {
    queryClient.invalidateQueries({ queryKey: ['alerts'] })
    queryClient.invalidateQueries({ queryKey: ['pending'] })
    queryClient.invalidateQueries({ queryKey: ['statistics'] })
  }

  // Acknowledge mutation
  const acknowledgeMutation = useMutation({
    mutationFn: (item) => itemsApi.acknowledge(item.id),
    onSuccess: invalidateAlerts,
  })

  // Resolve mutation
  const resolveMutation = useMutation({
    mutationFn: (item) => itemsApi.resolve(item.id),
    onSuccess: invalidateAlerts,
  })

  // Silence mutation
  const silenceMutation = useMutation({
    mutationFn: ({ id, duration }) =>
      itemsApi.silence(id, { duration_minutes: duration }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
      setSilenceModal(null)
    },
  })

  // Bulk acknowledge mutation
  const bulkAcknowledgeMutation = useMutation({
    mutationFn: (ids) => alertsApi.bulkAcknowledge({ item_ids: ids }),
    onSuccess: () => {
      invalidateAlerts()
      deselectAll()
    },
  })

  // Bulk resolve mutation
  const bulkResolveMutation = useMutation({
    mutationFn: (ids) => alertsApi.bulkResolve({ item_ids: ids }),
    onSuccess: () => {
      invalidateAlerts()
      deselectAll()
    },
  })

  // Single delete mutation
  const deleteMutation = useMutation({
    mutationFn: (item) => itemsApi.delete(item.id),
    onSuccess: () => {
      invalidateAlerts()
      setSelectedAlert(null)
    },
  })

  // Bulk delete mutation
  const bulkDeleteMutation = useMutation({
    mutationFn: (ids) => alertsApi.bulkDelete(ids),
    onSuccess: () => {
      invalidateAlerts()
      deselectAll()
      setShowDeleteConfirm(false)
    },
  })

  // Webhook keys query
  const { data: webhookKeysData, isLoading: webhookKeysLoading } = useQuery({
    queryKey: ['webhookKeys'],
    queryFn: () => webhookKeysApi.list().then((res) => res.data),
    enabled: showWebhookConfig,
  })

  // Projects for routing alerts (loaded only when the create-webhook modal opens)
  const { data: projectsData } = useQuery({
    queryKey: ['projects', 'for-webhook-routing'],
    queryFn: () => projectsApi.list({ page_size: 100 }).then((res) => res.data),
    enabled: createWebhookModal,
  })

  const { data: webhookTeamsData, isLoading: webhookTeamsLoading } = useQuery({
    queryKey: ['teams', 'for-webhook-routing', newWebhookProject],
    queryFn: () => itemsApi.teams({ project_id: newWebhookProject }).then((res) => res.data),
    enabled: createWebhookModal && Boolean(newWebhookProject),
  })

  // Create webhook key mutation
  const createWebhookMutation = useMutation({
    mutationFn: (data) => webhookKeysApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['webhookKeys'] })
      setCreateWebhookModal(false)
      setNewWebhookName('')
      setNewWebhookType('grafana')
      setNewWebhookProject('')
      setNewWebhookGroup('')
    },
  })

  // Delete webhook key mutation
  const deleteWebhookMutation = useMutation({
    mutationFn: (id) => webhookKeysApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['webhookKeys'] })
    },
  })

  const handleCreateWebhook = (e) => {
    e.preventDefault()
    if (!newWebhookName.trim()) return
    const payload = {
      name: newWebhookName.trim(),
      integration_type: newWebhookType,
    }
    if (newWebhookProject) payload.default_project = parseInt(newWebhookProject, 10)
    if (newWebhookGroup.trim()) payload.default_assignment_group = newWebhookGroup.trim()
    createWebhookMutation.mutate(payload)
  }

  const copyToClipboard = (text, keyId) => {
    navigator.clipboard.writeText(text)
    setCopiedUrl(keyId)
    setTimeout(() => setCopiedUrl(null), 2000)
  }

  const handleSilence = (e) => {
    e.preventDefault()
    if (!silenceModal) return
    silenceMutation.mutate({ id: silenceModal.id, duration: silenceDuration })
  }

  const toggleSelectionMode = () => {
    if (selectionMode) {
      deselectAll()
      setShowDeleteConfirm(false)
    }
    setSelectionMode(!selectionMode)
  }

  // Replace backend-internal base URL with the browser's origin
  const fixWebhookUrl = (url) => {
    if (!url) return url
    try {
      const parsed = new URL(url)
      const origin = window.location.origin
      return `${origin}${parsed.pathname}`
    } catch {
      return url
    }
  }

  const isBulkLoading = bulkAcknowledgeMutation.isPending || bulkResolveMutation.isPending || bulkDeleteMutation.isPending

  if (isLoading) {
    return <PageLoader />
  }

  const firingCount = alerts.filter((a) => a.status === 'firing').length

  // Group alerts by severity for firing view
  const alertsBySeverity = {
    critical: alerts.filter((a) => a.severity === 'critical'),
    error: alerts.filter((a) => a.severity === 'error'),
    warning: alerts.filter((a) => a.severity === 'warning'),
    info: alerts.filter((a) => a.severity === 'info'),
  }

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Alerts</h1>
          <p className="text-gray-500 dark:text-gray-400">Monitor and manage system alerts</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowWebhookConfig(!showWebhookConfig)}
            className={cn(
              'btn-icon',
              showWebhookConfig && 'bg-indigo-100 text-indigo-600 dark:bg-indigo-900/50 dark:text-indigo-400'
            )}
            title="Webhook Configuration"
          >
            <Settings size={20} />
          </button>
          <button
            onClick={() => refetch()}
            className="btn-icon"
            title="Refresh"
          >
            <RefreshCw size={20} />
          </button>
          {alerts.length > 0 && (
            <button
              onClick={toggleSelectionMode}
              className={cn(
                'btn-secondary btn-sm',
                selectionMode && 'bg-brand-100 text-brand-700 dark:bg-brand-900/50 dark:text-brand-300'
              )}
            >
              {selectionMode ? 'Cancel' : 'Select'}
            </button>
          )}
        </div>
      </div>

      {/* Webhook Configuration Section */}
      {showWebhookConfig && (
        <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-700 p-4 space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Key size={20} className="text-indigo-500" />
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Webhook Integration</h2>
            </div>
            <button
              onClick={() => setCreateWebhookModal(true)}
              className="btn-primary btn-sm flex items-center gap-1"
            >
              <Plus size={16} />
              New Webhook
            </button>
          </div>

          <p className="text-sm text-gray-600 dark:text-gray-400">
            Connect external alerting systems like Grafana or Prometheus Alertmanager to push alerts directly to Shizuha Pulse.
          </p>

          {webhookKeysLoading ? (
            <div className="py-4 text-center text-gray-500">Loading webhooks...</div>
          ) : webhookKeysData?.results?.length > 0 ? (
            <div className="space-y-3">
              {webhookKeysData.results.map((webhookKey) => (
                <div
                  key={webhookKey.id}
                  className="bg-gray-50 dark:bg-gray-800 rounded-lg p-4 space-y-3"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-gray-900 dark:text-gray-100">
                        {webhookKey.name}
                      </span>
                      <span className={cn(
                        'text-xs px-2 py-0.5 rounded-full',
                        webhookKey.integration_type === 'grafana'
                          ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300'
                          : webhookKey.integration_type === 'prometheus'
                          ? 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300'
                          : 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
                      )}>
                        {webhookKey.integration_type}
                      </span>
                      {!webhookKey.is_active && (
                        <span className="text-xs px-2 py-0.5 rounded-full bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-400">
                          Inactive
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => deleteWebhookMutation.mutate(webhookKey.id)}
                      className="btn-icon text-red-500 hover:bg-red-100 dark:hover:bg-red-900/30"
                      title="Delete webhook"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>

                  {webhookKey.webhook_urls && (
                    <div className="space-y-2">
                      <div className="text-xs text-gray-500 dark:text-gray-400">
                        Webhook URL ({webhookKey.integration_type}):
                      </div>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 text-xs bg-gray-100 dark:bg-gray-700 px-3 py-2 rounded font-mono overflow-x-auto">
                          {fixWebhookUrl(webhookKey.webhook_urls[webhookKey.integration_type])}
                        </code>
                        <button
                          onClick={() => copyToClipboard(
                            fixWebhookUrl(webhookKey.webhook_urls[webhookKey.integration_type]),
                            webhookKey.id
                          )}
                          className="btn-icon shrink-0"
                          title="Copy URL"
                        >
                          {copiedUrl === webhookKey.id ? (
                            <CheckCircle size={16} className="text-green-500" />
                          ) : (
                            <Copy size={16} />
                          )}
                        </button>
                      </div>
                    </div>
                  )}

                  {(webhookKey.default_project || webhookKey.default_assignment_group) && (
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="text-gray-500 dark:text-gray-400">Routes to:</span>
                      {webhookKey.default_project && (
                        <span className="px-2 py-0.5 rounded bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                          project #{webhookKey.default_project}
                        </span>
                      )}
                      {webhookKey.default_assignment_group && (
                        <span className="px-2 py-0.5 rounded bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                          Team: {formatTeamName(webhookKey.default_assignment_group)}
                        </span>
                      )}
                    </div>
                  )}

                  <div className="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
                    <span>Alerts received: {webhookKey.alerts_received}</span>
                    {webhookKey.last_used_at && (
                      <span>Last used: {new Date(webhookKey.last_used_at).toLocaleString()}</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="py-6 text-center">
              <Key size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
              <p className="text-sm text-gray-500 dark:text-gray-400">
                No webhook keys configured yet. Create one to start receiving alerts from external systems.
              </p>
            </div>
          )}

          {/* Setup Instructions */}
          <details className="border-t border-gray-200 dark:border-gray-700 pt-4">
            <summary className="flex items-center gap-2 cursor-pointer text-sm font-medium text-gray-700 dark:text-gray-300">
              <ChevronDown size={16} className="details-chevron" />
              Grafana Setup Instructions
            </summary>
            <div className="mt-3 text-sm text-gray-600 dark:text-gray-400 space-y-2 pl-6">
              <ol className="list-decimal space-y-2">
                <li>In Grafana, go to <strong>Alerting → Contact points</strong></li>
                <li>Click <strong>Add contact point</strong></li>
                <li>Select <strong>Webhook</strong> as the integration type</li>
                <li>Paste your webhook URL from above</li>
                <li>Set HTTP Method to <strong>POST</strong></li>
                <li>Leave username/password empty (authentication is via the URL key)</li>
                <li>Click <strong>Test</strong> to verify, then <strong>Save</strong></li>
              </ol>
              <p className="mt-3">
                <a
                  href="https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-indigo-600 dark:text-indigo-400 hover:underline inline-flex items-center gap-1"
                >
                  Grafana Documentation <ExternalLink size={14} />
                </a>
              </p>
            </div>
          </details>
        </div>
      )}

      {/* Status Tabs */}
      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((filter) => {
          const count =
            filter.value === 'firing'
              ? firingCount
              : filter.value === 'acknowledged'
              ? alerts.filter((a) => a.status === 'acknowledged').length
              : null

          return (
            <button
              key={filter.value}
              onClick={() => setStatusFilter(filter.value)}
              className={cn(
                'px-3 py-1.5 rounded-lg text-sm font-medium transition-colors flex items-center gap-2',
                statusFilter === filter.value
                  ? filter.color === 'red'
                    ? 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300'
                    : filter.color === 'yellow'
                    ? 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300'
                    : filter.color === 'green'
                    ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
                    : 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
              )}
            >
              {filter.label}
              {count !== null && count > 0 && (
                <span
                  className={cn(
                    'text-xs px-1.5 py-0.5 rounded-full',
                    statusFilter === filter.value
                      ? 'bg-white/50 dark:bg-black/30'
                      : 'bg-gray-200 dark:bg-gray-700'
                  )}
                >
                  {count}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {/* Severity Filter */}
      <div className="flex items-center gap-2">
        <select
          value={severityFilter}
          onChange={(e) => setSeverityFilter(e.target.value)}
          className="input w-auto"
        >
          {SEVERITY_FILTERS.map((filter) => (
            <option key={filter.value} value={filter.value}>
              {filter.label}
            </option>
          ))}
        </select>
      </div>

      {/* Selection Toolbar */}
      {selectionMode && selectedCount > 0 && (
        <div className="sticky top-0 z-40 bg-brand-50 dark:bg-brand-950 border border-brand-200 dark:border-brand-800 rounded-lg p-3 shadow-sm">
          <div className="flex flex-wrap items-center gap-3">
            {/* Selection info */}
            <div className="flex items-center gap-2">
              <button
                onClick={allSelected ? deselectAll : selectAll}
                className="p-1 rounded hover:bg-brand-100 dark:hover:bg-brand-900"
                title={allSelected ? 'Deselect all' : 'Select all'}
              >
                {allSelected ? (
                  <CheckSquare size={18} className="text-brand-600 dark:text-brand-400" />
                ) : someSelected ? (
                  <div className="relative">
                    <Square size={18} className="text-brand-600 dark:text-brand-400" />
                    <div className="absolute inset-0 flex items-center justify-center">
                      <div className="w-2 h-0.5 bg-brand-600 dark:bg-brand-400" />
                    </div>
                  </div>
                ) : (
                  <Square size={18} className="text-gray-400" />
                )}
              </button>
              <span className="text-sm font-medium text-brand-700 dark:text-brand-300">
                {selectedCount} of {alerts.length} selected
              </span>
            </div>

            {/* Divider */}
            <div className="h-5 w-px bg-brand-200 dark:bg-brand-700" />

            {/* Actions */}
            <div className="flex items-center gap-2">
              {/* Acknowledge */}
              <button
                onClick={() => bulkAcknowledgeMutation.mutate(selectedIds)}
                disabled={isBulkLoading}
                className={cn(
                  'flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg transition-colors',
                  'bg-yellow-500 text-white hover:bg-yellow-600',
                  'disabled:opacity-50 disabled:cursor-not-allowed'
                )}
              >
                <Bell size={14} />
                <span>Acknowledge</span>
              </button>

              {/* Resolve */}
              <button
                onClick={() => bulkResolveMutation.mutate(selectedIds)}
                disabled={isBulkLoading}
                className={cn(
                  'flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg transition-colors',
                  'bg-green-600 text-white hover:bg-green-700',
                  'disabled:opacity-50 disabled:cursor-not-allowed'
                )}
              >
                <CheckCircle size={14} />
                <span>Resolve</span>
              </button>

              {/* Delete */}
              {!showDeleteConfirm ? (
                <button
                  onClick={() => setShowDeleteConfirm(true)}
                  disabled={isBulkLoading}
                  className={cn(
                    'flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg transition-colors',
                    'text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30',
                    'disabled:opacity-50 disabled:cursor-not-allowed'
                  )}
                >
                  <Trash2 size={14} />
                  <span>Delete</span>
                </button>
              ) : (
                <div className="flex items-center gap-2 bg-red-50 dark:bg-red-900/30 px-3 py-1.5 rounded-lg">
                  <span className="text-sm text-red-700 dark:text-red-300">
                    Delete {selectedCount} alerts?
                  </span>
                  <button
                    onClick={() => bulkDeleteMutation.mutate(selectedIds)}
                    disabled={isBulkLoading}
                    className="px-2 py-1 text-xs font-medium text-white bg-red-600 rounded hover:bg-red-700"
                  >
                    Yes
                  </button>
                  <button
                    onClick={() => setShowDeleteConfirm(false)}
                    className="px-2 py-1 text-xs font-medium text-red-700 dark:text-red-300 hover:underline"
                  >
                    No
                  </button>
                </div>
              )}
            </div>

            {/* Cancel */}
            <div className="ml-auto">
              <button
                onClick={toggleSelectionMode}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200"
              >
                <X size={14} />
                <span>Cancel</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Alert List */}
      {alerts.length > 0 ? (
        <div className="space-y-3">
          {alerts.map((alert) => (
            <ItemCard
              key={alert.id}
              item={alert}
              selectable={selectionMode}
              isSelected={isSelected(alert.id)}
              onToggleSelect={(id, shiftKey) => toggleWithShift(id, shiftKey)}
              onClick={(item) => setSelectedAlert(item)}
              onAcknowledge={
                alert.status === 'firing'
                  ? (item) => acknowledgeMutation.mutate(item)
                  : undefined
              }
              onResolve={
                alert.status === 'firing' || alert.status === 'acknowledged'
                  ? (item) => resolveMutation.mutate(item)
                  : undefined
              }
              onSilence={
                alert.status === 'firing'
                  ? (item) => setSilenceModal(item)
                  : undefined
              }
            />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={AlertTriangle}
          title="No alerts found"
          description={
            statusFilter === 'firing'
              ? 'No active alerts - everything is running smoothly!'
              : 'No alerts match the selected filters'
          }
        />
      )}

      {/* Silence Modal */}
      {silenceModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => setSilenceModal(null)}
          />
          <div className="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-md p-6">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">Silence Alert</h2>
            <p className="text-gray-500 dark:text-gray-400 mb-4">
              Silence notifications for "{silenceModal.title}"
            </p>
            <form onSubmit={handleSilence} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Duration
                </label>
                <select
                  value={silenceDuration}
                  onChange={(e) => setSilenceDuration(Number(e.target.value))}
                  className="input"
                >
                  <option value={15}>15 minutes</option>
                  <option value={30}>30 minutes</option>
                  <option value={60}>1 hour</option>
                  <option value={120}>2 hours</option>
                  <option value={240}>4 hours</option>
                  <option value={480}>8 hours</option>
                  <option value={1440}>24 hours</option>
                </select>
              </div>

              <div className="flex gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setSilenceModal(null)}
                  className="btn-secondary flex-1"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={silenceMutation.isPending}
                  className="btn-primary flex-1"
                >
                  {silenceMutation.isPending ? 'Silencing...' : 'Silence'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Create Webhook Modal */}
      {createWebhookModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => setCreateWebhookModal(false)}
          />
          <div className="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-md p-6">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">Create Webhook Key</h2>
            <p className="text-gray-500 dark:text-gray-400 mb-4">
              Generate a new webhook URL for external alert integration
            </p>
            <form onSubmit={handleCreateWebhook} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Name
                </label>
                <input
                  type="text"
                  value={newWebhookName}
                  onChange={(e) => setNewWebhookName(e.target.value)}
                  placeholder="e.g., Production Grafana"
                  className="input"
                  autoFocus
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Integration Type
                </label>
                <select
                  value={newWebhookType}
                  onChange={(e) => setNewWebhookType(e.target.value)}
                  className="input"
                >
                  <option value="grafana">Grafana</option>
                  <option value="prometheus">Prometheus Alertmanager</option>
                  <option value="generic">Generic Webhook</option>
                </select>
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  {newWebhookType === 'grafana' && 'Native Grafana alerting webhook format'}
                  {newWebhookType === 'prometheus' && 'Prometheus Alertmanager webhook format'}
                  {newWebhookType === 'generic' && 'Standard JSON format for custom integrations'}
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Route to Project <span className="text-gray-400 font-normal">(optional)</span>
                </label>
                <select
                  value={newWebhookProject}
                  onChange={(e) => {
                    setNewWebhookProject(e.target.value)
                    setNewWebhookGroup('')
                  }}
                  className="input"
                >
                  <option value="">— Unassigned bucket —</option>
                  {projectsData?.results?.map((p) => (
                    <option key={p.id} value={p.id}>
                      {[formatOrganizationName(p), `${p.key} — ${p.name}`].filter(Boolean).join(' › ')}
                    </option>
                  ))}
                </select>
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Incoming alerts land in this project. Leave blank to ingest without a project.
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Team <span className="text-gray-400 font-normal">(optional)</span>
                </label>
                <select
                  value={newWebhookGroup}
                  onChange={(e) => setNewWebhookGroup(e.target.value)}
                  disabled={!newWebhookProject || webhookTeamsLoading}
                  className="input"
                >
                  <option value="">
                    {!newWebhookProject
                      ? '— Select a project first —'
                      : webhookTeamsLoading
                        ? '— Loading teams —'
                        : '— No default team —'}
                  </option>
                  {(webhookTeamsData?.teams || []).map(team => (
                    <option
                      key={team.canonical_key || team.team_id}
                      value={team.canonical_name || team.team_key}
                    >
                      {formatScopedTeamName(team)}
                    </option>
                  ))}
                </select>
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Incoming alerts are routed only to a team owned by the selected project organization.
                </p>
              </div>

              <div className="flex gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setCreateWebhookModal(false)}
                  className="btn-secondary flex-1"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={createWebhookMutation.isPending || !newWebhookName.trim()}
                  className="btn-primary flex-1"
                >
                  {createWebhookMutation.isPending ? 'Creating...' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Alert Detail Modal */}
      {selectedAlert && (
        <AlertDetailModal
          alert={selectedAlert}
          onClose={() => setSelectedAlert(null)}
          onAcknowledge={
            selectedAlert.status === 'firing'
              ? (item) => acknowledgeMutation.mutate(item)
              : undefined
          }
          onResolve={
            selectedAlert.status === 'firing' || selectedAlert.status === 'acknowledged'
              ? (item) => resolveMutation.mutate(item)
              : undefined
          }
          onSilence={
            selectedAlert.status === 'firing'
              ? (item) => setSilenceModal(item)
              : undefined
          }
          onDelete={(item) => deleteMutation.mutate(item)}
        />
      )}
    </div>
  )
}
