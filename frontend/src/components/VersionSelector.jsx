import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, X, Tag, CheckCircle } from 'lucide-react'
import { versionsApi } from '../services/api'
import { cn } from '../utils/cn'

const STATUS_COLORS = {
  planning: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
  active: 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
  released: 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400',
  archived: 'bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-500',
}

export default function VersionSelector({
  projectId,
  value,
  onChange,
  disabled = false,
  multiple = false, // For affects_versions
  excludeArchived = true,
  label = 'Fix Version',
  className = '',
}) {
  const [isOpen, setIsOpen] = useState(false)

  // Fetch versions for the project
  const { data: versions = [], isLoading } = useQuery({
    queryKey: ['versions', projectId, excludeArchived],
    queryFn: async () => {
      if (!projectId) return []
      const res = await versionsApi.list({
        project: projectId,
        exclude_archived: excludeArchived,
      })
      // Handle both paginated response and direct array
      const data = res.data
      return Array.isArray(data) ? data : (data?.results || data || [])
    },
    enabled: !!projectId,
  })

  // For single select
  const selectedVersion = !multiple ? versions.find(v => v.id === value) : null

  // For multi select
  const selectedVersions = multiple
    ? versions.filter(v => (value || []).includes(v.id))
    : []

  const handleSelect = (versionId) => {
    if (multiple) {
      const currentValues = value || []
      if (currentValues.includes(versionId)) {
        onChange(currentValues.filter(id => id !== versionId))
      } else {
        onChange([...currentValues, versionId])
      }
    } else {
      onChange(versionId)
      setIsOpen(false)
    }
  }

  const handleClear = (e) => {
    e.stopPropagation()
    onChange(multiple ? [] : null)
  }

  if (!projectId) {
    return (
      <div className="text-sm text-gray-400 dark:text-gray-500">
        Select a project first
      </div>
    )
  }

  return (
    <div className={cn('relative', className)} data-dropdown>
      <label className="text-sm text-gray-500 dark:text-gray-400 block mb-1">
        {label}
      </label>
      <button
        type="button"
        onClick={() => !disabled && setIsOpen(!isOpen)}
        disabled={disabled}
        className={cn(
          'w-full flex items-center gap-2 px-3 py-2 rounded-lg border text-left transition-colors',
          'bg-white dark:bg-gray-900',
          'border-gray-200 dark:border-gray-700',
          'hover:border-gray-300 dark:hover:border-gray-600',
          disabled && 'opacity-50 cursor-not-allowed'
        )}
      >
        <Tag size={16} className="text-gray-400 flex-shrink-0" />
        <span className={cn(
          'flex-1 truncate',
          (selectedVersion || selectedVersions.length > 0)
            ? 'text-gray-900 dark:text-gray-100'
            : 'text-gray-400'
        )}>
          {multiple
            ? selectedVersions.length > 0
              ? selectedVersions.map(v => v.name).join(', ')
              : 'Select versions...'
            : selectedVersion
              ? selectedVersion.name
              : 'Select version...'
          }
        </span>
        {((multiple && selectedVersions.length > 0) || (!multiple && selectedVersion)) && !disabled && (
          <button
            onClick={handleClear}
            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded"
          >
            <X size={14} className="text-gray-400" />
          </button>
        )}
        <ChevronDown size={16} className="text-gray-400 flex-shrink-0" />
      </button>

      {isOpen && (
        <div className="absolute z-40 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-60 overflow-y-auto">
          {isLoading ? (
            <div className="p-3 text-sm text-gray-500 text-center">Loading...</div>
          ) : versions.length === 0 ? (
            <div className="p-3 text-sm text-gray-500 text-center">
              No versions defined
            </div>
          ) : (
            <>
              {/* Clear option */}
              {((multiple && selectedVersions.length > 0) || (!multiple && value)) && (
                <button
                  onClick={(e) => { handleClear(e); setIsOpen(false) }}
                  className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-100 dark:hover:bg-gray-700 text-red-600 dark:text-red-400"
                >
                  <X size={14} />
                  <span className="text-sm">Clear selection</span>
                </button>
              )}
              {versions.map((version) => {
                const isSelected = multiple
                  ? (value || []).includes(version.id)
                  : value === version.id

                return (
                  <button
                    key={version.id}
                    onClick={() => handleSelect(version.id)}
                    className={cn(
                      'w-full flex items-center gap-2 px-3 py-2 text-left transition-colors',
                      isSelected
                        ? 'bg-brand-50 dark:bg-brand-900/20'
                        : 'hover:bg-gray-100 dark:hover:bg-gray-700'
                    )}
                  >
                    {multiple && (
                      <div className={cn(
                        'w-4 h-4 rounded border flex items-center justify-center',
                        isSelected
                          ? 'bg-brand-500 border-brand-500'
                          : 'border-gray-300 dark:border-gray-600'
                      )}>
                        {isSelected && <CheckCircle size={12} className="text-white" />}
                      </div>
                    )}
                    <Tag size={14} className="text-gray-400 flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-gray-900 dark:text-gray-100 truncate">
                          {version.name}
                        </span>
                        <span className={cn(
                          'text-xs px-1.5 py-0.5 rounded',
                          STATUS_COLORS[version.status]
                        )}>
                          {version.status}
                        </span>
                      </div>
                      {version.release_date && (
                        <span className="text-xs text-gray-500 dark:text-gray-400">
                          {new Date(version.release_date).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                    {version.progress_percent !== undefined && (
                      <div className="flex items-center gap-1 flex-shrink-0">
                        <div className="w-12 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                          <div
                            className="h-full bg-green-500 rounded-full"
                            style={{ width: `${version.progress_percent}%` }}
                          />
                        </div>
                        <span className="text-xs text-gray-400">{version.progress_percent}%</span>
                      </div>
                    )}
                  </button>
                )
              })}
            </>
          )}
        </div>
      )}
    </div>
  )
}
