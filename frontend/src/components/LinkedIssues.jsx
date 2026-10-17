import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Link as LinkIcon,
  Plus,
  Trash2,
  ArrowRight,
  Ban,
  Copy,
  GitBranch,
  Loader2,
  X,
  Search,
  CheckCircle,
  Circle,
} from 'lucide-react'
import { itemsApi } from '../services/api'
import { cn } from '../utils/cn'
import { formatStatus } from '../utils/format'

// Link type configurations
const LINK_TYPES = [
  { value: 'blocks', label: 'Blocks', description: 'This issue blocks' },
  { value: 'is_blocked_by', label: 'Is Blocked By', description: 'This issue is blocked by' },
  { value: 'relates_to', label: 'Relates To', description: 'This issue relates to' },
  { value: 'duplicates', label: 'Duplicates', description: 'This issue duplicates' },
  { value: 'is_duplicated_by', label: 'Is Duplicated By', description: 'This issue is duplicated by' },
  { value: 'parent_of', label: 'Parent Of', description: 'This issue is parent of' },
  { value: 'child_of', label: 'Child Of', description: 'This issue is child of' },
]

const LINK_ICONS = {
  blocks: Ban,
  is_blocked_by: Ban,
  relates_to: LinkIcon,
  duplicates: Copy,
  is_duplicated_by: Copy,
  parent_of: GitBranch,
  child_of: GitBranch,
  clones: Copy,
  is_cloned_by: Copy,
}

const TERMINAL_STATUSES = new Set([
  'completed',
  'cancelled',
  'canceled',
  'closed',
  'done',
  'resolved',
  'rejected',
  'duplicate',
  'wont_fix',
  'merged',
  'failed',
  'expired',
  'deferred',
])

function isTerminalStatus(status) {
  return TERMINAL_STATUSES.has(String(status || '').toLowerCase())
}

function linkedItemStatus(link, isOutward) {
  return isOutward ? link.target_item_status : link.source_item_status
}

function LinkedItem({ link, isOutward, onDelete, deleting, compact = false }) {
  const item = isOutward ? link.target_item : link.source_item
  const itemKey = isOutward ? link.target_item_key : link.source_item_key
  const itemTitle = isOutward ? link.target_item_title : link.source_item_title
  const itemStatus = linkedItemStatus(link, isOutward)
  const itemStatusLabel = formatStatus(itemStatus)
  const linkType = link.link_type_display

  // Route by key if available, otherwise by ID
  const linkTo = itemKey ? `/tasks/${itemKey}` : `/tasks/${item}`

  const Icon = LINK_ICONS[link.link_type] || LinkIcon
  const isTerminal = isTerminalStatus(itemStatus)

  // Compact mode — used in sidebar, grouped by type so we skip the type label
  if (compact) {
    return (
      <Link
        to={linkTo}
        className={cn(
          'flex items-center gap-2 p-1.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800/50 transition-colors group min-w-0',
          isTerminal && 'opacity-60'
        )}
      >
        <Icon size={13} className="text-gray-400 flex-shrink-0" />
        <span className={cn(
            'text-xs font-semibold flex-shrink-0',
            isTerminal ? 'line-through text-gray-400' : 'text-gray-900 dark:text-white'
          )}>
          {itemKey || `#${item}`}
        </span>
        {itemTitle && (
          <span className={cn(
            'text-xs truncate min-w-0',
            isTerminal ? 'line-through text-gray-400' : 'text-gray-500 dark:text-gray-400'
          )}>
            {itemTitle}
          </span>
        )}
        {itemStatus && (
          <span className={cn(
            'flex-shrink-0 ml-auto inline-flex items-center gap-1 text-[11px]',
            isTerminal ? 'text-green-500' : 'text-gray-400'
          )}>
            {isTerminal ? <CheckCircle size={12} /> : <Circle size={12} />}
            {itemStatusLabel}
          </span>
        )}
      </Link>
    )
  }

  // Full mode — used in the Links tab with delete buttons and more spacing
  return (
    <div className="flex items-center gap-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-800 group">
      <Icon size={16} className="text-gray-400 flex-shrink-0" />

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {linkType}
          </span>
          <ArrowRight size={12} className="text-gray-400" />
          <Link
            to={linkTo}
            className={cn(
              'text-sm font-medium hover:text-brand-600 dark:hover:text-brand-400 hover:underline',
              isTerminal ? 'line-through text-gray-400' : 'text-gray-900 dark:text-white'
            )}
          >
            {itemKey || `#${item}`}
          </Link>
        </div>
        {itemTitle && (
          <Link
            to={linkTo}
            className={cn(
              'text-sm truncate mt-0.5 block hover:text-brand-600 dark:hover:text-brand-400',
              isTerminal ? 'line-through text-gray-400' : 'text-gray-600 dark:text-gray-400'
            )}
          >
            {itemTitle}
          </Link>
        )}
      </div>

      {itemStatus && (
        <span className={cn('text-xs inline-flex items-center gap-1', isTerminal ? 'text-green-500' : 'text-gray-400')}>
          {isTerminal ? <CheckCircle size={14} /> : <Circle size={14} />}
          {itemStatusLabel}
        </span>
      )}

      <button
        onClick={() => onDelete(link.id)}
        disabled={deleting}
        className="btn-icon text-gray-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
        title="Remove link"
      >
        <Trash2 size={14} />
      </button>
    </div>
  )
}

