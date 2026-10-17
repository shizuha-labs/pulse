import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, X, Layers } from 'lucide-react'
import { componentsApi } from '../services/api'
import { cn } from '../utils/cn'

export default function ComponentSelector({
  projectId,
  value,
  onChange,
  disabled = false,
  className = '',
}) {
  const [isOpen, setIsOpen] = useState(false)

  // Fetch components for the project
  const { data: components = [], isLoading } = useQuery({
    queryKey: ['components', projectId],
    queryFn: async () => {
      if (!projectId) return []
      const res = await componentsApi.list({ project: projectId })
      // Handle both paginated response and direct array
      const data = res.data
      return Array.isArray(data) ? data : (data?.results || data || [])
    },
    enabled: !!projectId,
  })

  const selectedComponent = components.find(c => c.id === value)

  const handleSelect = (componentId) => {
    onChange(componentId)
    setIsOpen(false)
  }

  const handleClear = (e) => {
    e.stopPropagation()
    onChange(null)
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
        <Layers size={16} className="text-gray-400 flex-shrink-0" />
        <span className={cn(
          'flex-1 truncate',
          selectedComponent ? 'text-gray-900 dark:text-gray-100' : 'text-gray-400'
        )}>
          {selectedComponent ? selectedComponent.name : 'Select component...'}
        </span>
        {selectedComponent && !disabled && (
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
          ) : components.length === 0 ? (
            <div className="p-3 text-sm text-gray-500 text-center">
              No components defined
            </div>
          ) : (
            <>
              {/* Clear option */}
              {value && (
                <button
                  onClick={() => handleSelect(null)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-100 dark:hover:bg-gray-700 text-red-600 dark:text-red-400"
                >
                  <X size={14} />
                  <span className="text-sm">Clear component</span>
                </button>
              )}
              {components.map((component) => (
                <button
                  key={component.id}
                  onClick={() => handleSelect(component.id)}
                  className={cn(
                    'w-full flex items-center gap-2 px-3 py-2 text-left transition-colors',
                    component.id === value
                      ? 'bg-brand-50 dark:bg-brand-900/20'
                      : 'hover:bg-gray-100 dark:hover:bg-gray-700'
                  )}
                >
                  <Layers size={14} className="text-gray-400 flex-shrink-0" />
                  <div className="flex-1 min-w-0">
                    <span className="text-sm text-gray-900 dark:text-gray-100 block truncate">
                      {component.name}
                    </span>
                    {component.description && (
                      <span className="text-xs text-gray-500 dark:text-gray-400 block truncate">
                        {component.description}
                      </span>
                    )}
                  </div>
                  {component.item_count > 0 && (
                    <span className="text-xs text-gray-400 flex-shrink-0">
                      {component.item_count}
                    </span>
                  )}
                </button>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  )
}
