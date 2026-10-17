import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Archive as ArchiveIcon, Search, X } from 'lucide-react'
import { tasksApi } from '../services/api'
import { PageLoader } from '../components/LoadingSpinner'
import EmptyState from '../components/EmptyState'
import { cn } from '../utils/cn'

/**
 * Archive view — browses Done-category items that have been auto-archived
 * (default after 28 days via the daily `sweep_archive_done_items` Celery job).
 *
 * Rationale for having this as a separate page rather than a filter on Tasks:
 *   - The main views (Dashboard, Tasks, Board) default-exclude archived items
 *     so that active work stays front and center even after thousands of
 *     historical tasks accumulate.
 *   - Users who want to browse historical items get a dedicated page that
 *     exposes the right affordances (search, fetch more, resolution/date
 *     columns) without polluting the primary backlog view.
 *
 * The page queries `/api/items/?archived_only=true` which bypasses the default
 * `is_archived=false` filter and returns ONLY archived items.
 */
export default function Archive() {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)

  const { data, isLoading } = useQuery({
    queryKey: ['archive', page, search],
    queryFn: () => tasksApi.list({
      archived_only: 'true',
      page_size: 50,
      page,
      ...(search.trim() ? { search: search.trim() } : {}),
    }).then(r => r.data),
  })

  const items = data?.results || []
  const total = data?.count || 0
  const hasMore = !!data?.next

  const formatDate = (iso) => {
    if (!iso) return '—'
    try {
      const d = new Date(iso)
      return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
    } catch { return '—' }
  }

  if (isLoading && page === 1) return <PageLoader />

  return (
    <div className="p-4 max-w-6xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center gap-3">
        <ArchiveIcon size={24} className="text-gray-500 dark:text-gray-400" />
        <div>
          <h1 className="text-xl font-semibold text-gray-900 dark:text-white">Archive</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {total > 0 ? `${total.toLocaleString()} archived ${total === 1 ? 'task' : 'tasks'}` : 'No archived tasks yet'}
            {' — auto-archived after 28 days in a Done state.'}
          </p>
        </div>
      </div>

      {/* Search */}
      <div className="relative">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
        <input
          type="text"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1) }}
          placeholder="Search archived tasks..."
          className="input pl-9 pr-9 w-full"
        />
        {search && (
          <button
            onClick={() => { setSearch(''); setPage(1) }}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
          >
            <X size={16} />
          </button>
        )}
      </div>

      {/* List */}
      {items.length === 0 ? (
        <EmptyState
          icon={ArchiveIcon}
          title="No archived tasks"
          description={
            search
              ? `No archived tasks match "${search}"`
              : "Tasks will land here automatically after they've been in a Done state for 28 days."
          }
        />
      ) : (
        <div className="card divide-y divide-gray-200 dark:divide-gray-700">
          {items.map((item) => (
            <button
              key={item.id}
              onClick={() => navigate(`/tasks/${item.item_key || item.id}`)}
              className={cn(
                'w-full flex items-center justify-between gap-4 p-3 text-left',
                'hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors'
              )}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400">
                  <span className="font-mono font-medium">
                    {item.item_key || `#${item.id}`}
                  </span>
                  <span>•</span>
                  <span>{item.status?.replace(/_/g, ' ')}</span>
                  {item.resolution && (
                    <>
                      <span>•</span>
                      <span>{String(item.resolution).toLowerCase()}</span>
                    </>
                  )}
                </div>
                <div className="mt-0.5 text-sm text-gray-900 dark:text-white line-through decoration-gray-400/60 truncate">
                  {item.title}
                </div>
              </div>
              <div className="flex-shrink-0 text-right text-[11px] text-gray-500 dark:text-gray-400">
                <div>Archived {formatDate(item.archived_at)}</div>
                {item.resolution_date && (
                  <div>Resolved {formatDate(item.resolution_date)}</div>
                )}
              </div>
            </button>
          ))}
        </div>
      )}

      {/* Pagination */}
      {hasMore && (
        <div className="flex justify-center">
          <button
            onClick={() => setPage(p => p + 1)}
            className="btn-secondary text-sm"
          >
            Load more
          </button>
        </div>
      )}
    </div>
  )
}
