import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { GitPullRequest, ExternalLink, Plus, X, Trash2 } from 'lucide-react'
import { itemsApi } from '../services/api'
import { cn } from '../utils/cn'

const STATUS_COLORS = {
  open: 'bg-blue-500',
  merged: 'bg-purple-500',
  closed: 'bg-red-500',
  draft: 'bg-zinc-500',
}

const STATUS_OPTIONS = [
  { value: 'open', label: 'Open' },
  { value: 'draft', label: 'Draft' },
  { value: 'merged', label: 'Merged' },
  { value: 'closed', label: 'Closed' },
]

// Parse a GitHub/GitLab PR URL to extract repo and number
function parsePrUrl(url) {
  try {
    const u = new URL(url)
    // GitHub: /owner/repo/pull/123
    // GitLab: /owner/repo/-/merge_requests/123
    const parts = u.pathname.split('/').filter(Boolean)
    const ghIdx = parts.indexOf('pull')
    const glIdx = parts.indexOf('merge_requests')
    if (ghIdx >= 2) {
      return { repo: `${parts[0]}/${parts[1]}`, number: parseInt(parts[ghIdx + 1]) || null }
    }
    if (glIdx >= 2) {
      const owner = parts[0]
      const repoName = parts[1]
      return { repo: `${owner}/${repoName}`, number: parseInt(parts[glIdx + 1]) || null }
    }
    return { repo: u.hostname + u.pathname, number: null }
  } catch {
    return { repo: '', number: null }
  }
}

export default function PullRequests({ task, alwaysShow = false }) {
  const queryClient = useQueryClient()
  const [showAddForm, setShowAddForm] = useState(false)
  const [newPrUrl, setNewPrUrl] = useState('')
  const [newPrStatus, setNewPrStatus] = useState('open')

  const pullRequests = task?.metadata?.pull_requests || []

  const updateMutation = useMutation({
    mutationFn: (newPrs) => itemsApi.update(task.id, {
      metadata: { ...(task.metadata || {}), pull_requests: newPrs },
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['task'] })
    },
  })

  const handleAddPr = (e) => {
    e.preventDefault()
    if (!newPrUrl.trim()) return
    const parsed = parsePrUrl(newPrUrl.trim())
    const newPr = {
      url: newPrUrl.trim(),
      status: newPrStatus,
      repo: parsed.repo,
      number: parsed.number,
      title: `PR #${parsed.number || '?'}`,
    }
    updateMutation.mutate([...pullRequests, newPr])
    setNewPrUrl('')
    setNewPrStatus('open')
    setShowAddForm(false)
  }

  const handleRemovePr = (index) => {
    const updated = pullRequests.filter((_, i) => i !== index)
    updateMutation.mutate(updated)
  }

  // Hide entirely when there's nothing to show and not always-visible
  if (!alwaysShow && pullRequests.length === 0) return null
  if (!task) return null

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider flex items-center gap-1.5">
          <GitPullRequest className="w-3.5 h-3.5" />
          Pull Requests {pullRequests.length > 0 && `(${pullRequests.length})`}
        </h3>
        {!showAddForm && (
          <button
            onClick={() => setShowAddForm(true)}
            className="p-1 text-gray-400 hover:text-brand-600 dark:hover:text-brand-400 transition-colors"
            title="Link pull request"
          >
            <Plus size={14} />
          </button>
        )}
      </div>

      {/* PR List */}
      {pullRequests.length > 0 ? (
        <div className="space-y-1.5">
          {pullRequests.map((pr, i) => (
            <div
              key={i}
              className="flex items-center gap-2 p-2 rounded-md bg-gray-50 dark:bg-gray-800/50 group"
            >
              <span className={cn('w-2 h-2 rounded-full flex-shrink-0', STATUS_COLORS[pr.status] || STATUS_COLORS.open)} />
              <a
                href={pr.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex-1 min-w-0 hover:text-brand-600 dark:hover:text-brand-400"
              >
                <p className="text-sm text-gray-800 dark:text-gray-200 truncate">
                  {pr.title || `PR #${pr.number || '?'}`}
                </p>
                {pr.repo && (
                  <p className="text-[11px] text-gray-500 truncate">
                    {pr.repo}{pr.number ? `#${pr.number}` : ''} · {pr.status || 'open'}
                  </p>
                )}
              </a>
              <a
                href={pr.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                <ExternalLink className="w-3 h-3" />
              </a>
              <button
                onClick={() => handleRemovePr(i)}
                disabled={updateMutation.isPending}
                className="text-gray-400 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                title="Remove"
              >
                <Trash2 className="w-3 h-3" />
              </button>
            </div>
          ))}
        </div>
      ) : !showAddForm ? (
        <p className="text-xs text-gray-500 dark:text-gray-400 italic">
          No pull requests linked yet
        </p>
      ) : null}

      {/* Add PR Form */}
      {showAddForm && (
        <form onSubmit={handleAddPr} className="mt-2 space-y-2">
          <input
            type="url"
            value={newPrUrl}
            onChange={(e) => setNewPrUrl(e.target.value)}
            placeholder="https://github.com/owner/repo/pull/123"
            className="input text-sm py-1.5 w-full"
            autoFocus
            required
          />
          <div className="flex items-center gap-2">
            <select
              value={newPrStatus}
              onChange={(e) => setNewPrStatus(e.target.value)}
              className="input text-xs py-1 w-auto"
            >
              {STATUS_OPTIONS.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
            <div className="flex-1" />
            <button
              type="button"
              onClick={() => { setShowAddForm(false); setNewPrUrl('') }}
              className="btn-secondary btn-sm text-xs"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!newPrUrl.trim() || updateMutation.isPending}
              className="btn-primary btn-sm text-xs"
            >
              {updateMutation.isPending ? 'Adding...' : 'Add'}
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
