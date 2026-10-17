import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Shield, CheckCircle, BellOff, TrendingUp, GitBranch } from 'lucide-react'
import { rulesApi } from '../services/api'
import EmptyState from '../components/EmptyState'
import { PageLoader } from '../components/LoadingSpinner'
import { cn } from '../utils/cn'
import { formatDate } from '../utils/format'

const RULE_TYPES = [
  { key: 'approval', label: 'Approval Rules', icon: CheckCircle, color: 'green' },
  { key: 'silence', label: 'Silence Rules', icon: BellOff, color: 'gray' },
  { key: 'escalation', label: 'Escalation Rules', icon: TrendingUp, color: 'orange' },
  { key: 'inhibition', label: 'Inhibition Rules', icon: GitBranch, color: 'purple' },
]

export default function Rules() {
  const [activeTab, setActiveTab] = useState('approval')

  // Fetch rules for active tab
  const { data, isLoading } = useQuery({
    queryKey: ['rules', activeTab],
    queryFn: () => rulesApi[activeTab].list().then((res) => res.data),
  })

  const rules = data?.results || data || []
  const activeType = RULE_TYPES.find((t) => t.key === activeTab)

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Rules</h1>
        <p className="text-gray-500 dark:text-gray-400">
          Manage approval, silencing, escalation, and inhibition rules
        </p>
      </div>

      {/* Tab Navigation */}
      <div className="flex flex-wrap gap-2 border-b border-gray-200 dark:border-gray-700 pb-2">
        {RULE_TYPES.map((type) => (
          <button
            key={type.key}
            onClick={() => setActiveTab(type.key)}
            className={cn(
              'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors',
              activeTab === type.key
                ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300'
                : 'text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'
            )}
          >
            <type.icon size={18} />
            <span className="hidden sm:inline">{type.label}</span>
          </button>
        ))}
      </div>

      {/* Content */}
      {isLoading ? (
        <PageLoader />
      ) : rules.length > 0 ? (
        <div className="space-y-3">
          {rules.map((rule) => (
            <RuleCard key={rule.id} rule={rule} type={activeTab} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={activeType?.icon || Shield}
          title={`No ${activeType?.label?.toLowerCase() || 'rules'}`}
          description="Create a rule to automate workflows"
        />
      )}
    </div>
  )
}

function RuleCard({ rule, type }) {
  const isActive = rule.is_active !== false

  return (
    <div className={cn('card p-4', !isActive && 'opacity-50')}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-medium text-gray-900 dark:text-gray-100">{rule.name}</h3>
            <span
              className={cn(
                'badge',
                isActive ? 'badge-success' : 'badge-gray'
              )}
            >
              {isActive ? 'Active' : 'Inactive'}
            </span>
          </div>

          {rule.description && (
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{rule.description}</p>
          )}

          {/* Rule-specific details */}
          <div className="flex flex-wrap items-center gap-2 mt-2 text-xs text-gray-500 dark:text-gray-400">
            {/* Approval rules */}
            {type === 'approval' && (
              <>
                {rule.auto_approve && (
                  <span className="badge badge-success">Auto-approve</span>
                )}
                {rule.auto_reject && (
                  <span className="badge badge-error">Auto-reject</span>
                )}
              </>
            )}

            {/* Silence rules */}
            {type === 'silence' && (
              <>
                {rule.starts_at && (
                  <span>Starts: {formatDate(rule.starts_at)}</span>
                )}
                {rule.ends_at && (
                  <span>Ends: {formatDate(rule.ends_at)}</span>
                )}
                {rule.match_count > 0 && (
                  <span className="badge badge-gray">
                    {rule.match_count} matches
                  </span>
                )}
              </>
            )}

            {/* Escalation rules */}
            {type === 'escalation' && rule.stages && (
              <span>{rule.stages.length} stages</span>
            )}

            {/* Item types */}
            {rule.item_types?.length > 0 && (
              <span>Types: {rule.item_types.join(', ')}</span>
            )}

            {/* Modes */}
            {rule.modes?.length > 0 && (
              <span>Modes: {rule.modes.join(', ')}</span>
            )}

            {/* Created */}
            <span>Created: {formatDate(rule.created_at)}</span>
          </div>
        </div>
      </div>
    </div>
  )
}
