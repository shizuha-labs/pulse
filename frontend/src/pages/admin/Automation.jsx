import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Zap, Play, Pause, Plus, Edit2, Trash2, Clock,
  CheckCircle, XCircle, ChevronDown, ChevronRight, Activity,
} from 'lucide-react'
import { automationRulesApi, automationRuleLogsApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner, { PageLoader } from '../../components/LoadingSpinner'

const TRIGGER_TYPES = [
  { value: 'issue_created', label: 'Issue Created' },
  { value: 'issue_updated', label: 'Issue Updated' },
  { value: 'issue_transitioned', label: 'Issue Transitioned' },
  { value: 'issue_assigned', label: 'Issue Assigned' },
  { value: 'comment_added', label: 'Comment Added' },
  { value: 'worklog_added', label: 'Worklog Added' },
  { value: 'sprint_started', label: 'Sprint Started' },
  { value: 'sprint_completed', label: 'Sprint Completed' },
  { value: 'version_released', label: 'Version Released' },
  { value: 'schedule', label: 'Schedule' },
  { value: 'field_value_changed', label: 'Field Value Changed' },
  { value: 'issue_linked', label: 'Issue Linked' },
  { value: 'sla_breached', label: 'SLA Breached' },
]

const TABS = [
  { key: 'rules', label: 'Automation Rules', icon: Zap },
  { key: 'logs', label: 'Execution Logs', icon: Activity },
]

function getTriggerBadgeClasses(triggerType) {
  if (triggerType?.startsWith('issue_')) {
    return 'bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-300'
  }
  if (triggerType === 'comment_added' || triggerType === 'worklog_added') {
    return 'bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-300'
  }
  if (triggerType?.startsWith('sprint_') || triggerType === 'version_released') {
    return 'bg-purple-100 text-purple-800 dark:bg-purple-900/50 dark:text-purple-300'
  }
  if (triggerType === 'schedule') {
    return 'bg-orange-100 text-orange-800 dark:bg-orange-900/50 dark:text-orange-300'
  }
  if (triggerType === 'sla_breached') {
    return 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-300'
  }
  if (triggerType === 'field_value_changed' || triggerType === 'issue_linked') {
    return 'bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-300'
  }
  return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300'
}

function getStatusBadgeClasses(status) {
  if (status === 'success') {
    return 'bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-300'
  }
  if (status === 'failure') {
    return 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-300'
  }
  return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
}

function formatRelativeTime(dateString) {
  if (!dateString) return 'never'
  const date = new Date(dateString)
  const now = new Date()
  const diffMs = now - date
  const diffSec = Math.floor(diffMs / 1000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHour = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHour / 24)

  if (diffSec < 60) return 'just now'
  if (diffMin < 60) return `${diffMin} minute${diffMin !== 1 ? 's' : ''} ago`
  if (diffHour < 24) return `${diffHour} hour${diffHour !== 1 ? 's' : ''} ago`
  if (diffDay < 30) return `${diffDay} day${diffDay !== 1 ? 's' : ''} ago`
  return date.toLocaleDateString()
}

function formatTriggerLabel(triggerType) {
  const entry = TRIGGER_TYPES.find((t) => t.value === triggerType)
  return entry ? entry.label : triggerType?.replace(/_/g, ' ') || 'Unknown'
}

const EMPTY_FORM = {
  name: '',
  description: '',
  project: '',
  trigger_type: 'issue_created',
  trigger_config: '{}',
  conditions: '[]',
  actions: '[]',
  is_enabled: true,
}

function isValidJson(str) {
  try {
    JSON.parse(str)
    return true
  } catch {
    return false
  }
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function Automation() {
  const [activeTab, setActiveTab] = useState('rules')
  const [showForm, setShowForm] = useState(false)
  const [editingRule, setEditingRule] = useState(null)
  const [formData, setFormData] = useState(EMPTY_FORM)
  const [formErrors, setFormErrors] = useState({})
  const [deleteConfirmId, setDeleteConfirmId] = useState(null)
  const [logStatusFilter, setLogStatusFilter] = useState('all')
  const [expandedLogId, setExpandedLogId] = useState(null)
  const [logsPage, setLogsPage] = useState(1)

  const queryClient = useQueryClient()

  // ---- Rules queries / mutations ----

  const { data: rules = [], isLoading: rulesLoading, error: rulesError } = useQuery({
    queryKey: ['automationRules'],
    queryFn: () => automationRulesApi.list().then((r) => r.data.results || r.data || []),
  })

  const toggleMutation = useMutation({
    mutationFn: (id) => automationRulesApi.toggle(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['automationRules'] }),
  })

  const createMutation = useMutation({
    mutationFn: (data) => automationRulesApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['automationRules'] })
      resetForm()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }) => automationRulesApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['automationRules'] })
      resetForm()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => automationRulesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['automationRules'] })
      setDeleteConfirmId(null)
    },
  })

  // ---- Logs queries ----

  const logsParams = { page: logsPage }
  if (logStatusFilter !== 'all') logsParams.status = logStatusFilter

  const { data: logsData, isLoading: logsLoading, error: logsError } = useQuery({
    queryKey: ['automationRuleLogs', logStatusFilter, logsPage],
    queryFn: () => automationRuleLogsApi.list(logsParams).then((r) => r.data),
    enabled: activeTab === 'logs',
  })

  const logs = logsData?.results || logsData || []
  const logsCount = logsData?.count || 0
  const logsPageSize = 20
  const logsTotalPages = Math.max(1, Math.ceil(logsCount / logsPageSize))

  // ---- Form helpers ----

  function resetForm() {
    setShowForm(false)
    setEditingRule(null)
    setFormData(EMPTY_FORM)
    setFormErrors({})
  }

  function openCreateForm() {
    setEditingRule(null)
    setFormData(EMPTY_FORM)
    setFormErrors({})
    setShowForm(true)
  }

  function openEditForm(rule) {
    setEditingRule(rule)
    setFormData({
      name: rule.name || '',
      description: rule.description || '',
      project: rule.project || '',
      trigger_type: rule.trigger_type || 'issue_created',
      trigger_config: JSON.stringify(rule.trigger_config || {}, null, 2),
      conditions: JSON.stringify(rule.conditions || [], null, 2),
      actions: JSON.stringify(rule.actions || [], null, 2),
      is_enabled: rule.is_enabled !== false,
    })
    setFormErrors({})
    setShowForm(true)
  }

  function validateForm() {
    const errors = {}
    if (!formData.name.trim()) errors.name = 'Name is required'
    if (!isValidJson(formData.trigger_config)) errors.trigger_config = 'Invalid JSON'
    if (!isValidJson(formData.conditions)) errors.conditions = 'Invalid JSON'
    if (!isValidJson(formData.actions)) errors.actions = 'Invalid JSON'
    setFormErrors(errors)
    return Object.keys(errors).length === 0
  }

  function handleSubmit(e) {
    e.preventDefault()
    if (!validateForm()) return

    const payload = {
      name: formData.name.trim(),
      description: formData.description.trim(),
      trigger_type: formData.trigger_type,
      trigger_config: JSON.parse(formData.trigger_config),
      conditions: JSON.parse(formData.conditions),
      actions: JSON.parse(formData.actions),
      is_enabled: formData.is_enabled,
    }
    if (formData.project.trim()) {
      payload.project = formData.project.trim()
    }

    if (editingRule) {
      updateMutation.mutate({ id: editingRule.id, data: payload })
    } else {
      createMutation.mutate(payload)
    }
  }

  const isSaving = createMutation.isPending || updateMutation.isPending

  // ---- Render ----

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Automation</h1>
          <p className="text-gray-500 dark:text-gray-400">Automate repetitive tasks with rules</p>
        </div>
        {activeTab === 'rules' && (
          <button onClick={openCreateForm} className="btn btn-primary flex items-center gap-2">
            <Plus size={16} />
            Create Rule
          </button>
        )}
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
            {tab.label}
          </button>
        ))}
      </div>

      {/* Create / Edit Form */}
      {showForm && activeTab === 'rules' && (
        <div className="card p-6">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">
            {editingRule ? 'Edit Rule' : 'Create Automation Rule'}
          </h2>
          <form onSubmit={handleSubmit} className="space-y-4">
            {/* Name */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Name <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className={cn('input w-full', formErrors.name && 'border-red-500')}
                placeholder="e.g. Auto-assign on creation"
              />
              {formErrors.name && (
                <p className="text-red-500 text-xs mt-1">{formErrors.name}</p>
              )}
            </div>

            {/* Description */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Description
              </label>
              <textarea
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                className="input w-full"
                rows={2}
                placeholder="Describe what this rule does"
              />
            </div>

            {/* Project */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Project ID
                <span className="text-gray-400 dark:text-gray-500 font-normal ml-1">(optional)</span>
              </label>
              <input
                type="text"
                value={formData.project}
                onChange={(e) => setFormData({ ...formData, project: e.target.value })}
                className="input w-full"
                placeholder="Leave empty for org-wide rule"
              />
            </div>

            {/* Trigger Type */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Trigger Type
              </label>
              <select
                value={formData.trigger_type}
                onChange={(e) => setFormData({ ...formData, trigger_type: e.target.value })}
                className="input w-full"
              >
                {TRIGGER_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Trigger Config */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Trigger Config (JSON)
              </label>
              <textarea
                value={formData.trigger_config}
                onChange={(e) => setFormData({ ...formData, trigger_config: e.target.value })}
                className={cn('input w-full font-mono text-sm', formErrors.trigger_config && 'border-red-500')}
                rows={4}
                placeholder="{}"
              />
              {formErrors.trigger_config && (
                <p className="text-red-500 text-xs mt-1">{formErrors.trigger_config}</p>
              )}
            </div>

            {/* Conditions */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Conditions (JSON)
              </label>
              <textarea
                value={formData.conditions}
                onChange={(e) => setFormData({ ...formData, conditions: e.target.value })}
                className={cn('input w-full font-mono text-sm', formErrors.conditions && 'border-red-500')}
                rows={4}
                placeholder="[]"
              />
              {formErrors.conditions && (
                <p className="text-red-500 text-xs mt-1">{formErrors.conditions}</p>
              )}
            </div>

            {/* Actions */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Actions (JSON)
              </label>
              <textarea
                value={formData.actions}
                onChange={(e) => setFormData({ ...formData, actions: e.target.value })}
                className={cn('input w-full font-mono text-sm', formErrors.actions && 'border-red-500')}
                rows={4}
                placeholder="[]"
              />
              {formErrors.actions && (
                <p className="text-red-500 text-xs mt-1">{formErrors.actions}</p>
              )}
            </div>

            {/* Enabled */}
            <div className="flex items-center gap-3">
              <input
                type="checkbox"
                id="is_enabled"
                checked={formData.is_enabled}
                onChange={(e) => setFormData({ ...formData, is_enabled: e.target.checked })}
                className="h-4 w-4 rounded border-gray-300 text-brand-600 focus:ring-brand-500"
              />
              <label htmlFor="is_enabled" className="text-sm font-medium text-gray-700 dark:text-gray-300">
                Enabled
              </label>
            </div>

            {/* Form actions */}
            <div className="flex items-center gap-3 pt-2">
              <button type="submit" className="btn btn-primary flex items-center gap-2" disabled={isSaving}>
                {isSaving && <LoadingSpinner size="sm" />}
                {editingRule ? 'Update Rule' : 'Create Rule'}
              </button>
              <button type="button" onClick={resetForm} className="btn">
                Cancel
              </button>
            </div>

            {(createMutation.isError || updateMutation.isError) && (
              <p className="text-red-500 text-sm">
                {createMutation.error?.response?.data?.detail ||
                  updateMutation.error?.response?.data?.detail ||
                  'An error occurred. Please try again.'}
              </p>
            )}
          </form>
        </div>
      )}

      {/* Tab Content */}
      {activeTab === 'rules' && <RulesTab
        rules={rules}
        isLoading={rulesLoading}
        error={rulesError}
        toggleMutation={toggleMutation}
        onEdit={openEditForm}
        deleteConfirmId={deleteConfirmId}
        setDeleteConfirmId={setDeleteConfirmId}
        deleteMutation={deleteMutation}
      />}

      {activeTab === 'logs' && <LogsTab
        logs={logs}
        isLoading={logsLoading}
        error={logsError}
        statusFilter={logStatusFilter}
        setStatusFilter={(v) => { setLogStatusFilter(v); setLogsPage(1) }}
        expandedLogId={expandedLogId}
        setExpandedLogId={setExpandedLogId}
        page={logsPage}
        totalPages={logsTotalPages}
        setPage={setLogsPage}
      />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Rules Tab
// ---------------------------------------------------------------------------

function RulesTab({ rules, isLoading, error, toggleMutation, onEdit, deleteConfirmId, setDeleteConfirmId, deleteMutation }) {
  if (isLoading) return <PageLoader />

  if (error) {
    return (
      <div className="card p-6 text-center">
        <XCircle size={32} className="mx-auto text-red-400 mb-2" />
        <p className="text-red-500">Failed to load automation rules.</p>
        <p className="text-gray-400 text-sm mt-1">{error.message}</p>
      </div>
    )
  }

  if (rules.length === 0) {
    return (
      <div className="card p-12 text-center">
        <Zap size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-4" />
        <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-1">No automation rules</h3>
        <p className="text-gray-500 dark:text-gray-400">Create your first rule to automate workflows.</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {rules.map((rule) => (
        <div key={rule.id} className="card p-4">
          <div className="flex items-center justify-between gap-4">
            {/* Left: info */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-3 flex-wrap">
                <h3 className="font-medium text-gray-900 dark:text-gray-100 truncate">{rule.name}</h3>
                <span className={cn('inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium', getTriggerBadgeClasses(rule.trigger_type))}>
                  {formatTriggerLabel(rule.trigger_type)}
                </span>
              </div>

              {rule.description && (
                <p className="text-sm text-gray-500 dark:text-gray-400 mt-1 truncate">{rule.description}</p>
              )}

              <div className="flex flex-wrap items-center gap-4 mt-2 text-xs text-gray-500 dark:text-gray-400">
                {rule.project_name && (
                  <span>Project: {rule.project_name}</span>
                )}
                <span className="flex items-center gap-1">
                  <Activity size={12} />
                  {rule.execution_count ?? 0} executions
                </span>
                <span className="flex items-center gap-1">
                  <Clock size={12} />
                  {formatRelativeTime(rule.last_executed)}
                </span>
              </div>
            </div>

            {/* Right: toggle + actions */}
            <div className="flex items-center gap-3 shrink-0">
              {/* Toggle switch */}
              <button
                onClick={() => toggleMutation.mutate(rule.id)}
                className={cn(
                  'relative inline-flex h-6 w-11 items-center rounded-full transition-colors',
                  rule.is_enabled ? 'bg-brand-600' : 'bg-gray-300 dark:bg-gray-600'
                )}
                title={rule.is_enabled ? 'Disable rule' : 'Enable rule'}
              >
                <span className={cn(
                  'inline-block h-4 w-4 transform rounded-full bg-white transition-transform',
                  rule.is_enabled ? 'translate-x-6' : 'translate-x-1'
                )} />
              </button>

              {/* Edit */}
              <button
                onClick={() => onEdit(rule)}
                className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:text-gray-300 dark:hover:bg-gray-800 transition-colors"
                title="Edit rule"
              >
                <Edit2 size={16} />
              </button>

              {/* Delete */}
              {deleteConfirmId === rule.id ? (
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => deleteMutation.mutate(rule.id)}
                    className="px-2 py-1 text-xs font-medium text-white bg-red-600 hover:bg-red-700 rounded transition-colors"
                    disabled={deleteMutation.isPending}
                  >
                    {deleteMutation.isPending ? 'Deleting...' : 'Confirm'}
                  </button>
                  <button
                    onClick={() => setDeleteConfirmId(null)}
                    className="px-2 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setDeleteConfirmId(rule.id)}
                  className="p-1.5 rounded-lg text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
                  title="Delete rule"
                >
                  <Trash2 size={16} />
                </button>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Logs Tab
// ---------------------------------------------------------------------------

function LogsTab({ logs, isLoading, error, statusFilter, setStatusFilter, expandedLogId, setExpandedLogId, page, totalPages, setPage }) {
  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-gray-700 dark:text-gray-300">Status:</span>
        {['all', 'success', 'failure', 'skipped'].map((s) => (
          <button
            key={s}
            onClick={() => setStatusFilter(s)}
            className={cn(
              'px-3 py-1 rounded-full text-xs font-medium transition-colors capitalize',
              statusFilter === s
                ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300'
                : 'text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'
            )}
          >
            {s}
          </button>
        ))}
      </div>

      {/* Content */}
      {isLoading ? (
        <PageLoader />
      ) : error ? (
        <div className="card p-6 text-center">
          <XCircle size={32} className="mx-auto text-red-400 mb-2" />
          <p className="text-red-500">Failed to load execution logs.</p>
          <p className="text-gray-400 text-sm mt-1">{error.message}</p>
        </div>
      ) : logs.length === 0 ? (
        <div className="card p-12 text-center">
          <Activity size={48} className="mx-auto text-gray-300 dark:text-gray-600 mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-1">No execution logs</h3>
          <p className="text-gray-500 dark:text-gray-400">
            {statusFilter !== 'all'
              ? `No logs with status "${statusFilter}".`
              : 'Logs will appear here when automation rules are triggered.'}
          </p>
        </div>
      ) : (
        <>
          <div className="space-y-2">
            {logs.map((log) => (
              <LogEntry
                key={log.id}
                log={log}
                isExpanded={expandedLogId === log.id}
                onToggle={() => setExpandedLogId(expandedLogId === log.id ? null : log.id)}
              />
            ))}
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-2 pt-2">
              <button
                onClick={() => setPage(Math.max(1, page - 1))}
                disabled={page <= 1}
                className="btn text-sm disabled:opacity-40"
              >
                Previous
              </button>
              <span className="text-sm text-gray-500 dark:text-gray-400">
                Page {page} of {totalPages}
              </span>
              <button
                onClick={() => setPage(Math.min(totalPages, page + 1))}
                disabled={page >= totalPages}
                className="btn text-sm disabled:opacity-40"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Log Entry
// ---------------------------------------------------------------------------

function LogEntry({ log, isExpanded, onToggle }) {
  const statusIcon = log.status === 'success'
    ? <CheckCircle size={16} className="text-green-500" />
    : log.status === 'failure'
      ? <XCircle size={16} className="text-red-500" />
      : <Pause size={16} className="text-gray-400" />

  return (
    <div className="card">
      <button
        onClick={onToggle}
        className="w-full p-4 flex items-center justify-between gap-4 text-left"
      >
        <div className="flex items-center gap-3 flex-1 min-w-0">
          {isExpanded
            ? <ChevronDown size={16} className="text-gray-400 shrink-0" />
            : <ChevronRight size={16} className="text-gray-400 shrink-0" />}

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium text-gray-900 dark:text-gray-100 truncate">
                {log.rule_name || log.rule?.name || `Rule #${log.rule}`}
              </span>
              <span className={cn(
                'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium',
                getStatusBadgeClasses(log.status)
              )}>
                {statusIcon}
                {log.status}
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-4 mt-1 text-xs text-gray-500 dark:text-gray-400">
              <span className="flex items-center gap-1">
                <Clock size={12} />
                {formatRelativeTime(log.triggered_at)}
              </span>
              {log.items_affected != null && (
                <span>{log.items_affected} item{log.items_affected !== 1 ? 's' : ''} affected</span>
              )}
            </div>

            {log.error_message && (
              <p className="text-xs text-red-500 mt-1 truncate">{log.error_message}</p>
            )}
          </div>
        </div>
      </button>

      {/* Expanded details */}
      {isExpanded && (
        <div className="border-t border-gray-200 dark:border-gray-700 px-4 py-3">
          {log.error_message && (
            <div className="mb-3">
              <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Error Message</p>
              <p className="text-sm text-red-500">{log.error_message}</p>
            </div>
          )}

          <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Execution Details</p>
          {log.execution_details ? (
            <pre className="text-xs bg-gray-50 dark:bg-gray-900 rounded-lg p-3 overflow-x-auto font-mono text-gray-800 dark:text-gray-200">
              {typeof log.execution_details === 'string'
                ? log.execution_details
                : JSON.stringify(log.execution_details, null, 2)}
            </pre>
          ) : (
            <p className="text-sm text-gray-400 dark:text-gray-500 italic">No details available.</p>
          )}
        </div>
      )}
    </div>
  )
}
