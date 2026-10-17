/**
 * WorkflowStatusBar — shows workflow pipeline + transition buttons on task detail.
 * Rendered in the sidebar when a task has workflow_name and workflow_status.
 */

import { useState, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  GitBranch,
  ChevronRight,
  ChevronDown,
  ArrowRight,
  Users,
  Loader2,
  MessageSquare,
  Map,
  Maximize2,
  Minimize2,
  X,
} from 'lucide-react'
import { createPortal } from 'react-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { itemsApi, workflowsApi } from '../services/api'
import { useToast } from './Toast'
import { cn } from '../utils/cn'
import { formatScopedTeamName } from '../utils/teamLabels'
import WorkflowDiagram from './WorkflowDiagram'

// Status category colors
const CATEGORY_STYLES = {
  open: { bg: 'bg-gray-100 dark:bg-gray-700', text: 'text-gray-700 dark:text-gray-300', ring: 'ring-gray-400' },
  todo: { bg: 'bg-gray-100 dark:bg-gray-700', text: 'text-gray-700 dark:text-gray-300', ring: 'ring-gray-400' },
  in_progress: { bg: 'bg-blue-100 dark:bg-blue-900/50', text: 'text-blue-700 dark:text-blue-300', ring: 'ring-blue-500' },
  done: { bg: 'bg-green-100 dark:bg-green-900/50', text: 'text-green-700 dark:text-green-300', ring: 'ring-green-500' },
  cancelled: { bg: 'bg-gray-100 dark:bg-gray-800', text: 'text-gray-500 dark:text-gray-500', ring: 'ring-gray-500' },
  // Scheduled: purple tone signals "timer-owned, will re-open on schedule"
  scheduled: { bg: 'bg-violet-100 dark:bg-violet-900/40', text: 'text-violet-700 dark:text-violet-300', ring: 'ring-violet-500' },
}

function StatusPill({ status, isCurrent, isPast }) {
  const style = CATEGORY_STYLES[status.category] || CATEGORY_STYLES.open
  return (
    <span
      className={cn(
        'px-2.5 py-1 rounded-full text-xs font-medium whitespace-nowrap transition-all',
        isCurrent
          ? `${style.bg} ${style.text} ring-2 ${style.ring} shadow-sm`
          : isPast
            ? 'bg-gray-100 dark:bg-gray-700/50 text-gray-500 dark:text-gray-500'
            : 'bg-gray-50 dark:bg-gray-800/50 text-gray-400 dark:text-gray-600'
      )}
      style={isCurrent && status.color ? {
        backgroundColor: status.color + '20',
        color: status.color,
        boxShadow: `0 0 0 2px ${status.color}40`,
      } : undefined}
    >
      {status.label || status.name}
    </span>
  )
}