function AddLinkForm({ itemId, onClose, onSuccess }) {
  const [linkType, setLinkType] = useState('relates_to')
  const [targetKey, setTargetKey] = useState('')
  const [error, setError] = useState('')

  const createMutation = useMutation({
    mutationFn: (data) => itemsApi.createLink(itemId, data),
    onSuccess: () => {
      onSuccess()
      onClose()
    },
    onError: (err) => {
      setError(err.response?.data?.error || 'Failed to create link')
    },
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    setError('')

    if (!targetKey.trim()) {
      setError('Please enter an issue key')
      return
    }

    createMutation.mutate({
      target_item_key: targetKey.trim().toUpperCase(),
      link_type: linkType,
    })
  }

  return (
    <form onSubmit={handleSubmit} className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-4">
      <div className="flex items-center justify-between">
        <h4 className="font-medium text-gray-900 dark:text-white">Link Issue</h4>
        <button
          type="button"
          onClick={onClose}
          className="btn-icon text-gray-400 hover:text-gray-600"
        >
          <X size={16} />
        </button>
      </div>

      <div>
        <label className="text-sm text-gray-600 dark:text-gray-400">Link Type</label>
        <select
          value={linkType}
          onChange={(e) => setLinkType(e.target.value)}
          className="input mt-1 w-full"
        >
          {LINK_TYPES.map(type => (
            <option key={type.value} value={type.value}>
              {type.description}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="text-sm text-gray-600 dark:text-gray-400">Issue Key</label>
        <div className="relative mt-1">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            value={targetKey}
            onChange={(e) => setTargetKey(e.target.value)}
            placeholder="e.g., PROJ-123"
            className="input pl-9 w-full"
          />
        </div>
      </div>

      {error && (
        <p className="text-sm text-red-500">{error}</p>
      )}

      <div className="flex gap-2 justify-end">
        <button
          type="button"
          onClick={onClose}
          className="btn-secondary btn-sm"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={createMutation.isPending || !targetKey.trim()}
          className="btn-primary btn-sm"
        >
          {createMutation.isPending ? 'Linking...' : 'Link'}
        </button>
      </div>
    </form>
  )
}

// Labels for link type groups (Jira-style)
const LINK_TYPE_LABELS = {
  blocks: 'Blocks',
  is_blocked_by: 'Is blocked by',
  relates_to: 'Relates to',
  duplicates: 'Duplicates',
  is_duplicated_by: 'Is duplicated by',
  parent_of: 'Parent of',
  child_of: 'Child of',
  clones: 'Clones',
  is_cloned_by: 'Is cloned by',
}

/**
 * Compact inline links display — shows on the main task page without needing to click the Links tab.
 * Groups links by relationship type like Jira.
 *
 * Props:
 *  - itemId: the item whose links to show
 *  - embedded: when true, renders without the card wrapper (for nesting inside another card).
 *              Also renders a top divider when there are links, so it separates from preceding content.
 */
export function InlineLinkedIssues({ itemId, embedded = false }) {
  const { data: linksData } = useQuery({
    queryKey: ['task-links', itemId],
    queryFn: async () => {
      const res = await itemsApi.getLinks(itemId)
      return res.data
    },
    enabled: !!itemId,
  })

  const outwardLinks = linksData?.outward_links || []
  const inwardLinks = linksData?.inward_links || []
  const allLinks = [...outwardLinks, ...inwardLinks]

  if (allLinks.length === 0) return null

  // Group by link_type. Keep direction per link; inverse groups can mix inward
  // and outward links on unusual issue graphs.
  const grouped = {}
  const addGroupedLink = (type, link, isOutward) => {
    if (!grouped[type]) grouped[type] = []
    grouped[type].push({ link, isOutward })
  }

  outwardLinks.forEach(link => {
    const type = link.link_type || 'relates_to'
    addGroupedLink(type, link, true)
  })
  inwardLinks.forEach(link => {
    // For inward links, show the inverse label
    const type = link.link_type || 'relates_to'
    const inverseType = {
      blocks: 'is_blocked_by', is_blocked_by: 'blocks',
      duplicates: 'is_duplicated_by', is_duplicated_by: 'duplicates',
      parent_of: 'child_of', child_of: 'parent_of',
      clones: 'is_cloned_by', is_cloned_by: 'clones',
      relates_to: 'relates_to',
    }[type] || type
    addGroupedLink(inverseType, link, false)
  })

  const groups = Object.entries(grouped).map(([type, entries]) => {
    const terminalCount = entries.filter(({ link, isOutward }) => (
      isTerminalStatus(linkedItemStatus(link, isOutward))
    )).length
    const activeCount = entries.length - terminalCount
    const countLabel = terminalCount > 0
      ? `${activeCount} active, ${terminalCount} terminal`
      : String(activeCount)

    return (
      <div key={type} className="space-y-1">
        <span className="text-xs text-gray-500 dark:text-gray-400 font-medium">
          {LINK_TYPE_LABELS[type] || type.replace(/_/g, ' ')} ({countLabel})
        </span>
        <div className="space-y-0.5">
          {entries.map(({ link, isOutward }) => (
            <LinkedItem
              key={`${isOutward ? 'out' : 'in'}-${link.id}`}
              link={link}
              isOutward={isOutward}
              onDelete={() => {}}
              deleting={false}
              compact={true}
            />
          ))}
        </div>
      </div>
    )
  })

  // Embedded: render as peer groups (no card wrapper, no own header — parent provides that)
  if (embedded) {
    return <div className="space-y-3">{groups}</div>
  }

  // Standalone: render with card wrapper and its own header
  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center gap-2">
        <LinkIcon size={14} className="text-gray-500 dark:text-gray-400" />
        <h4 className="font-medium text-gray-900 dark:text-gray-100">
          Linked Issues ({allLinks.length})
        </h4>
      </div>
      {groups}
    </div>
  )
}

export default function LinkedIssues({ itemId, itemKey }) {
  const queryClient = useQueryClient()
  const [showAddForm, setShowAddForm] = useState(false)

  // Fetch links
  const { data: linksData, isLoading } = useQuery({
    queryKey: ['task-links', itemId],
    queryFn: async () => {
      const res = await itemsApi.getLinks(itemId)
      return res.data
    },
    enabled: !!itemId,
  })

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (linkId) => itemsApi.deleteLink(itemId, linkId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task-links', itemId] })
      queryClient.invalidateQueries({ queryKey: ['task-activity', itemId] })
    },
  })

  const handleLinkCreated = () => {
    queryClient.invalidateQueries({ queryKey: ['task-links', itemId] })
    queryClient.invalidateQueries({ queryKey: ['task-activity', itemId] })
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="animate-spin text-gray-400" size={24} />
      </div>
    )
  }

  const outwardLinks = linksData?.outward_links || []
  const inwardLinks = linksData?.inward_links || []
  const hasLinks = outwardLinks.length > 0 || inwardLinks.length > 0

  return (
    <div className="space-y-4">
      {/* Add Link Button */}
      {!showAddForm && (
        <button
          onClick={() => setShowAddForm(true)}
          className="flex items-center gap-2 text-sm text-brand-600 dark:text-brand-400 hover:text-brand-700 dark:hover:text-brand-300"
        >
          <Plus size={16} />
          Link Issue
        </button>
      )}

      {/* Add Link Form */}
      {showAddForm && (
        <AddLinkForm
          itemId={itemId}
          onClose={() => setShowAddForm(false)}
          onSuccess={handleLinkCreated}
        />
      )}

      {/* Links List */}
      {!hasLinks ? (
        <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
          No linked issues
        </p>
      ) : (
        <div className="space-y-3">
          {/* Outward Links */}
          {outwardLinks.length > 0 && (
            <div className="space-y-2">
              {outwardLinks.map(link => (
                <LinkedItem
                  key={link.id}
                  link={link}
                  isOutward={true}
                  onDelete={(id) => deleteMutation.mutate(id)}
                  deleting={deleteMutation.isPending}
                />
              ))}
            </div>
          )}

          {/* Inward Links */}
          {inwardLinks.length > 0 && (
            <div className="space-y-2">
              {inwardLinks.map(link => (
                <LinkedItem
                  key={link.id}
                  link={link}
                  isOutward={false}
                  onDelete={(id) => deleteMutation.mutate(id)}
                  deleting={deleteMutation.isPending}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
