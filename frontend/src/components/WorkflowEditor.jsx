import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Plus, Trash2, GripVertical, Edit2, Check, X,
  AlertCircle, ArrowRight, Settings, GitBranch, List,
  Shield, Zap, UserCheck, ChevronDown,
} from 'lucide-react'
import { workflowsApi } from '../services/api'
import { cn } from '../utils/cn'
import WorkflowDiagram from './WorkflowDiagram'

const STATUS_CATEGORIES = [
  { value: 'todo', label: 'To Do', color: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300' },
  { value: 'in_progress', label: 'In Progress', color: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400' },
  { value: 'done', label: 'Done', color: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' },
]

const DEFAULT_STATUSES = [
  { name: 'pending', label: 'Pending', category: 'todo' },
  { name: 'in_progress', label: 'In Progress', category: 'in_progress' },
  { name: 'blocked', label: 'Blocked', category: 'in_progress' },
  { name: 'completed', label: 'Completed', category: 'done' },
]

export default function WorkflowEditor({ projectId, className = '' }) {
  const queryClient = useQueryClient()
  const [editingStatus, setEditingStatus] = useState(null)
  const [newStatus, setNewStatus] = useState({ name: '', label: '', category: 'todo' })
  const [showAddForm, setShowAddForm] = useState(false)
  const [showTransitions, setShowTransitions] = useState(false)
  const [viewMode, setViewMode] = useState('diagram') // 'diagram' | 'list'
  const [selectedDiagramStatus, setSelectedDiagramStatus] = useState(null)
  const [selectedDiagramTransition, setSelectedDiagramTransition] = useState(null)

  // Fetch workflow for this project
  const { data: workflow, isLoading } = useQuery({
    queryKey: ['workflow', projectId],
    queryFn: async () => {
      try {
        // Fetch workflows filtered by project, pick the one matching this project
        const res = await workflowsApi.list({ project_id: projectId })
        const results = res.data?.results || res.data || []
        const pid = parseInt(projectId)
        const match = results.find(w => w.project === pid)
        if (match) return match
        // Fallback: try direct ID fetch
        const direct = await workflowsApi.get(projectId).catch(() => null)
        return direct?.data || null
      } catch (err) {
        if (err.response?.status === 404) return null
        throw err
      }
    },
    enabled: !!projectId,
    retry: false,
  })

  // Create/update workflow mutation
  const saveMutation = useMutation({
    mutationFn: (data) => {
      if (workflow?.id) {
        return workflowsApi.update(projectId, data)
      }
      return workflowsApi.create({ project: projectId, ...data })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflow', projectId] })
    },
  })

  const statuses = workflow?.statuses || DEFAULT_STATUSES
  const initialStatus = workflow?.initial_status || 'pending'

  const handleAddStatus = () => {
    if (!newStatus.name || !newStatus.label) return

    const slug = newStatus.name.toLowerCase().replace(/\s+/g, '_')
    const updatedStatuses = [...statuses, { ...newStatus, name: slug }]

    saveMutation.mutate({ statuses: updatedStatuses, initial_status: initialStatus })
    setNewStatus({ name: '', label: '', category: 'todo' })
    setShowAddForm(false)
  }

  const handleUpdateStatus = (index, updates) => {
    const updatedStatuses = statuses.map((s, i) =>
      i === index ? { ...s, ...updates } : s
    )
    saveMutation.mutate({ statuses: updatedStatuses, initial_status: initialStatus })
    setEditingStatus(null)
  }

  const handleDeleteStatus = (index) => {
    const statusToDelete = statuses[index]
    if (statusToDelete.name === initialStatus) {
      alert('Cannot delete the initial status')
      return
    }
    const updatedStatuses = statuses.filter((_, i) => i !== index)
    saveMutation.mutate({ statuses: updatedStatuses, initial_status: initialStatus })
  }

  const handleSetInitial = (statusName) => {
    saveMutation.mutate({ statuses, initial_status: statusName })
  }

  const handleMoveStatus = (fromIndex, toIndex) => {
    const updatedStatuses = [...statuses]
    const [removed] = updatedStatuses.splice(fromIndex, 1)
    updatedStatuses.splice(toIndex, 0, removed)
    saveMutation.mutate({ statuses: updatedStatuses, initial_status: initialStatus })
  }

  const getCategoryColor = (category) => {
    return STATUS_CATEGORIES.find(c => c.value === category)?.color || STATUS_CATEGORIES[0].color
  }

  if (isLoading) {
    return (
      <div className={cn('text-gray-500 dark:text-gray-400', className)}>
        Loading workflow...
      </div>
    )
  }

  return (
    <div className={cn('space-y-6', className)}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            Workflow
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {statuses.length} statuses, {(workflow?.transitions || []).length} transitions
          </p>
        </div>
        <div className="flex items-center gap-2">
          {/* View toggle */}
          <div className="flex items-center bg-gray-100 dark:bg-gray-800 rounded-lg p-0.5">
            <button
              onClick={() => setViewMode('diagram')}
              className={cn(
                'px-3 py-1.5 rounded-md text-sm font-medium transition-colors flex items-center gap-1.5',
                viewMode === 'diagram'
                  ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                  : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
              )}
            >
              <GitBranch size={14} />
              Diagram
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={cn(
                'px-3 py-1.5 rounded-md text-sm font-medium transition-colors flex items-center gap-1.5',
                viewMode === 'list'
                  ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                  : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
              )}
            >
              <List size={14} />
              List
            </button>
          </div>
          <button
            onClick={() => setShowTransitions(!showTransitions)}
            className={cn(
              'btn-secondary text-sm',
              showTransitions && 'bg-gray-100 dark:bg-gray-800'
            )}
          >
            <Settings size={14} className="mr-1" />
            Rules
          </button>
        </div>
      </div>

      {/* Diagram View */}
      {viewMode === 'diagram' && (
        <div className="space-y-4">
          <WorkflowDiagram
            statuses={statuses}
            transitions={workflow?.transitions || []}
            initialStatus={initialStatus}
            isEditing={true}
            selectedStatus={selectedDiagramStatus}
            selectedTransition={selectedDiagramTransition}
            onSelectStatus={(name) => {
              setSelectedDiagramStatus(name)
              setSelectedDiagramTransition(null)
            }}
            onSelectTransition={(t) => {
              setSelectedDiagramTransition(t)
              setSelectedDiagramStatus(null)
            }}
            onAddTransition={async (fromStatus, toStatus) => {
              try {
                const fromLabel = statuses.find(s => s.name === fromStatus)?.label || fromStatus
                const targetLabel = statuses.find(s => s.name === toStatus)?.label || toStatus
                await workflowsApi.createTransition({
                  workflow: workflow?.id || projectId,
                  from_status: fromStatus,
                  to_status: toStatus,
                  name: `${fromLabel} → ${targetLabel}`,
                })
                queryClient.invalidateQueries({ queryKey: ['workflow', projectId] })
                queryClient.invalidateQueries({ queryKey: ['workflow-transitions', projectId] })
              } catch (err) {
                console.error('Failed to create transition:', err)
              }
            }}
            onRemoveTransition={async (transitionId) => {
              try {
                await workflowsApi.deleteTransition(transitionId)
                queryClient.invalidateQueries({ queryKey: ['workflow', projectId] })
                setSelectedDiagramTransition(null)
              } catch (err) {
                console.error('Failed to delete transition:', err)
              }
            }}
            className="min-h-[350px]"
          />

          {/* Selected node info panel */}
          {selectedDiagramStatus && (() => {
            const status = statuses.find(s => s.name === selectedDiagramStatus)
            if (!status) return null
            const outgoing = (workflow?.transitions || []).filter(t => t.from_status === selectedDiagramStatus)
            const incoming = (workflow?.transitions || []).filter(t => t.to_status === selectedDiagramStatus)

            return (
              <div className="card p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {status.color && <span className="w-3 h-3 rounded-full" style={{ backgroundColor: status.color }} />}
                    <h4 className="font-semibold text-gray-900 dark:text-white">{status.label || status.name}</h4>
                    <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 px-2 py-0.5 rounded-full">
                      {status.category}
                    </span>
                    {selectedDiagramStatus === initialStatus && (
                      <span className="text-xs text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-900/30 px-2 py-0.5 rounded-full font-medium">
                        Initial
                      </span>
                    )}
                  </div>
                  <button onClick={() => setSelectedDiagramStatus(null)} className="btn-icon text-gray-400">
                    <X size={16} />
                  </button>
                </div>

                {outgoing.length > 0 && (
                  <div>
                    <p className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1">
                      Transitions out ({outgoing.length})
                    </p>
                    <div className="space-y-1">
                      {outgoing.map((t, i) => (
                        <div key={i} className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                          <ArrowRight size={14} className="text-gray-400" />
                          <span className="font-medium">{t.name || t.to_status}</span>
                          <span className="text-gray-400">→</span>
                          <span>{statuses.find(s => s.name === t.to_status)?.label || t.to_status}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {incoming.length > 0 && (
                  <div>
                    <p className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1">
                      Transitions in ({incoming.length})
                    </p>
                    <div className="space-y-1">
                      {incoming.map((t, i) => (
                        <div key={i} className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                          <span>{statuses.find(s => s.name === t.from_status)?.label || t.from_status}</span>
                          <span className="text-gray-400">→</span>
                          <span className="font-medium">{t.name || 'transition'}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )
          })()}

          {/* Selected transition editing panel */}
          {selectedDiagramTransition && (
            <TransitionDetailPanel
              transition={selectedDiagramTransition}
              statuses={statuses}
              onClose={() => setSelectedDiagramTransition(null)}
              onUpdate={async (updates) => {
                try {
                  await workflowsApi.updateTransition(selectedDiagramTransition.id, updates)
                  queryClient.invalidateQueries({ queryKey: ['workflow', projectId] })
                } catch (err) {
                  console.error('Failed to update transition:', err)
                }
              }}
              onDelete={async () => {
                try {
                  await workflowsApi.deleteTransition(selectedDiagramTransition.id)
                  queryClient.invalidateQueries({ queryKey: ['workflow', projectId] })
                  setSelectedDiagramTransition(null)
                } catch (err) {
                  console.error('Failed to delete transition:', err)
                }
              }}
            />
          )}
        </div>
      )}

      {/* List View — Status List */}
      {viewMode === 'list' && (<>
      <div className="space-y-2">
        {statuses.map((status, index) => (
          <div
            key={status.name}
            className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg group"
          >
            {/* Drag handle */}
            <button
              className="cursor-grab text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              onMouseDown={(e) => {
                // Simple drag reorder would go here
              }}
            >
              <GripVertical size={16} />
            </button>

            {/* Status badge */}
            <span className={cn(
              'px-2 py-1 rounded text-xs font-medium',
              getCategoryColor(status.category)
            )}>
              {status.name}
            </span>

            {/* Label */}
            {editingStatus === index ? (
              <input
                type="text"
                defaultValue={status.label}
                className="input flex-1 text-sm"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    handleUpdateStatus(index, { label: e.target.value })
                  } else if (e.key === 'Escape') {
                    setEditingStatus(null)
                  }
                }}
                autoFocus
              />
            ) : (
              <span className="flex-1 text-sm text-gray-700 dark:text-gray-300">
                {status.label}
              </span>
            )}

            {/* Category selector */}
            <select
              value={status.category}
              onChange={(e) => handleUpdateStatus(index, { category: e.target.value })}
              className="text-xs border border-gray-200 dark:border-gray-700 rounded px-2 py-1 bg-white dark:bg-gray-900"
            >
              {STATUS_CATEGORIES.map(cat => (
                <option key={cat.value} value={cat.value}>{cat.label}</option>
              ))}
            </select>

            {/* Initial status indicator */}
            {status.name === initialStatus ? (
              <span className="text-xs text-brand-600 dark:text-brand-400 font-medium">
                Initial
              </span>
            ) : (
              <button
                onClick={() => handleSetInitial(status.name)}
                className="text-xs text-gray-400 hover:text-brand-500 opacity-0 group-hover:opacity-100 transition-opacity"
              >
                Set Initial
              </button>
            )}

            {/* Actions */}
            <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
              {editingStatus === index ? (
                <>
                  <button
                    onClick={() => setEditingStatus(null)}
                    className="p-1 text-gray-400 hover:text-gray-600"
                  >
                    <X size={14} />
                  </button>
                </>
              ) : (
                <>
                  <button
                    onClick={() => setEditingStatus(index)}
                    className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                  >
                    <Edit2 size={14} />
                  </button>
                  <button
                    onClick={() => handleDeleteStatus(index)}
                    className="p-1 text-gray-400 hover:text-red-500"
                    disabled={status.name === initialStatus}
                  >
                    <Trash2 size={14} />
                  </button>
                </>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Add Status Form */}
      {showAddForm ? (
        <div className="p-4 border border-gray-200 dark:border-gray-700 rounded-lg space-y-3">
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-xs text-gray-500 mb-1">Name (slug)</label>
              <input
                type="text"
                value={newStatus.name}
                onChange={(e) => setNewStatus({ ...newStatus, name: e.target.value })}
                placeholder="e.g., review"
                className="input w-full text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Display Label</label>
              <input
                type="text"
                value={newStatus.label}
                onChange={(e) => setNewStatus({ ...newStatus, label: e.target.value })}
                placeholder="e.g., In Review"
                className="input w-full text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Category</label>
              <select
                value={newStatus.category}
                onChange={(e) => setNewStatus({ ...newStatus, category: e.target.value })}
                className="input w-full text-sm"
              >
                {STATUS_CATEGORIES.map(cat => (
                  <option key={cat.value} value={cat.value}>{cat.label}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setShowAddForm(false)}
              className="btn-secondary text-sm"
            >
              Cancel
            </button>
            <button
              onClick={handleAddStatus}
              disabled={!newStatus.name || !newStatus.label}
              className="btn-primary text-sm"
            >
              Add Status
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setShowAddForm(true)}
          className="flex items-center gap-2 text-sm text-brand-500 hover:text-brand-600"
        >
          <Plus size={16} />
          Add Status
        </button>
      )}
      </>)}

      {/* Transitions Section */}
      {showTransitions && (
        <TransitionEditor
          projectId={projectId}
          statuses={statuses}
        />
      )}

      {/* Save indicator */}
      {saveMutation.isPending && (
        <div className="text-sm text-gray-500 dark:text-gray-400">
          Saving...
        </div>
      )}
      {saveMutation.isError && (
        <div className="flex items-center gap-2 text-sm text-red-600 dark:text-red-400">
          <AlertCircle size={14} />
          Failed to save workflow
        </div>
      )}
    </div>
  )
}

// ── Transition Detail Panel (Jira-style) ──

const POST_FUNCTION_TYPES = [
  { type: 'assign_to', label: 'Assign To', icon: UserCheck, options: ['project_lead', 'reporter', 'unassign'] },
  { type: 'set_field', label: 'Set Field', icon: Edit2 },
  { type: 'add_comment', label: 'Add Comment', icon: Edit2 },
  { type: 'send_notification', label: 'Send Notification', icon: AlertCircle },
  { type: 'fire_webhook', label: 'Fire Webhook', icon: Zap },
]

const CONDITION_TYPES = [
  { type: 'role', label: 'User in Role' },
  { type: 'assignee_only', label: 'Assignee Only' },
  { type: 'reporter_only', label: 'Reporter Only' },
]

function TransitionDetailPanel({ transition, statuses, onClose, onUpdate, onDelete }) {
  const [name, setName] = useState(transition.name || '')
  const [description, setDescription] = useState(transition.description || '')
  const [showPostFunctions, setShowPostFunctions] = useState(false)
  const [showConditions, setShowConditions] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const fromLabel = statuses.find(s => s.name === transition.from_status)?.label || transition.from_status
  const toLabel = statuses.find(s => s.name === transition.to_status)?.label || transition.to_status
  const postFunctions = transition.post_functions || []
  const conditions = transition.conditions || {}

  return (
    <div className="card p-0 overflow-hidden border-indigo-200 dark:border-indigo-800">
      {/* Header */}
      <div className="bg-indigo-50 dark:bg-indigo-900/20 px-4 py-3 flex items-center justify-between border-b border-indigo-100 dark:border-indigo-800/50">
        <div className="flex items-center gap-2">
          <ArrowRight size={16} className="text-indigo-500" />
          <span className="font-semibold text-gray-900 dark:text-white text-sm">Transition Properties</span>
        </div>
        <button onClick={onClose} className="btn-icon text-gray-400 hover:text-gray-600">
          <X size={16} />
        </button>
      </div>

      <div className="p-4 space-y-4">
        {/* From → To display */}
        <div className="flex items-center gap-3">
          <span className="px-3 py-1.5 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm font-medium text-gray-700 dark:text-gray-300">
            {fromLabel}
          </span>
          <ArrowRight size={18} className="text-indigo-400" />
          <span className="px-3 py-1.5 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm font-medium text-gray-700 dark:text-gray-300">
            {toLabel}
          </span>
        </div>

        {/* Name */}
        <div>
          <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
            Transition Name
          </label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => { if (name !== transition.name) onUpdate({ name }) }}
            className="input mt-1 w-full"
            placeholder="e.g., Start Progress"
          />
        </div>

        {/* Description */}
        <div>
          <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
            Description
          </label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            onBlur={() => { if (description !== transition.description) onUpdate({ description }) }}
            className="input mt-1 w-full resize-none"
            rows={2}
            placeholder="Describe when this transition should be used..."
          />
        </div>

        {/* Button Style */}
        <div>
          <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
            Button Style
          </label>
          <select
            value={transition.button_style || ''}
            onChange={(e) => onUpdate({ button_style: e.target.value || null })}
            className="input mt-1 w-full"
          >
            <option value="">Default</option>
            <option value="primary">Primary (Blue)</option>
            <option value="success">Success (Green)</option>
            <option value="danger">Danger (Red)</option>
            <option value="warning">Warning (Orange)</option>
          </select>
        </div>

        {/* Conditions */}
        <div>
          <button
            onClick={() => setShowConditions(!showConditions)}
            className="flex items-center gap-2 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider hover:text-gray-700 dark:hover:text-gray-300 w-full"
          >
            <Shield size={14} />
            Conditions
            {Object.keys(conditions).length > 0 && (
              <span className="bg-indigo-100 dark:bg-indigo-900/50 text-indigo-600 dark:text-indigo-400 px-1.5 py-0.5 rounded-full text-[10px]">
                {Object.keys(conditions).length}
              </span>
            )}
            <ChevronDown size={14} className={cn('ml-auto transition-transform', showConditions && 'rotate-180')} />
          </button>
          {showConditions && (
            <div className="mt-2 space-y-2 p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg">
              <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input
                  type="checkbox"
                  checked={transition.assignee_only || false}
                  onChange={(e) => onUpdate({ assignee_only: e.target.checked })}
                  className="rounded border-gray-300"
                />
                Only assignee can execute
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input
                  type="checkbox"
                  checked={transition.reporter_only || false}
                  onChange={(e) => onUpdate({ reporter_only: e.target.checked })}
                  className="rounded border-gray-300"
                />
                Only reporter can execute
              </label>
              {conditions.allowed_roles && (
                <div className="text-xs text-gray-500">
                  Allowed roles: {JSON.stringify(conditions.allowed_roles)}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Post-functions */}
        <div>
          <button
            onClick={() => setShowPostFunctions(!showPostFunctions)}
            className="flex items-center gap-2 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider hover:text-gray-700 dark:hover:text-gray-300 w-full"
          >
            <Zap size={14} />
            Post-Functions
            {postFunctions.length > 0 && (
              <span className="bg-amber-100 dark:bg-amber-900/50 text-amber-600 dark:text-amber-400 px-1.5 py-0.5 rounded-full text-[10px]">
                {postFunctions.length}
              </span>
            )}
            <ChevronDown size={14} className={cn('ml-auto transition-transform', showPostFunctions && 'rotate-180')} />
          </button>
          {showPostFunctions && (
            <div className="mt-2 space-y-2 p-3 bg-gray-50 dark:bg-gray-800/50 rounded-lg">
              {postFunctions.length === 0 ? (
                <p className="text-xs text-gray-500 dark:text-gray-400">No post-functions configured</p>
              ) : (
                postFunctions.map((pf, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 rounded p-2">
                    <Zap size={12} className="text-amber-500" />
                    <span className="font-medium">{pf.type}</span>
                    {pf.value && <span className="text-gray-500">= {typeof pf.value === 'string' ? pf.value : JSON.stringify(pf.value)}</span>}
                    <button
                      onClick={() => {
                        const updated = postFunctions.filter((_, j) => j !== i)
                        onUpdate({ post_functions: updated })
                      }}
                      className="ml-auto text-gray-400 hover:text-red-500"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                ))
              )}
              {/* Add post-function */}
              <select
                onChange={(e) => {
                  if (!e.target.value) return
                  const updated = [...postFunctions, { type: e.target.value }]
                  onUpdate({ post_functions: updated })
                  e.target.value = ''
                }}
                className="input text-xs w-full"
                defaultValue=""
              >
                <option value="">+ Add post-function...</option>
                {POST_FUNCTION_TYPES.map(pf => (
                  <option key={pf.type} value={pf.type}>{pf.label}</option>
                ))}
              </select>
            </div>
          )}
        </div>

        {/* Delete */}
        <div className="pt-2 border-t border-gray-200 dark:border-gray-700">
          {confirmDelete ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-red-500">Delete this transition?</span>
              <button onClick={onDelete} className="btn-sm text-xs bg-red-500 text-white hover:bg-red-600 rounded px-2 py-1">
                Yes, delete
              </button>
              <button onClick={() => setConfirmDelete(false)} className="btn-sm text-xs text-gray-500 hover:text-gray-700 px-2 py-1">
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmDelete(true)}
              className="flex items-center gap-1.5 text-xs text-red-500 hover:text-red-600"
            >
              <Trash2 size={14} />
              Delete Transition
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// Transition Editor Component
function TransitionEditor({ projectId, statuses }) {
  const queryClient = useQueryClient()
  const [newTransition, setNewTransition] = useState({ from_status: '', to_status: '' })

  // Fetch transitions
  const { data: transitions = [] } = useQuery({
    queryKey: ['workflow-transitions', projectId],
    queryFn: async () => {
      const res = await workflowsApi.getTransitions(projectId)
      return res.data
    },
    enabled: !!projectId,
  })

  // Add transition mutation
  const addMutation = useMutation({
    mutationFn: (data) => workflowsApi.createTransition(projectId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflow-transitions', projectId] })
      setNewTransition({ from_status: '', to_status: '' })
    },
  })

  // Delete transition mutation
  const deleteMutation = useMutation({
    mutationFn: (transitionId) => workflowsApi.deleteTransition(projectId, transitionId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflow-transitions', projectId] })
    },
  })

  const handleAddTransition = () => {
    if (!newTransition.from_status || !newTransition.to_status) return
    addMutation.mutate(newTransition)
  }

  return (
    <div className="border-t border-gray-200 dark:border-gray-700 pt-6 space-y-4">
      <div>
        <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
          Allowed Transitions
        </h4>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Define which status changes are allowed (empty = all allowed)
        </p>
      </div>

      {/* Existing transitions */}
      {transitions.length > 0 ? (
        <div className="space-y-2">
          {transitions.map((transition) => (
            <div
              key={transition.id}
              className="flex items-center gap-3 p-2 bg-gray-50 dark:bg-gray-800 rounded"
            >
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {transition.from_status}
              </span>
              <ArrowRight size={14} className="text-gray-400" />
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {transition.to_status}
              </span>
              <div className="flex-1" />
              <button
                onClick={() => deleteMutation.mutate(transition.id)}
                className="p-1 text-gray-400 hover:text-red-500"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-sm text-gray-400 italic">
          No transitions defined - all status changes are allowed
        </p>
      )}

      {/* Add new transition */}
      <div className="flex items-center gap-2">
        <select
          value={newTransition.from_status}
          onChange={(e) => setNewTransition({ ...newTransition, from_status: e.target.value })}
          className="input text-sm"
        >
          <option value="">From...</option>
          {statuses.map(s => (
            <option key={s.name} value={s.name}>{s.label}</option>
          ))}
        </select>
        <ArrowRight size={14} className="text-gray-400" />
        <select
          value={newTransition.to_status}
          onChange={(e) => setNewTransition({ ...newTransition, to_status: e.target.value })}
          className="input text-sm"
        >
          <option value="">To...</option>
          {statuses.map(s => (
            <option key={s.name} value={s.name}>{s.label}</option>
          ))}
        </select>
        <button
          onClick={handleAddTransition}
          disabled={!newTransition.from_status || !newTransition.to_status || addMutation.isPending}
          className="btn-primary text-sm"
        >
          <Plus size={14} className="mr-1" />
          Add
        </button>
      </div>
    </div>
  )
}
