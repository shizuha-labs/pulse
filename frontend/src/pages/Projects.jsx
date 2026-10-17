import { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Plus, FolderKanban, Users, Archive, MoreVertical, ChevronRight,
  UserPlus, Settings, X, Building2, ChevronDown, Trash2,
  CheckSquare, Square, LayoutGrid, List, Search, ArrowUpDown,
  ArrowUp, ArrowDown, Check, Minus,
} from 'lucide-react'
import { useOrganization } from '../contexts/OrganizationContext'
import { projectsApi, organizationsApi } from '../services/api'
import LoadingSpinner from '../components/LoadingSpinner'
import EmptyState from '../components/EmptyState'
import { cn } from '../utils/cn'

const VIEW_MODES = { GRID: 'grid', LIST: 'list' }

const SORT_OPTIONS = [
  { key: 'name-asc', label: 'Name A-Z', field: 'name', dir: 'asc' },
  { key: 'name-desc', label: 'Name Z-A', field: 'name', dir: 'desc' },
  { key: 'created-desc', label: 'Newest first', field: 'created_at', dir: 'desc' },
  { key: 'created-asc', label: 'Oldest first', field: 'created_at', dir: 'asc' },
  { key: 'items-desc', label: 'Most items', field: 'item_count', dir: 'desc' },
  { key: 'items-asc', label: 'Fewest items', field: 'item_count', dir: 'asc' },
  { key: 'members-desc', label: 'Most members', field: 'member_count', dir: 'desc' },
]

const PROJECT_TYPE_LABELS = {
  software: 'Software',
  business: 'Business',
  service_management: 'Service',
  operations: 'Operations',
}

