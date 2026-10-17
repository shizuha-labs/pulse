import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search, X, UserPlus, Check, Loader2, Users } from 'lucide-react'
import { projectsApi, itemsApi } from '../services/api'
import { cn } from '../utils/cn'
import {
  buildTeamScope,
  formatScopedTeamName,
  getTeamRoutingKey,
  normalizeTeamOption,
} from '../utils/teamLabels'

/**
 * Debounce hook for search input
 */
function useDebounce(value, delay) {
  const [debouncedValue, setDebouncedValue] = useState(value)

  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedValue(value)
    }, delay)

    return () => {
      clearTimeout(handler)
    }
  }, [value, delay])

  return debouncedValue
}

/**
 * Reusable user search input component for assignee selection.
 *
 * - On focus (empty query): shows project members as quick suggestions
 * - On typing 2+ chars: searches all organization members
 */
export default function UserSearchInput({
  projectId,
  onSelect,
  currentValue = null, // { id, email, name }
  placeholder = 'Search for a user...',
  disabled = false,
  showClearButton = true,
  showTeams = true,
  organizationId = null,
  organizationSlug = '',
  organizationName = '',
  className = '',
}) {
  const [query, setQuery] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const [highlightedIndex, setHighlightedIndex] = useState(0)
  const inputRef = useRef(null)
  const dropdownRef = useRef(null)

  const debouncedQuery = useDebounce(query, 300)

  // Eagerly fetch project members (shown as default suggestions when project exists)
  const { data: membersData } = useQuery({
    queryKey: ['project-members', projectId],
    queryFn: async () => {
      const res = await projectsApi.members(projectId)
      return res.data.members || []
    },
    enabled: !!projectId,
    staleTime: 60000,
  })

  // Fetch assignable users when no project (org-wide list)
  const { data: assignableData } = useQuery({
    queryKey: ['assignableUsers'],
    queryFn: async () => {
      const res = await itemsApi.assignableUsers()
      return res.data.users || []
    },
    enabled: !projectId,
    staleTime: 60000,
  })

  const teamScope = useMemo(
    () => buildTeamScope({ organizationId, organizationSlug, projectId }),
    [organizationId, organizationSlug, projectId]
  )
  const canLoadTeams = showTeams && Boolean(projectId) && teamScope.isScoped

  // Team assignment is project-org scoped. The scope is part of both the
  // request and cache key so same-named teams from another org cannot bleed
  // into this picker when the user moves between tasks.
  const { data: teamsData, isLoading: isLoadingTeams } = useQuery({
    queryKey: ['teams', teamScope.cacheKey],
    queryFn: async () => {
      const res = await itemsApi.teams(teamScope.params)
      return res.data.teams || []
    },
    staleTime: 300000, // 5 min cache
    enabled: canLoadTeams,
  })

  const projectMembers = membersData || []
  const assignableUsers = assignableData || []
  const teams = useMemo(
    () => (teamsData || [])
      .map(team => normalizeTeamOption(team, {
        organization_id: organizationId,
        organization_slug: organizationSlug,
        organization_name: organizationName,
      }))
      .filter(team => team.team_key),
    [teamsData, organizationId, organizationSlug, organizationName]
  )

  // Fetch org members when user types a search query
  const { data: searchData, isLoading, isFetching } = useQuery({
    queryKey: ['org-members-search', projectId, debouncedQuery],
    queryFn: async () => {
      if (projectId) {
        const response = await projectsApi.searchOrgMembers(projectId, debouncedQuery)
        return response.data
      }
      // For non-project items, search org members via backend
      const response = await itemsApi.searchOrgMembers({ search: debouncedQuery })
      return response.data
    },
    enabled: debouncedQuery.length >= 2,
    staleTime: 30000,
  })

  const searchResults = searchData?.members || []

  // Decide what to show: search results (when typing) or members (on focus)
  const isSearching = debouncedQuery.length >= 2
  const displayItems = useMemo(() => {
    if (isSearching) return searchResults

    if (projectId) {
      // Default suggestions: project members
      return projectMembers.map(m => ({
        user_id: m.user_id,
        user_email: m.user_email,
        user_name: m.user_name || m.user_email?.split('@')[0],
        role: m.role,
        is_project_member: true,
      }))
    }

    // No project: show all assignable users
    return assignableUsers.map(u => ({
      user_id: u.user_id,
      user_email: u.email,
      user_name: u.user_name || u.email?.split('@')[0],
    }))
  }, [isSearching, searchResults, projectMembers, assignableUsers, projectId])

  // Reset highlight when results change
  useEffect(() => {
    setHighlightedIndex(0)
  }, [displayItems])

  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(event) {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(event.target) &&
        inputRef.current &&
        !inputRef.current.contains(event.target)
      ) {
        setIsOpen(false)
      }
    }

    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const handleSelect = useCallback((member) => {
    onSelect(member)
    setQuery('')
    setIsOpen(false)
    inputRef.current?.blur()
  }, [onSelect])

  const handleClear = useCallback(() => {
    onSelect(null)
    setQuery('')
  }, [onSelect])

  const handleKeyDown = useCallback((e) => {
    if (!isOpen || displayItems.length === 0) return

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        setHighlightedIndex((prev) => (prev + 1) % displayItems.length)
        break
      case 'ArrowUp':
        e.preventDefault()
        setHighlightedIndex((prev) => (prev - 1 + displayItems.length) % displayItems.length)
        break
      case 'Enter':
        e.preventDefault()
        if (displayItems[highlightedIndex]) {
          handleSelect(displayItems[highlightedIndex])
        }
        break
      case 'Escape':
        e.preventDefault()
        setIsOpen(false)
        inputRef.current?.blur()
        break
    }
  }, [isOpen, displayItems, highlightedIndex, handleSelect])

  // Filter teams by search query
  const filteredTeams = useMemo(() => {
    if (!showTeams || teams.length === 0) return []
    if (!isSearching) return teams
    const q = debouncedQuery.toLowerCase()
    return teams.filter(t =>
      formatScopedTeamName(t).toLowerCase().includes(q) ||
      t.team_key?.toLowerCase().includes(q) ||
      t.lead_email?.toLowerCase().includes(q)
    )
  }, [showTeams, teams, isSearching, debouncedQuery])

  const showDropdown = isOpen && (
    displayItems.length > 0 || filteredTeams.length > 0 || isSearching || isLoadingTeams
  )

  return (
    <div className={cn('relative', className)}>
      {/* Search Input */}
      <div className="relative">
        <Search
          size={16}
          className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
        />
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            setIsOpen(true)
          }}
          onFocus={() => setIsOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={currentValue?.email ? currentValue.email : placeholder}
          disabled={disabled}
          className={cn(
            'input w-full pl-9 pr-8',
            currentValue?.email && !query && 'text-gray-500 dark:text-gray-400'
          )}
        />
        {/* Loading indicator */}
        {(isLoading || isFetching) && query.length >= 2 && (
          <Loader2
            size={16}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 animate-spin"
          />
        )}
        {/* Clear button */}
        {showClearButton && currentValue?.email && !query && (
          <button
            type="button"
            onClick={handleClear}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded"
            title="Clear assignee"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {/* Current value display (when not searching) */}
      {currentValue?.email && !query && !isOpen && (
        <div className="mt-2 flex items-center gap-2 p-2 bg-gray-50 dark:bg-gray-800 rounded-lg">
          <div className="w-6 h-6 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center">
            <span className="text-xs text-brand-600 dark:text-brand-400 font-medium">
              {(currentValue.name || currentValue.email)[0].toUpperCase()}
            </span>
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-gray-900 dark:text-white truncate">
              {currentValue.name || currentValue.email.split('@')[0]}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
              {currentValue.email}
            </p>
          </div>
        </div>
      )}

      {/* Dropdown */}
      {showDropdown && (
        <div
          ref={dropdownRef}
          className="absolute z-50 mt-1 w-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-72 overflow-y-auto"
        >
          {/* Teams section */}
          {showTeams && isLoadingTeams && (
            <div className="flex items-center gap-2 px-3 py-2 text-xs text-gray-500 dark:text-gray-400 border-b border-gray-100 dark:border-gray-700">
              <Loader2 size={12} className="animate-spin" />
              Loading teams for this organization…
            </div>
          )}
          {showTeams && filteredTeams.length > 0 && (
            <>
              <div className="flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider border-b border-gray-100 dark:border-gray-700">
                <Users size={12} />
                Assign to team
              </div>
              {filteredTeams.map((team) => (
                <button
                  key={`team-${team.canonical_key || `${team.organization_id || team.organization_slug}:${team.team_key}`}`}
                  onClick={() => handleSelect({
                    user_id: null,
                    user_email: team.lead_email,
                    user_name: team.lead_email?.split('@')[0],
                    team_name: getTeamRoutingKey(team),
                    team_key: getTeamRoutingKey(team),
                    team_display_name: team.display_name,
                    organization_id: team.organization_id,
                    organization_slug: team.organization_slug,
                    organization_name: team.organization_name,
                    is_team: true,
                  })}
                  className="w-full flex items-center gap-3 p-3 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors text-left"
                >
                  <div className="w-8 h-8 rounded-full bg-violet-100 dark:bg-violet-900 flex items-center justify-center flex-shrink-0">
                    <Users size={14} className="text-violet-600 dark:text-violet-400" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-gray-900 dark:text-white text-sm">
                      {formatScopedTeamName(team)}
                    </p>
                    <p className="text-xs text-gray-500 dark:text-gray-400">Lead: {team.lead_email || 'None'}</p>
                  </div>
                </button>
              ))}
            </>
          )}

          {/* Users section header */}
          {displayItems.length > 0 && (
            <div className="flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider border-b border-gray-100 dark:border-gray-700">
              <Users size={12} />
              {isSearching ? 'Search results' : projectId ? 'Project members' : 'Organization members'}
            </div>
          )}

          {displayItems.length === 0 && filteredTeams.length === 0 ? (
            <div className="p-3 text-sm text-gray-500 dark:text-gray-400 text-center">
              {isLoading ? 'Searching...' : 'No users found'}
            </div>
          ) : displayItems.length === 0 ? null : (
            displayItems.map((member, index) => (
              <button
                key={member.user_id || member.user_email}
                onClick={() => handleSelect(member)}
                className={cn(
                  'w-full flex items-center gap-3 p-3 transition-colors text-left',
                  index === highlightedIndex
                    ? 'bg-brand-50 dark:bg-brand-900/30'
                    : 'hover:bg-gray-100 dark:hover:bg-gray-700',
                  currentValue?.email === member.user_email && 'bg-green-50 dark:bg-green-900/20'
                )}
              >
                {/* Avatar */}
                <div className="w-8 h-8 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center flex-shrink-0">
                  <span className="text-sm text-brand-600 dark:text-brand-400 font-medium">
                    {(member.user_name || member.user_email || '?')[0].toUpperCase()}
                  </span>
                </div>

                {/* User info */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="font-medium text-gray-900 dark:text-white text-sm truncate">
                      {member.user_name || member.user_email?.split('@')[0] || 'Unknown'}
                    </p>
                    {/* Project member indicator (only in search mode) */}
                    {isSearching && (
                      member.is_project_member ? (
                        <span className="text-xs text-green-600 dark:text-green-400 flex items-center gap-0.5">
                          <Check size={12} />
                          Member
                        </span>
                      ) : (
                        <span className="text-xs text-blue-600 dark:text-blue-400 flex items-center gap-0.5">
                          <UserPlus size={12} />
                          Add to project
                        </span>
                      )
                    )}
                  </div>
                  <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                    {member.user_email}
                  </p>
                </div>

                {/* Current selection indicator */}
                {currentValue?.email === member.user_email && (
                  <Check size={16} className="text-green-500 flex-shrink-0" />
                )}
              </button>
            ))
          )}

          {/* Hint to search org-wide */}
          {!isSearching && projectId && (
            <div className="px-3 py-2 text-xs text-gray-400 dark:text-gray-500 border-t border-gray-100 dark:border-gray-700">
              Type to search all organization members
            </div>
          )}
        </div>
      )}

      {/* Help text when typing but not enough chars */}
      {isOpen && query.length === 1 && (
        <div className="absolute z-50 mt-1 w-full p-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg text-sm text-gray-500 dark:text-gray-400">
          Type one more character to search organization
        </div>
      )}
    </div>
  )
}