export default function WorkflowStatusBar({ task, onTaskUpdate }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const teamLabel = formatScopedTeamName(
    task.assignment_team || {
      canonical_name: task.assignment_group,
      display_name: task.assignment_group,
    },
    {
      organization_id: task.organization_id,
      organization_name: task.organization_name,
      organization_slug: task.organization_slug,
    }
  )
  const [transitionComment, setTransitionComment] = useState('')
  const [showCommentFor, setShowCommentFor] = useState(null)
  // Cancel/Defer reason category — server-owned. The options come from the
  // transition's screen_fields (reason_category.enum) returned by
  // available-transitions (PLS-756): single source of truth, no hardcoded enum.
  const [reasonCategory, setReasonCategory] = useState('')
  const [showDiagram, setShowDiagram] = useState(false)
  const [selectedArticle, setSelectedArticle] = useState(null)
  const pipelineRef = useRef(null)
  const currentPillRef = useRef(null)

  // Fetch workflow definition by slug/name (Item.workflow_name is often the
  // slug, e.g. "simple"), not by scanning page 1 of GET /workflows/.
  const {
    data: workflowData,
    isPending: isWorkflowLoading,
  } = useQuery({
    queryKey: ['task-workflow', task.workflow_name],
    queryFn: async () => {
      if (!task.workflow_name) return null
      const key = String(task.workflow_name).trim()
      try {
        const detail = await workflowsApi.getJiraWorkflow(key)
        if (detail.data?.id) return detail.data
      } catch { /* older servers only look up UUID; fall through */ }
      try {
        const res = await workflowsApi.listJiraWorkflows({ page_size: 200 })
        const all = res.data?.results || res.data || []
        const targetName = key.toLowerCase()
        const normalizeKey = (s) => (s || '').toLowerCase().replace(/[\s_-]+/g, '-').trim()
        const targetKey = normalizeKey(key)
        const match = all.find(w => {
          const wName = (w.name || '').toLowerCase().trim()
          if (wName === targetName) return true
          const wSlug = (w.slug || '').toLowerCase()
          return wSlug === targetKey
            || normalizeKey(w.name) === targetKey
        })
        if (match) {
          const detail = await workflowsApi.getJiraWorkflow(match.id)
          return detail.data
        }
      } catch { /* fall through */ }
      if (task.project) {
        const res = await workflowsApi.list({ project_id: task.project })
        const results = res.data?.results || res.data || []
        const pid = parseInt(task.project)
        return results.find(w => w.project === pid) || null
      }
      return null
    },
    enabled: !!task.workflow_name,
  })

  // Fetch available transitions
  const { data: transitionsData } = useQuery({
    queryKey: ['available-transitions', task.id, task.workflow_status || task.status],
    queryFn: async () => {
      const res = await itemsApi.getAvailableTransitions(task.id)
      return res.data
    },
    enabled: !!task.workflow_name,
    retry: false,
  })

  // Execute transition
  const transitionMutation = useMutation({
    mutationFn: ({ toStatus, transitionId, transitionName, comment, reasonCategory: rc }) => {
      const isRunNow = (transitionName || '').trim().toLowerCase() === 'run now'
      const cycle = task.metadata?.recurring_cycle || {}
      const screenData = {
        ...(comment ? { comment } : {}),
        ...(rc ? { reason_category: rc } : {}),
        ...(isRunNow ? {
          expected_cycle_revision: cycle.cycle_revision ?? 0,
          expected_generation: cycle.active_generation ?? 0,
          expected_due_at: task.due_date,
          expected_completion_watermark: cycle.last_terminal_completion_at ?? '',
          recurring_event_id: cycle.last_event_id || undefined,
        } : {}),
      }
      return itemsApi.executeTransition(task.id, {
        to_status: toStatus,
        transition_id: transitionId,
        comment: comment || undefined,
        screen_data: Object.keys(screenData).length ? screenData : undefined,
      })
    },
    onSuccess: () => {
      // Invalidate all task queries — use both string and number ID to match TaskDetail's queryKey
      queryClient.invalidateQueries({ queryKey: ['task', String(task.id)] })
      queryClient.invalidateQueries({ queryKey: ['task', task.id] })
      if (task.item_key) queryClient.invalidateQueries({ queryKey: ['task', task.item_key] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['available-transitions', task.id] })
      setShowCommentFor(null)
      setTransitionComment('')
      setReasonCategory('')
      if (onTaskUpdate) onTaskUpdate()
    },
    onError: (err) => {
      const data = err?.response?.data
      const msg = data?.error || data?.detail || (typeof data === 'string' ? data : null) || err?.message || 'Transition failed'
      toast.error(msg)
    },
  })

  // Scroll current status pill into view (centered) when workflow data loads
  // or when the current status changes. Uses scrollLeft instead of scrollIntoView
  // to avoid scrolling the whole page.
  useEffect(() => {
    const pipeline = pipelineRef.current
    const currentPill = currentPillRef.current
    if (!pipeline || !currentPill) return
    const pillCenter = currentPill.offsetLeft + currentPill.offsetWidth / 2
    const targetScrollLeft = pillCenter - pipeline.clientWidth / 2
    pipeline.scrollTo({ left: Math.max(0, targetScrollLeft), behavior: 'smooth' })
  }, [workflowData, task.workflow_status, task.status])

  if (!task.workflow_name) return null

  // Build status pipeline from workflow data
  const rawStatuses = workflowData?.statuses || []
  const transitions = transitionsData?.transitions || []

  // Normalize statuses — handle both ProjectWorkflow format and Jira Workflow format
  const statuses = rawStatuses.map(s => ({
    name: s.status_slug || s.name || s.slug || '',
    label: s.status_name || s.label || s.name || '',
    category: s.status_category || s.category || 'todo',
    color: s.status_color || s.color || null,
    position: s.position ?? 999,
  }))

  // Order by position, then by category
  const categoryOrder = ['open', 'todo', 'in_progress', 'done', 'scheduled', 'cancelled']
  const orderedStatuses = [...statuses].sort((a, b) => {
    if (a.position !== b.position) return a.position - b.position
    return categoryOrder.indexOf(a.category) - categoryOrder.indexOf(b.category)
  })

  // Match current status. Prefer the canonical slug on `task.status` over
  // the display-name `task.workflow_status` ("To Do") — the latter would
  // normalize to "to do" (with a space) which matches no node slug.
  // If status is missing, fall back to the label and match against each
  // node's LABEL rather than slug.
  const normalizeSlug = (s) => (s || '').replace(/-/g, '_').toLowerCase()
  const slugSource = task.status && task.status.trim() ? task.status : ''
  const currentSlug = normalizeSlug(slugSource)
  const currentLabel = (task.workflow_status || '').trim().toLowerCase()
  const currentIdx = orderedStatuses.findIndex(s => {
    if (currentSlug && normalizeSlug(s.name) === currentSlug) return true
    if (currentLabel && (s.label || '').toLowerCase() === currentLabel) return true
    return false
  })

  // Also normalize transitions for matching
  const normalizedTransitions = transitions.map(t => ({
    ...t,
    to_status: t.to_status,
    to_status_label: t.to_status_label || t.name || t.to_status,
  }))

  return (
    <div className="card p-4 space-y-4">
      {/* Workflow header */}
      <div className="flex items-center gap-2">
        <GitBranch size={16} className="text-brand-500" />
        {/* Clickable workflow name — opens switcher. Matches Jira's
            scheme-migration UX: click the workflow name → pick target →
            server auto-maps current status into the new workflow. */}
        <WorkflowSwitcher task={task} onChanged={onTaskUpdate} />
        {task.assignment_group && (
          <span className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
            <Users size={12} />
            {teamLabel}
          </span>
        )}
        <button
          onClick={() => setShowDiagram(v => !v)}
          className={cn(
            'ml-auto p-1.5 rounded-md transition-colors',
            showDiagram
              ? 'bg-brand-100 dark:bg-brand-900/50 text-brand-600 dark:text-brand-400'
              : 'text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'
          )}
          title="View workflow diagram"
        >
          <Map size={15} />
        </button>
      </div>

      {/* Status pipeline */}
      {orderedStatuses.length > 0 && (
        <div ref={pipelineRef} className="flex items-center gap-1 overflow-x-auto py-1.5 -mx-1 px-1">
          {orderedStatuses.map((s, i) => {
            const isCurrent = i === currentIdx
            const isPast = i < currentIdx
            return (
              <div
                key={s.name}
                ref={isCurrent ? currentPillRef : null}
                className="flex items-center gap-1 shrink-0"
              >
                {i > 0 && (
                  <ChevronRight
                    size={14}
                    className={cn(
                      isPast || isCurrent ? 'text-brand-400' : 'text-gray-300 dark:text-gray-700'
                    )}
                  />
                )}
                <StatusPill status={s} isCurrent={isCurrent} isPast={isPast} />
              </div>
            )
          })}
        </div>
      )}

      {/* Workflow diagram — fullscreen modal overlay.
          Mount even while the definition is loading; gating on workflowData
          made the map button look dead when the catalogue miss returned null. */}
      {showDiagram && createPortal(
        <div
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={(e) => { if (e.target === e.currentTarget) setShowDiagram(false) }}
        >
          <div className="bg-white dark:bg-gray-900 rounded-xl shadow-2xl w-[90vw] h-[85vh] max-w-[1400px] flex flex-col overflow-hidden border border-gray-200 dark:border-gray-700">
            {/* Header */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 shrink-0">
              <div className="flex items-center gap-3">
                <GitBranch size={18} className="text-brand-500" />
                <span className="font-semibold text-gray-900 dark:text-white">
                  {workflowData?.name || task.workflow_name}
                </span>
                {task.assignment_group && (
                  <span className="flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 px-2 py-0.5 rounded">
                    <Users size={13} />
                    {teamLabel}
                  </span>
                )}
                <span className="text-sm text-gray-400 dark:text-gray-500">
                  Current: <span className="text-brand-600 dark:text-brand-400 font-medium">{task.workflow_status || task.status}</span>
                </span>
              </div>
              <button
                onClick={() => setShowDiagram(false)}
                className="p-2 rounded-lg text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors"
              >
                <X size={20} />
              </button>
            </div>
            {/* Diagram */}
            <div className="flex-1 overflow-auto p-4">
              {isWorkflowLoading && (
                <div className="h-full flex items-center justify-center gap-2 text-sm text-gray-500 dark:text-gray-400">
                  <Loader2 size={18} className="animate-spin" />
                  Loading workflow
                </div>
              )}
              {!isWorkflowLoading && !workflowData && (
                <div className="h-full flex items-center justify-center text-sm text-gray-500 dark:text-gray-400">
                  Could not load the {task.workflow_name} workflow definition.
                </div>
              )}
              {workflowData && (
                <WorkflowDiagram
                  statuses={orderedStatuses.map(s => ({
                    name: s.name,
                    label: s.label,
                    category: s.category,
                    color: s.color,
                  }))}
                  transitions={(workflowData.transitions || []).map(t => ({
                    id: t.id,
                    from_status: t.from_status,
                    to_status: t.to_status,
                    name: t.name,
                    post_functions: t.post_functions,
                    knowledge_articles: t.knowledge_articles,
                  }))}
                  initialStatus={workflowData.initial_status_slug || workflowData.initial_status_name || ''}
                  selectedStatus={orderedStatuses[currentIdx]?.name || currentSlug}
                  currentStatus={orderedStatuses[currentIdx]?.name || currentSlug}
                />
              )}
            </div>
            {/* Footer — knowledge base */}
            {workflowData?.knowledge_base?.length > 0 && (
              <div className="border-t border-gray-200 dark:border-gray-700 px-5 py-3 bg-gray-50 dark:bg-gray-800/50 shrink-0">
                <div className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Knowledge Base</div>
                <div className="flex gap-3 overflow-x-auto">
                  {workflowData.knowledge_base.map((ka, i) => {
                    // External links open in a new tab. Inline + wiki articles
                    // open a detail view inside the modal so the user doesn't
                    // lose their place on the workflow diagram.
                    const isExternal = ka.type === 'link' && ka.url
                    const handleClick = () => {
                      if (isExternal) {
                        window.open(ka.url, '_blank', 'noopener,noreferrer')
                      } else {
                        setSelectedArticle(ka)
                      }
                    }
                    return (
                      <button
                        key={i}
                        type="button"
                        onClick={handleClick}
                        className="flex items-start gap-2 text-left text-xs text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2 min-w-[200px] max-w-[350px] shrink-0 hover:border-brand-400 hover:shadow-sm transition-colors cursor-pointer"
                        title={isExternal ? 'Open link' : 'View article'}
                      >
                        <span className="shrink-0">{ka.type === 'inline' ? '📋' : '🔗'}</span>
                        <div>
                          <div className="font-medium text-gray-800 dark:text-gray-200">{ka.title}</div>
                          <div className="mt-0.5 line-clamp-2">{ka.content || ka.url}</div>
                        </div>
                      </button>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        </div>,
        document.body
      )}

      {/* Knowledge article detail — rendered on top of the diagram modal.
          The diagram modal uses z-[9999]; this needs to be strictly greater
          so the article overlays instead of hiding behind an opaque card. */}
      {selectedArticle && createPortal(
        <div
          className="fixed inset-0 z-[10000] flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={(e) => { if (e.target === e.currentTarget) setSelectedArticle(null) }}
        >
          <div className="bg-white dark:bg-gray-900 rounded-xl shadow-2xl w-[92vw] max-w-3xl max-h-[88vh] flex flex-col overflow-hidden">
            <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200 dark:border-gray-700 shrink-0">
              <div className="flex items-center gap-2 min-w-0">
                <span className="shrink-0">{selectedArticle.type === 'inline' ? '📋' : '🔗'}</span>
                <h3 className="text-sm font-semibold text-gray-900 dark:text-white truncate">
                  {selectedArticle.title || 'Knowledge Article'}
                </h3>
                {selectedArticle.type && selectedArticle.type !== 'inline' && (
                  <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 shrink-0">
                    {selectedArticle.type}
                  </span>
                )}
              </div>
              <button
                onClick={() => setSelectedArticle(null)}
                className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 shrink-0"
                title="Close"
              >
                <X size={18} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {selectedArticle.content ? (
                // KA content is authored in Markdown. We render with
                // react-markdown + remark-gfm so GitHub-style extensions work
                // (tables, task lists, strikethrough, autolinks). The `prose`
                // classes come from @tailwindcss/typography and style
                // headings, lists, code blocks, etc. without bespoke CSS.
                <div className="prose prose-sm dark:prose-invert max-w-none">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {selectedArticle.content}
                  </ReactMarkdown>
                </div>
              ) : selectedArticle.url ? (
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  <p className="mb-3">External reference:</p>
                  <a
                    href={selectedArticle.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-brand-600 dark:text-brand-400 hover:underline break-all"
                  >
                    {selectedArticle.url}
                    <ArrowRight size={14} />
                  </a>
                </div>
              ) : (
                <p className="text-sm text-gray-500 dark:text-gray-400 italic">No content available.</p>
              )}
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* Transition buttons */}
      {transitions.length > 0 && (
        <div className="space-y-2">
          <label className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
            Transitions
          </label>
          <div className="flex flex-wrap gap-2">
            {transitions.map((t) => {
              const targetStatus = orderedStatuses.find(s => s.name === t.to_status)
              const isCancelType = targetStatus?.category === 'cancelled' || targetStatus?.category === 'done'
              const transitionKey = t.id || t.to_status
              const isActive = showCommentFor === transitionKey
              const hasRequiredScreen = (t.screen_fields || []).some(f => f.required)

              return (
                <div key={transitionKey} className="flex flex-col">
                  <button
                    disabled={transitionMutation.isPending}
                    onClick={() => {
                      const isCancel = targetStatus?.category === 'cancelled'
                      const rcField = (t.screen_fields || []).find(f => f.field === 'reason_category')
                      const needsCategory = isCancel && (rcField?.enum || []).length > 0
                      const canExec = (!hasRequiredScreen || transitionComment.trim()) && (!needsCategory || reasonCategory)
                      if (isActive && canExec) {
                        // Execute (passes reason_category for cancel/defer)
                        transitionMutation.mutate({ toStatus: t.to_status, transitionId: t.id, transitionName: t.name, comment: transitionComment, reasonCategory })
                      } else {
                        // Open the input panel (comment + reason-category select for cancel)
                        setShowCommentFor(isActive ? null : transitionKey)
                        setTransitionComment('')
                        setReasonCategory('')
                      }
                    }}
                    className={cn(
                      'px-3 py-2 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5',
                      transitionMutation.isPending ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer',
                      isCancelType
                        ? 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                        : t.button_style === 'danger'
                          ? 'bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-900/50'
                          : 'bg-brand-50 dark:bg-brand-900/30 text-brand-700 dark:text-brand-300 hover:bg-brand-100 dark:hover:bg-brand-900/50',
                    )}
                    style={t.button_style === 'custom' && targetStatus?.color ? {
                      backgroundColor: targetStatus.color + '15',
                      color: targetStatus.color,
                    } : undefined}
                  >
                    {transitionMutation.isPending ? (
                      <Loader2 size={14} className="animate-spin" />
                    ) : (
                      <ArrowRight size={14} />
                    )}
                    {t.name || targetStatus?.label || t.to_status}
                  </button>
                </div>
              )
            })}
          </div>

          {/* Comment input (+ reason-category select for Cancel/Defer) */}
          {showCommentFor && (() => {
            const activeTransition = transitions.find(t => (t.id || t.to_status) === showCommentFor)
            const activeTarget = orderedStatuses.find(s => s.name === activeTransition?.to_status)
            const isCancel = activeTarget?.category === 'cancelled'
            // PLS-756: reason-category options come from server screen_fields.
            const rcField = (activeTransition?.screen_fields || []).find(f => f.field === 'reason_category')
            const reasonOptions = rcField?.enum || []
            const needsCategory = isCancel && reasonOptions.length > 0
            const requiredFields = (activeTransition?.screen_fields || []).filter(f => f.required)
            const isRequired = requiredFields.length > 0
            const label = requiredFields[0]?.label || (isRequired ? 'Required comment' : 'Add a comment')
            const blocked = (isRequired && !transitionComment.trim()) || (needsCategory && !reasonCategory)
            const doMutate = () => {
              if (blocked || !activeTransition) return
              transitionMutation.mutate({
                toStatus: activeTransition.to_status,
                transitionId: activeTransition.id,
                transitionName: activeTransition.name,
                comment: transitionComment,
                reasonCategory,
              })
            }
            return (
            <div className="flex items-center gap-2 mt-2 flex-wrap">
              {isCancel && reasonOptions.length > 0 && (
                <select
                  value={reasonCategory}
                  onChange={(e) => setReasonCategory(e.target.value)}
                  className={cn('input text-sm', !reasonCategory && 'ring-2 ring-amber-400')}
                  title="Reason category (required to cancel)"
                >
                  <option value="">Reason category…</option>
                  {reasonOptions.map(o => <option key={o} value={o}>{o}</option>)}
                </select>
              )}
              <MessageSquare size={14} className={cn('shrink-0', (isRequired || isCancel) ? 'text-amber-500' : 'text-gray-400')} />
              <input
                type="text"
                value={transitionComment}
                onChange={(e) => setTransitionComment(e.target.value)}
                placeholder={isCancel ? 'Reason detail (optional)...' : (isRequired ? `${label} (required)...` : 'Add a comment (optional)...')}
                className={cn('input flex-1 text-sm', isRequired && !transitionComment.trim() && 'ring-2 ring-amber-400')}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') doMutate()
                  if (e.key === 'Escape') setShowCommentFor(null)
                }}
                autoFocus
              />
              <button
                onClick={doMutate}
                disabled={transitionMutation.isPending || blocked}
                className={cn('btn-primary btn-sm', blocked && 'opacity-50 cursor-not-allowed')}
              >
                {transitionMutation.isPending ? 'Moving...' : 'Move'}
              </button>
              <button
                onClick={() => setShowCommentFor(null)}
                className="btn-ghost btn-sm text-gray-400"
              >
                Cancel
              </button>
            </div>
            )
          })()}
        </div>
      )}

      {/* No transitions available */}
      {transitions.length === 0 && task.workflow_status && (() => {
        const currentStatus = orderedStatuses.find(
          s => normalizeSlug(s.name) === normalizeSlug(task.workflow_status)
        )
        const label = currentStatus?.label
          || task.workflow_status.split(/[_-]/).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
        return (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            No transitions available from <strong>{label}</strong>
          </p>
        )
      })()}
    </div>
  )
}


// --------------------------------------------------------------------------
// WorkflowSwitcher — click the workflow name to pick a different workflow.
//
// Mirrors Jira's scheme-migration wizard (click workflow → pick target →
// server auto-maps current status). Fires the /change-workflow/ endpoint;
// server uses Item._coerce_invalid_status() to pick the right slug in the
// target (legacy alias → category match → workflow initial).
// --------------------------------------------------------------------------
function WorkflowSwitcher({ task, onChanged }) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef(null)
  const queryClient = useQueryClient()
  const toast = useToast()

  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    if (open) document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const { data: workflowsResp } = useQuery({
    queryKey: ['workflows-list'],
    queryFn: async () => (await workflowsApi.listJiraWorkflows({ page_size: 200 })).data,
    enabled: open,
    staleTime: 60_000,
  })

  const mutation = useMutation({
    mutationFn: (name) => itemsApi.changeWorkflow(task.id, { workflow: name }),
    onSuccess: (res) => {
      const d = res?.data || {}
      toast.success(
        `Workflow → ${d.workflow_name || 'updated'} (status: ${d.status || '?'})`
      )
      queryClient.invalidateQueries({ queryKey: ['task'] })
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      queryClient.invalidateQueries({ queryKey: ['task-workflow', task.workflow_name] })
      queryClient.invalidateQueries({ queryKey: ['available-transitions', task.id] })
      onChanged?.()
      setOpen(false)
    },
    onError: (err) => {
      const msg = err?.response?.data?.error || err?.message || 'Could not change workflow'
      toast.error(msg)
    },
  })

  const workflows = Array.isArray(workflowsResp)
    ? workflowsResp
    : (workflowsResp?.results || [])

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className={cn(
          'flex items-center gap-1 text-sm font-medium text-gray-900 dark:text-white',
          'px-1.5 py-0.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800',
        )}
        title="Change workflow"
      >
        {task.workflow_name || 'No workflow'}
        <ChevronDown size={12} className="text-gray-400" />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-64 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg py-1">
          <div className="px-3 py-1.5 text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Switch workflow
          </div>
          {workflows.length === 0 && (
            <div className="px-3 py-2 text-xs text-gray-500">Loading…</div>
          )}
          {workflows.map(wf => {
            const isCurrent =
              (wf.name || '').toLowerCase() === (task.workflow_name || '').toLowerCase()
              || (wf.slug || '').toLowerCase() === (task.workflow_name || '').toLowerCase()
            return (
              <button
                key={wf.id || wf.slug || wf.name}
                onClick={() => !isCurrent && mutation.mutate(wf.name || wf.slug)}
                disabled={isCurrent || mutation.isPending}
                className={cn(
                  'w-full flex items-center justify-between gap-2 px-3 py-1.5 text-sm text-left',
                  isCurrent
                    ? 'text-gray-400 dark:text-gray-600 cursor-default'
                    : 'text-gray-900 dark:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-700/60'
                )}
              >
                <span>{wf.name}</span>
                {isCurrent && <span className="text-[10px] uppercase tracking-wide">current</span>}
              </button>
            )
          })}
          <div className="border-t border-gray-200 dark:border-gray-700 mt-1 pt-1">
            <p className="px-3 py-1 text-[11px] text-gray-500 dark:text-gray-400">
              Current status auto-maps (category match → initial).
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
