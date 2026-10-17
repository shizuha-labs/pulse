import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Zap, Check, X, RefreshCw } from 'lucide-react'
import { itemsApi } from '../services/api'
import ItemCard from '../components/ItemCard'
import EmptyState from '../components/EmptyState'
import { PageLoader } from '../components/LoadingSpinner'
import { cn } from '../utils/cn'

const STATUS_FILTERS = [
  { value: 'pending', label: 'Pending' },
  { value: '', label: 'All' },
  { value: 'approved', label: 'Approved' },
  { value: 'executing', label: 'Executing' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'rejected', label: 'Rejected' },
]

export default function Actions() {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState('pending')
  const [rejectModal, setRejectModal] = useState(null)
  const [rejectReason, setRejectReason] = useState('')

  // Build query params
  const queryParams = { mode: 'action' }
  if (statusFilter) queryParams.status = statusFilter

  // Fetch actions
  const { data, isLoading } = useQuery({
    queryKey: ['actions', queryParams],
    queryFn: () => itemsApi.list(queryParams).then((res) => res.data),
  })

  // Approve mutation
  const approveMutation = useMutation({
    mutationFn: (item) => itemsApi.approve(item.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['actions'] })
      queryClient.invalidateQueries({ queryKey: ['pending'] })
      queryClient.invalidateQueries({ queryKey: ['statistics'] })
    },
  })

  // Reject mutation
  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }) => itemsApi.reject(id, { rejection_reason: reason }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['actions'] })
      queryClient.invalidateQueries({ queryKey: ['pending'] })
      queryClient.invalidateQueries({ queryKey: ['statistics'] })
      setRejectModal(null)
      setRejectReason('')
    },
  })

  const handleReject = (e) => {
    e.preventDefault()
    if (!rejectModal) return
    rejectMutation.mutate({ id: rejectModal.id, reason: rejectReason })
  }

  if (isLoading) {
    return <PageLoader />
  }

  const actions = data?.results || data || []
  const pendingCount = actions.filter((a) => a.status === 'pending').length

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Actions</h1>
          <p className="text-gray-500 dark:text-gray-400">
            System-generated actions requiring approval
          </p>
        </div>
        {pendingCount > 0 && (
          <span className="badge badge-warning text-sm">
            {pendingCount} pending
          </span>
        )}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((filter) => (
          <button
            key={filter.value}
            onClick={() => setStatusFilter(filter.value)}
            className={cn(
              'px-3 py-1.5 rounded-lg text-sm font-medium transition-colors',
              statusFilter === filter.value
                ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
            )}
          >
            {filter.label}
          </button>
        ))}
      </div>

      {/* Action List */}
      {actions.length > 0 ? (
        <div className="space-y-3">
          {actions.map((action) => (
            <ItemCard
              key={action.id}
              item={action}
              onApprove={(item) => approveMutation.mutate(item)}
              onReject={(item) => setRejectModal(item)}
              navigateOnClick
            />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={Zap}
          title="No actions found"
          description={
            statusFilter === 'pending'
              ? 'No pending actions requiring approval'
              : 'No actions match the selected filter'
          }
        />
      )}

      {/* Reject Modal */}
      {rejectModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => setRejectModal(null)}
          />
          <div className="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-md p-6">
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">Reject Action</h2>
            <p className="text-gray-500 dark:text-gray-400 mb-4">
              Are you sure you want to reject "{rejectModal.title}"?
            </p>
            <form onSubmit={handleReject} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Reason (optional)
                </label>
                <textarea
                  value={rejectReason}
                  onChange={(e) => setRejectReason(e.target.value)}
                  className="input min-h-[80px]"
                  placeholder="Why are you rejecting this action?"
                />
              </div>

              <div className="flex gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setRejectModal(null)}
                  className="btn-secondary flex-1"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={rejectMutation.isPending}
                  className="btn-danger flex-1"
                >
                  {rejectMutation.isPending ? 'Rejecting...' : 'Reject'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