export default function Projects() {
  const navigate = useNavigate()
  const {
    organizations,
    currentOrganization,
    switchOrganization,
    projects,
    createProject,
    createOrganization,
    archiveProject,
    deleteProjects,
    isLoading,
    refreshProjects,
  } = useOrganization()

  // View preferences (persisted)
  const [viewMode, setViewMode] = useState(() =>
    localStorage.getItem('pulse_projects_view') || VIEW_MODES.GRID
  )
  const [sortKey, setSortKey] = useState(() =>
    localStorage.getItem('pulse_projects_sort') || 'name-asc'
  )

  // Search
  const [searchQuery, setSearchQuery] = useState('')

  // Create project modal
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [newProject, setNewProject] = useState({
    name: '', key: '', description: '',
    project_type: 'software', access_level: 'limited',
  })
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState(null)

  // Organization creation
  const [showCreateOrgModal, setShowCreateOrgModal] = useState(false)
  const [newOrg, setNewOrg] = useState({ name: '', slug: '', description: '' })
  const [creatingOrg, setCreatingOrg] = useState(false)
  const [orgError, setOrgError] = useState(null)

  // Multi-select
  const [selectedIds, setSelectedIds] = useState([])
  const [isDeleting, setIsDeleting] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [lastClickedId, setLastClickedId] = useState(null)

  // Dropdown menus
  const [openMenuId, setOpenMenuId] = useState(null)
  const [showSortMenu, setShowSortMenu] = useState(false)
  const menuRef = useRef(null)
  const sortRef = useRef(null)

  // Invite modal
  const [showInviteModal, setShowInviteModal] = useState(false)
  const [inviteProject, setInviteProject] = useState(null)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState('member')
  const [inviting, setInviting] = useState(false)
  const [inviteError, setInviteError] = useState(null)
  const [inviteSuccess, setInviteSuccess] = useState(false)
  const [addToOrgFirst, setAddToOrgFirst] = useState(false)
  const [addingToOrg, setAddingToOrg] = useState(false)

  // Persist view preferences
  useEffect(() => {
    localStorage.setItem('pulse_projects_view', viewMode)
  }, [viewMode])
  useEffect(() => {
    localStorage.setItem('pulse_projects_sort', sortKey)
  }, [sortKey])

  // Close dropdowns on outside click
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setOpenMenuId(null)
      if (sortRef.current && !sortRef.current.contains(e.target)) setShowSortMenu(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e) => {
      // Escape clears selection
      if (e.key === 'Escape') {
        setSelectedIds([])
        setSearchQuery('')
      }
      // Ctrl/Cmd+A selects all (when not in input)
      if ((e.ctrlKey || e.metaKey) && e.key === 'a' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
        e.preventDefault()
        setSelectedIds(sortedProjects.map(p => p.id))
      }
      // Delete key triggers delete when items selected
      if ((e.key === 'Delete' || e.key === 'Backspace') && selectedIds.length > 0 && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
        e.preventDefault()
        setShowDeleteConfirm(true)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [selectedIds])

  // Filter + sort projects
  const sortedProjects = useMemo(() => {
    let filtered = projects
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase()
      filtered = projects.filter(p =>
        p.name.toLowerCase().includes(q) ||
        p.key.toLowerCase().includes(q) ||
        (p.description || '').toLowerCase().includes(q)
      )
    }
    const sortOpt = SORT_OPTIONS.find(s => s.key === sortKey) || SORT_OPTIONS[0]
    return [...filtered].sort((a, b) => {
      let av = a[sortOpt.field] ?? ''
      let bv = b[sortOpt.field] ?? ''
      if (typeof av === 'string') av = av.toLowerCase()
      if (typeof bv === 'string') bv = bv.toLowerCase()
      if (av < bv) return sortOpt.dir === 'asc' ? -1 : 1
      if (av > bv) return sortOpt.dir === 'asc' ? 1 : -1
      return 0
    })
  }, [projects, searchQuery, sortKey])

  // Clear selection when projects change
  useEffect(() => {
    setSelectedIds(prev => prev.filter(id => projects.some(p => p.id === id)))
  }, [projects])

  const hasSelection = selectedIds.length > 0
  const allSelected = sortedProjects.length > 0 && selectedIds.length === sortedProjects.length
  const someSelected = hasSelection && !allSelected

  // Selection handlers
  const toggleSelect = useCallback((id, e) => {
    e.stopPropagation()

    // Shift+click for range selection
    if (e.shiftKey && lastClickedId !== null) {
      const ids = sortedProjects.map(p => p.id)
      const start = ids.indexOf(lastClickedId)
      const end = ids.indexOf(id)
      if (start !== -1 && end !== -1) {
        const range = ids.slice(Math.min(start, end), Math.max(start, end) + 1)
        setSelectedIds(prev => [...new Set([...prev, ...range])])
        return
      }
    }

    setLastClickedId(id)
    setSelectedIds(prev =>
      prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]
    )
  }, [lastClickedId, sortedProjects])

  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedIds([])
    } else {
      setSelectedIds(sortedProjects.map(p => p.id))
    }
  }

  const handleBulkDelete = async () => {
    setIsDeleting(true)
    try {
      await deleteProjects(selectedIds)
      setSelectedIds([])
      setShowDeleteConfirm(false)
    } catch (err) {
      console.error('Failed to delete projects:', err)
    } finally {
      setIsDeleting(false)
    }
  }

  const handleBulkArchive = async () => {
    try {
      for (const id of selectedIds) {
        await archiveProject(id)
      }
      setSelectedIds([])
    } catch (err) {
      console.error('Failed to archive projects:', err)
    }
  }

  const handleCreateProject = async (e) => {
    e.preventDefault()
    setError(null)
    if (!currentOrganization) {
      setError('Please create or select an organization first')
      return
    }
    setCreating(true)
    try {
      await createProject({ ...newProject, organization: currentOrganization.id })
      setShowCreateModal(false)
      setNewProject({ name: '', key: '', description: '', project_type: 'software', access_level: 'limited' })
    } catch (err) {
      setError(err.response?.data?.detail || err.response?.data?.key?.[0] || 'Failed to create project')
    } finally {
      setCreating(false)
    }
  }

  const handleKeyChange = (e) => {
    const value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 10)
    setNewProject({ ...newProject, key: value })
  }

  const handleOrgNameChange = (e) => {
    const name = e.target.value
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
    setNewOrg({ ...newOrg, name, slug })
  }

  const handleCreateOrg = async (e) => {
    e.preventDefault()
    setOrgError(null)
    setCreatingOrg(true)
    try {
      await createOrganization(newOrg)
      setShowCreateOrgModal(false)
      setNewOrg({ name: '', slug: '', description: '' })
    } catch (err) {
      const d = err.response?.data
      setOrgError(d?.slug?.[0] || d?.name?.[0] || d?.detail || d?.non_field_errors?.[0] || 'Failed to create organization')
    } finally {
      setCreatingOrg(false)
    }
  }

  const handleOpenInviteModal = (project) => {
    setInviteProject(project)
    setInviteEmail('')
    setInviteRole('member')
    setInviteError(null)
    setInviteSuccess(false)
    setAddToOrgFirst(false)
    setShowInviteModal(true)
    setOpenMenuId(null)
  }

  const handleAddToOrgAndProject = async () => {
    setInviteError(null)
    setAddingToOrg(true)
    try {
      await organizationsApi.addMember(currentOrganization.slug, {
        user_email: inviteEmail, role: 'member',
      })
      await projectsApi.addMember(inviteProject.id, {
        user_email: inviteEmail, role: inviteRole,
        can_browse: true, can_create: inviteRole !== 'viewer',
        can_edit: inviteRole !== 'viewer', can_delete: inviteRole === 'admin',
        can_manage_project: inviteRole === 'admin', can_manage_members: inviteRole === 'admin',
      })
      setInviteSuccess(true)
      setAddToOrgFirst(false)
      setInviteEmail('')
      if (refreshProjects) refreshProjects()
    } catch (err) {
      setInviteError(err.response?.data?.detail || err.response?.data?.user_email?.[0] || err.response?.data?.error || 'Failed to add member')
    } finally {
      setAddingToOrg(false)
    }
  }

  const handleInviteMember = async (e) => {
    e.preventDefault()
    setInviteError(null)
    setInviting(true)
    try {
      await projectsApi.addMember(inviteProject.id, {
        user_email: inviteEmail, role: inviteRole,
        can_browse: true, can_create: inviteRole !== 'viewer',
        can_edit: inviteRole !== 'viewer', can_delete: inviteRole === 'admin',
        can_manage_project: inviteRole === 'admin', can_manage_members: inviteRole === 'admin',
      })
      setInviteSuccess(true)
      setInviteEmail('')
      if (refreshProjects) refreshProjects()
    } catch (err) {
      const errorMsg = err.response?.data?.error || err.response?.data?.detail || ''
      if (errorMsg.toLowerCase().includes('organization member')) {
        setAddToOrgFirst(true)
        setInviteError(null)
      } else {
        setInviteError(err.response?.data?.detail || err.response?.data?.user_email?.[0] || err.response?.data?.error || 'Failed to invite member')
      }
    } finally {
      setInviting(false)
    }
  }

  const handleArchiveProject = async (project) => {
    setOpenMenuId(null)
    try {
      await archiveProject(project.id)
    } catch (err) {
      console.error('Failed to archive project:', err)
    }
  }

  // ── Loading ──────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  // ── No organizations ─────────────────────────────────
  if (organizations.length === 0) {
    return (
      <div className="space-y-6">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Projects</h1>
            <p className="text-gray-500 dark:text-gray-400">Manage your projects and team tasks</p>
          </div>
        </div>
        <EmptyState
          icon={Building2}
          title="No organization yet"
          description="Create an organization to start managing projects and inviting team members"
          action={
            <button onClick={() => setShowCreateOrgModal(true)} className="btn-primary flex items-center gap-2">
              <Plus size={18} /><span>Create Organization</span>
            </button>
          }
        />
        {showCreateOrgModal && <CreateOrgModal {...{ newOrg, handleOrgNameChange, setNewOrg, orgError, handleCreateOrg, creatingOrg, onClose: () => { setShowCreateOrgModal(false); setOrgError(null); setNewOrg({ name: '', slug: '', description: '' }) } }} />}
      </div>
    )
  }

  // ── Main render ──────────────────────────────────────
  const currentSort = SORT_OPTIONS.find(s => s.key === sortKey) || SORT_OPTIONS[0]

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Projects</h1>
            {currentOrganization && (
              <>
                <span className="text-gray-300 dark:text-gray-600">/</span>
                {organizations.length > 1 ? (
                  <OrgSwitcher organizations={organizations} currentOrganization={currentOrganization} switchOrganization={switchOrganization} />
                ) : (
                  <OrgBadge org={currentOrganization} />
                )}
              </>
            )}
          </div>
          <p className="text-gray-500 dark:text-gray-400">
            {sortedProjects.length} project{sortedProjects.length !== 1 ? 's' : ''}
            {!currentOrganization && ' across all organizations'}
            {searchQuery && ` matching "${searchQuery}"`}
          </p>
        </div>
        <div className="relative group/create">
          <button
            onClick={() => currentOrganization && setShowCreateModal(true)}
            disabled={!currentOrganization}
            className="btn-primary flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Plus size={18} /><span>New Project</span>
          </button>
          {!currentOrganization && (
            <div className="absolute right-0 top-full mt-1 hidden group-hover/create:block z-40">
              <div className="bg-gray-900 dark:bg-gray-700 text-white text-xs rounded-md px-3 py-1.5 whitespace-nowrap shadow-lg">
                Select an organization to create a project
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Toolbar: Search, Sort, View Toggle */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2">
        {/* Search */}
        <div className="relative flex-1 max-w-sm">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search projects..."
            className="input pl-9 py-1.5 text-sm w-full"
          />
          {searchQuery && (
            <button onClick={() => setSearchQuery('')} className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700">
              <X size={14} className="text-gray-400" />
            </button>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* Sort */}
          <div className="relative" ref={sortRef}>
            <button
              onClick={() => setShowSortMenu(!showSortMenu)}
              className="btn-ghost btn-sm flex items-center gap-1.5 text-sm text-gray-600 dark:text-gray-300"
            >
              <ArrowUpDown size={14} />
              <span className="hidden sm:inline">{currentSort.label}</span>
            </button>
            {showSortMenu && (
              <div className="dropdown-menu right-0 top-full mt-1 min-w-[180px] py-1">
                {SORT_OPTIONS.map(opt => (
                  <button
                    key={opt.key}
                    onClick={() => { setSortKey(opt.key); setShowSortMenu(false) }}
                    className={cn(
                      'dropdown-item text-sm',
                      sortKey === opt.key && 'bg-brand-50 dark:bg-brand-900/30 text-brand-700 dark:text-brand-300'
                    )}
                  >
                    {sortKey === opt.key && <Check size={14} />}
                    {sortKey !== opt.key && <span className="w-3.5" />}
                    {opt.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Divider */}
          <div className="w-px h-6 bg-gray-200 dark:bg-gray-700" />

          {/* View Toggle */}
          <div className="flex items-center bg-gray-100 dark:bg-gray-800 rounded-lg p-0.5">
            <button
              onClick={() => setViewMode(VIEW_MODES.GRID)}
              className={cn(
                'p-1.5 rounded-md transition-colors',
                viewMode === VIEW_MODES.GRID
                  ? 'bg-white dark:bg-gray-700 shadow-sm text-brand-600 dark:text-brand-400'
                  : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
              )}
              title="Grid view"
            >
              <LayoutGrid size={16} />
            </button>
            <button
              onClick={() => setViewMode(VIEW_MODES.LIST)}
              className={cn(
                'p-1.5 rounded-md transition-colors',
                viewMode === VIEW_MODES.LIST
                  ? 'bg-white dark:bg-gray-700 shadow-sm text-brand-600 dark:text-brand-400'
                  : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
              )}
              title="List view"
            >
              <List size={16} />
            </button>
          </div>
        </div>
      </div>

      {/* Selection Toolbar */}
      {hasSelection && (
        <div className="flex items-center gap-3 p-3 bg-brand-50 dark:bg-brand-900/30 border border-brand-200 dark:border-brand-800 rounded-lg animate-fade-in">
          <button onClick={toggleSelectAll} className="p-0.5 rounded hover:bg-brand-100 dark:hover:bg-brand-800">
            {allSelected ? (
              <CheckSquare size={18} className="text-brand-600 dark:text-brand-400" />
            ) : someSelected ? (
              <div className="relative">
                <Square size={18} className="text-brand-600 dark:text-brand-400" />
                <Minus size={10} className="absolute top-1 left-1 text-brand-600 dark:text-brand-400" />
              </div>
            ) : (
              <Square size={18} className="text-brand-600 dark:text-brand-400" />
            )}
          </button>
          <span className="text-sm font-medium text-brand-700 dark:text-brand-300">
            {selectedIds.length} of {sortedProjects.length} selected
          </span>
          <div className="flex-1" />
          <button
            onClick={handleBulkArchive}
            className="btn-ghost btn-sm flex items-center gap-1.5 text-sm"
          >
            <Archive size={14} /> Archive
          </button>
          <button
            onClick={() => setShowDeleteConfirm(true)}
            disabled={isDeleting}
            className="btn-danger btn-sm flex items-center gap-1.5 text-sm"
          >
            <Trash2 size={14} /> Delete
          </button>
          <button onClick={() => setSelectedIds([])} className="btn-ghost btn-sm text-sm">
            Cancel
          </button>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-sm w-full p-6">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-2">
              Delete {selectedIds.length} project{selectedIds.length > 1 ? 's' : ''}?
            </h3>
            <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
              This will permanently delete the selected projects and all their items. This cannot be undone.
            </p>
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowDeleteConfirm(false)} disabled={isDeleting} className="btn">Cancel</button>
              <button onClick={handleBulkDelete} disabled={isDeleting} className="btn-danger">
                {isDeleting ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Project List */}
      {sortedProjects.length === 0 ? (
        searchQuery ? (
          <EmptyState
            icon={Search}
            title="No matching projects"
            description={`No projects match "${searchQuery}"`}
            action={
              <button onClick={() => setSearchQuery('')} className="btn-secondary flex items-center gap-2">
                <X size={16} /><span>Clear search</span>
              </button>
            }
          />
        ) : (
          <EmptyState
            icon={FolderKanban}
            title="No projects yet"
            description="Create your first project to start organizing tasks"
            action={
              <button onClick={() => setShowCreateModal(true)} className="btn-primary flex items-center gap-2">
                <Plus size={18} /><span>Create Project</span>
              </button>
            }
          />
        )
      ) : viewMode === VIEW_MODES.LIST ? (
        /* ── List View ────────────────────────────────── */
        <div className="card p-0 overflow-hidden">
          {/* Table Header */}
          <div className="grid grid-cols-[auto_1fr_auto_auto_auto_auto_auto] gap-4 items-center px-4 py-2.5 bg-gray-50 dark:bg-gray-800/50 border-b border-gray-200 dark:border-gray-700 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
            <div className="w-5">
              <button onClick={toggleSelectAll} className="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700">
                {allSelected ? (
                  <CheckSquare size={16} className="text-brand-600 dark:text-brand-400" />
                ) : someSelected ? (
                  <div className="relative">
                    <Square size={16} className="text-gray-400" />
                    <Minus size={8} className="absolute top-1 left-1 text-brand-600 dark:text-brand-400" />
                  </div>
                ) : (
                  <Square size={16} className="text-gray-400" />
                )}
              </button>
            </div>
            <div>Project</div>
            <div className="hidden md:block w-20 text-center">Type</div>
            <div className="hidden sm:block w-16 text-center">Access</div>
            <div className="w-16 text-center">Members</div>
            <div className="w-16 text-center">Items</div>
            <div className="w-8" />
          </div>

          {/* Table Body */}
          {sortedProjects.map((project) => {
            const isSelected = selectedIds.includes(project.id)
            return (
              <div
                key={project.id}
                className={cn(
                  'grid grid-cols-[auto_1fr_auto_auto_auto_auto_auto] gap-4 items-center px-4 py-3',
                  'border-b border-gray-100 dark:border-gray-800 last:border-b-0',
                  'hover:bg-gray-50 dark:hover:bg-gray-800/50 cursor-pointer transition-colors group',
                  isSelected && 'bg-brand-50/50 dark:bg-brand-900/20'
                )}
                onClick={() => navigate(`/projects/${project.id}`)}
              >
                {/* Checkbox */}
                <div className="w-5">
                  <button
                    onClick={(e) => toggleSelect(project.id, e)}
                    className="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700"
                  >
                    {isSelected ? (
                      <CheckSquare size={16} className="text-brand-600 dark:text-brand-400" />
                    ) : (
                      <Square size={16} className={cn(
                        'transition-colors',
                        hasSelection ? 'text-gray-400' : 'text-gray-300 dark:text-gray-600 group-hover:text-gray-400'
                      )} />
                    )}
                  </button>
                </div>

                {/* Project info */}
                <div className="flex items-center gap-3 min-w-0">
                  <div
                    className="w-8 h-8 rounded-md flex items-center justify-center text-white font-bold text-xs flex-shrink-0"
                    style={{ backgroundColor: project.color || '#0052CC' }}
                  >
                    {project.key}
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-gray-900 dark:text-white truncate">
                        {project.name}
                      </span>
                      {!currentOrganization && project.organization_name && (
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400 flex-shrink-0">
                          {project.organization_name}
                        </span>
                      )}
                    </div>
                    {project.description && (
                      <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
                        {project.description}
                      </div>
                    )}
                  </div>
                </div>

                {/* Type */}
                <div className="hidden md:block w-20 text-center">
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {PROJECT_TYPE_LABELS[project.project_type] || project.project_type}
                  </span>
                </div>

                {/* Access */}
                <div className="hidden sm:block w-16 text-center">
                  <AccessBadge level={project.access_level} />
                </div>

                {/* Members */}
                <div className="w-16 text-center">
                  <span className="text-sm text-gray-600 dark:text-gray-300 flex items-center justify-center gap-1">
                    <Users size={13} className="text-gray-400" />
                    {project.member_count || 1}
                  </span>
                </div>

                {/* Items */}
                <div className="w-16 text-center">
                  <span className="text-sm text-gray-600 dark:text-gray-300">
                    {project.item_count || 0}
                  </span>
                </div>

                {/* Actions */}
                <div className="w-8">
                  <div className="relative" ref={openMenuId === project.id ? menuRef : null}>
                    <button
                      className="p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-700 opacity-0 group-hover:opacity-100 transition-opacity"
                      onClick={(e) => {
                        e.stopPropagation()
                        setOpenMenuId(openMenuId === project.id ? null : project.id)
                      }}
                    >
                      <MoreVertical size={14} />
                    </button>
                    {openMenuId === project.id && (
                      <ProjectMenu
                        project={project}
                        onInvite={() => handleOpenInviteModal(project)}
                        onSettings={() => { setOpenMenuId(null); navigate(`/projects/${project.id}/settings`) }}
                        onArchive={() => handleArchiveProject(project)}
                      />
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      ) : (
        /* ── Grid View ────────────────────────────────── */
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {sortedProjects.map((project) => {
            const isSelected = selectedIds.includes(project.id)
            return (
              <div
                key={project.id}
                className={cn(
                  'card hover:shadow-md transition-all cursor-pointer relative group',
                  isSelected && 'ring-2 ring-brand-500 dark:ring-brand-400'
                )}
                onClick={() => navigate(`/projects/${project.id}`)}
              >
                {/* Checkbox — always visible when any item is selected */}
                <div className={cn(
                  'absolute top-3 left-3 z-10 transition-opacity',
                  hasSelection ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
                )}>
                  <button
                    onClick={(e) => toggleSelect(project.id, e)}
                    className="p-0.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
                  >
                    {isSelected ? (
                      <CheckSquare size={18} className="text-brand-600 dark:text-brand-400" />
                    ) : (
                      <Square size={18} className="text-gray-400" />
                    )}
                  </button>
                </div>

                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div
                      className="w-10 h-10 rounded-lg flex items-center justify-center text-white font-bold text-sm flex-shrink-0"
                      style={{ backgroundColor: project.color || '#0052CC' }}
                    >
                      {project.key}
                    </div>
                    <div className="min-w-0">
                      <h3 className="font-semibold text-gray-900 dark:text-white truncate">
                        {project.name}
                      </h3>
                      <p className="text-sm text-gray-500 dark:text-gray-400">{project.key}</p>
                    </div>
                  </div>
                  <div className="relative" ref={openMenuId === project.id ? menuRef : null}>
                    <button
                      className="btn-icon flex-shrink-0"
                      onClick={(e) => {
                        e.stopPropagation()
                        setOpenMenuId(openMenuId === project.id ? null : project.id)
                      }}
                    >
                      <MoreVertical size={16} />
                    </button>
                    {openMenuId === project.id && (
                      <ProjectMenu
                        project={project}
                        onInvite={() => handleOpenInviteModal(project)}
                        onSettings={() => { setOpenMenuId(null); navigate(`/projects/${project.id}/settings`) }}
                        onArchive={() => handleArchiveProject(project)}
                      />
                    )}
                  </div>
                </div>

                {project.description && (
                  <p className="mt-3 text-sm text-gray-600 dark:text-gray-300 line-clamp-2">
                    {project.description}
                  </p>
                )}

                <div className="mt-4 flex items-center justify-between text-sm text-gray-500 dark:text-gray-400">
                  <div className="flex items-center gap-4">
                    <span className="flex items-center gap-1">
                      <Users size={14} />{project.member_count || 1}
                    </span>
                    <span>{project.item_count || 0} items</span>
                  </div>
                  <div className="flex items-center gap-2">
                    {!currentOrganization && project.organization_name && (
                      <span className="px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300 truncate max-w-[120px]">
                        {project.organization_name}
                      </span>
                    )}
                    <AccessBadge level={project.access_level} />
                  </div>
                </div>

                <div className="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
                  <span className="text-xs text-gray-400">
                    {new Date(project.created_at).toLocaleDateString()}
                  </span>
                  <ChevronRight size={16} className="text-gray-400" />
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* Create Project Modal */}
      {showCreateModal && (
        <CreateProjectModal
          newProject={newProject}
          setNewProject={setNewProject}
          handleKeyChange={handleKeyChange}
          error={error}
          handleCreateProject={handleCreateProject}
          creating={creating}
          onClose={() => setShowCreateModal(false)}
        />
      )}

      {/* Create Organization Modal */}
      {showCreateOrgModal && (
        <CreateOrgModal
          newOrg={newOrg}
          handleOrgNameChange={handleOrgNameChange}
          setNewOrg={setNewOrg}
          orgError={orgError}
          handleCreateOrg={handleCreateOrg}
          creatingOrg={creatingOrg}
          onClose={() => { setShowCreateOrgModal(false); setOrgError(null); setNewOrg({ name: '', slug: '', description: '' }) }}
        />
      )}

      {/* Invite Member Modal */}
      {showInviteModal && inviteProject && (
        <InviteMemberModal
          inviteProject={inviteProject}
          inviteEmail={inviteEmail}
          setInviteEmail={setInviteEmail}
          inviteRole={inviteRole}
          setInviteRole={setInviteRole}
          inviteError={inviteError}
          inviteSuccess={inviteSuccess}
          inviting={inviting}
          addToOrgFirst={addToOrgFirst}
          addingToOrg={addingToOrg}
          currentOrganization={currentOrganization}
          setAddToOrgFirst={setAddToOrgFirst}
          handleInviteMember={handleInviteMember}
          handleAddToOrgAndProject={handleAddToOrgAndProject}
          onClose={() => setShowInviteModal(false)}
          setInviteError={setInviteError}
        />
      )}
    </div>
  )
}


// ── Sub-components ──────────────────────────────────────

function AccessBadge({ level }) {
  return (
    <span className={cn(
      'px-2 py-0.5 rounded text-xs font-medium',
      level === 'open' && 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300',
      level === 'limited' && 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-300',
      level === 'private' && 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300'
    )}>
      {level}
    </span>
  )
}

function OrgBadge({ org }) {
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-gradient-to-r from-brand-50 to-indigo-50 dark:from-brand-900/30 dark:to-indigo-900/30 border border-brand-200 dark:border-brand-800">
      <div className="w-6 h-6 rounded-md bg-gradient-to-br from-brand-500 to-indigo-600 flex items-center justify-center">
        <Building2 size={14} className="text-white" />
      </div>
      <span className="text-base font-semibold text-brand-700 dark:text-brand-300">{org.name}</span>
    </div>
  )
}

function OrgSwitcher({ organizations, currentOrganization, switchOrganization }) {
  return (
    <div className="relative inline-flex items-center group">
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-gradient-to-r from-brand-50 to-indigo-50 dark:from-brand-900/30 dark:to-indigo-900/30 border border-brand-200 dark:border-brand-800 hover:border-brand-300 dark:hover:border-brand-700 transition-colors cursor-pointer">
        <div className="w-6 h-6 rounded-md bg-gradient-to-br from-brand-500 to-indigo-600 flex items-center justify-center">
          <Building2 size={14} className="text-white" />
        </div>
        <select
          value={currentOrganization?.slug || ''}
          onChange={(e) => {
            const org = organizations.find(o => o.slug === e.target.value)
            if (org) switchOrganization(org)
          }}
          className="text-base font-semibold text-brand-700 dark:text-brand-300 bg-transparent border-none cursor-pointer focus:outline-none focus:ring-0 pr-5 appearance-none"
        >
          {organizations.map((org) => (
            <option key={org.slug} value={org.slug} className="bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100">
              {org.name}
            </option>
          ))}
        </select>
        <ChevronDown size={14} className="absolute right-2.5 text-brand-500 dark:text-brand-400 pointer-events-none" />
      </div>
    </div>
  )
}

function ProjectMenu({ project, onInvite, onSettings, onArchive }) {
  return (
    <div className="dropdown-menu right-0 top-full mt-1 min-w-[160px]">
      <button className="dropdown-item" onClick={(e) => { e.stopPropagation(); onInvite() }}>
        <UserPlus size={16} /> Invite Member
      </button>
      <button className="dropdown-item" onClick={(e) => { e.stopPropagation(); onSettings() }}>
        <Settings size={16} /> Settings
      </button>
      <button className="dropdown-item-danger" onClick={(e) => { e.stopPropagation(); onArchive() }}>
        <Archive size={16} /> Archive
      </button>
    </div>
  )
}

function CreateProjectModal({ newProject, setNewProject, handleKeyChange, error, handleCreateProject, creating, onClose }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">Create New Project</h2>
        <form onSubmit={handleCreateProject} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Project Name</label>
            <input type="text" value={newProject.name} onChange={(e) => setNewProject({ ...newProject, name: e.target.value })} className="input w-full" placeholder="Backend Development" required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Project Key</label>
            <input type="text" value={newProject.key} onChange={handleKeyChange} className="input w-full uppercase" placeholder="BE" maxLength={10} required />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Used for item IDs (e.g., {newProject.key || 'BE'}-123)</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Description</label>
            <textarea value={newProject.description} onChange={(e) => setNewProject({ ...newProject, description: e.target.value })} className="input w-full" rows={3} placeholder="What is this project about?" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Project Type</label>
            <select value={newProject.project_type} onChange={(e) => setNewProject({ ...newProject, project_type: e.target.value })} className="input w-full">
              <option value="software">Software Development</option>
              <option value="business">Business Project</option>
              <option value="service_management">Service Management</option>
              <option value="operations">Operations</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Access Level</label>
            <select value={newProject.access_level} onChange={(e) => setNewProject({ ...newProject, access_level: e.target.value })} className="input w-full">
              <option value="open">Open - All organization members can access</option>
              <option value="limited">Limited - Only project members</option>
              <option value="private">Private - Invitation only</option>
            </select>
          </div>
          {error && <div className="p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg">{error}</div>}
          <div className="flex justify-end gap-3 pt-4">
            <button type="button" onClick={onClose} className="btn">Cancel</button>
            <button type="submit" disabled={creating} className="btn-primary">{creating ? 'Creating...' : 'Create Project'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

function CreateOrgModal({ newOrg, handleOrgNameChange, setNewOrg, orgError, handleCreateOrg, creatingOrg, onClose }) {
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl max-w-md w-full mx-4 p-6">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">Create Organization</h2>
        <form onSubmit={handleCreateOrg} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Organization Name</label>
            <input type="text" value={newOrg.name} onChange={handleOrgNameChange} className="input w-full" placeholder="My Company" required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">URL Slug</label>
            <input type="text" value={newOrg.slug} onChange={(e) => setNewOrg({ ...newOrg, slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '') })} className="input w-full" placeholder="my-company" required />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Used in URLs (e.g., pulse.shizuha.com/{newOrg.slug || 'my-company'})</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Description (optional)</label>
            <textarea value={newOrg.description} onChange={(e) => setNewOrg({ ...newOrg, description: e.target.value })} className="input w-full" rows={3} placeholder="What does your organization do?" />
          </div>
          {orgError && <div className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 p-3 rounded-lg">{orgError}</div>}
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={creatingOrg} className="btn-primary">{creatingOrg ? 'Creating...' : 'Create Organization'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

function InviteMemberModal({
  inviteProject, inviteEmail, setInviteEmail, inviteRole, setInviteRole,
  inviteError, inviteSuccess, inviting, addToOrgFirst, addingToOrg,
  currentOrganization, setAddToOrgFirst, handleInviteMember,
  handleAddToOrgAndProject, onClose, setInviteError,
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white">Invite to {inviteProject.name}</h2>
          <button onClick={onClose} className="btn-icon"><X size={20} /></button>
        </div>
        <form onSubmit={handleInviteMember} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Email Address</label>
            <input
              type="email" value={inviteEmail}
              onChange={(e) => { setInviteEmail(e.target.value); setAddToOrgFirst(false); setInviteError(null) }}
              className="input w-full" placeholder="colleague@example.com" required disabled={addToOrgFirst}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Project Role</label>
            <select value={inviteRole} onChange={(e) => setInviteRole(e.target.value)} className="input w-full" disabled={addToOrgFirst}>
              <option value="admin">Admin - Full project control</option>
              <option value="member">Member - Create and edit items</option>
              <option value="viewer">Viewer - Read-only access</option>
            </select>
          </div>
          {inviteError && <div className="p-3 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-sm rounded-lg">{inviteError}</div>}
          {addToOrgFirst && (
            <div className="p-4 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg">
              <p className="text-sm text-amber-800 dark:text-amber-200 mb-3">
                <strong>{inviteEmail}</strong> is not a member of <strong>{currentOrganization?.name}</strong>.
              </p>
              <p className="text-sm text-amber-700 dark:text-amber-300 mb-3">
                Would you like to invite them to the organization and add them to this project?
              </p>
              <div className="flex gap-2">
                <button type="button" onClick={handleAddToOrgAndProject} disabled={addingToOrg} className="btn-primary text-sm">
                  {addingToOrg ? 'Adding...' : 'Yes, invite to organization & project'}
                </button>
                <button type="button" onClick={() => { setAddToOrgFirst(false); setInviteEmail('') }} className="btn text-sm">Cancel</button>
              </div>
            </div>
          )}
          {inviteSuccess && <div className="p-3 bg-green-50 dark:bg-green-900/30 text-green-600 dark:text-green-400 text-sm rounded-lg">Member added successfully!</div>}
          {!addToOrgFirst && (
            <div className="flex justify-end gap-3 pt-4">
              <button type="button" onClick={onClose} className="btn">{inviteSuccess ? 'Done' : 'Cancel'}</button>
              {!inviteSuccess && (
                <button type="submit" disabled={inviting || !inviteEmail} className="btn-primary">
                  {inviting ? 'Adding...' : 'Add to Project'}
                </button>
              )}
            </div>
          )}
        </form>
      </div>
    </div>
  )
}
