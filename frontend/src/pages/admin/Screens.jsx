import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Monitor, Layout, Layers, Plus, Edit2, Trash2, Eye, PenSquare, ChevronDown, ChevronRight } from 'lucide-react'
import { screensApi, screenSchemesApi, issueTypeScreenSchemesApi } from '../../services/api'
import { cn } from '../../utils/cn'
import LoadingSpinner from '../../components/LoadingSpinner'

const TABS = [
  { key: 'screens', label: 'Screens', icon: Monitor },
  { key: 'screen-schemes', label: 'Screen Schemes', icon: Layout },
  { key: 'itss', label: 'Issue Type Screen Schemes', icon: Layers },
]

const screenTypeColors = {
  create: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  edit: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  view: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  transition: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
}

export default function Screens() {
  const [activeTab, setActiveTab] = useState('screens')

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Screens</h1>
        <p className="text-gray-500 dark:text-gray-400">Configure field layouts for create, edit, and view operations</p>
      </div>

      {/* Tab Navigation */}
      <div className="border-b border-gray-200 dark:border-gray-700">
        <nav className="flex space-x-6 overflow-x-auto">
          {TABS.map(tab => (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={cn(
                'flex items-center gap-2 px-1 py-3 text-sm font-medium border-b-2 whitespace-nowrap transition-colors',
                activeTab === tab.key
                  ? 'border-brand-500 text-brand-600 dark:text-brand-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
              )}
            >
              <tab.icon size={16} />
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'screens' && <ScreensTab />}
      {activeTab === 'screen-schemes' && <ScreenSchemesTab />}
      {activeTab === 'itss' && <IssueTypeScreenSchemesTab />}
    </div>
  )
}

// =============================================================================
// Tab 1: Screens
// =============================================================================
function ScreensTab() {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [expandedId, setExpandedId] = useState(null)
  const [formData, setFormData] = useState({
    name: '',
    slug: '',
    description: '',
    screen_type: 'create',
  })

  const { data: screens = [], isLoading } = useQuery({
    queryKey: ['screens'],
    queryFn: () => screensApi.list().then(r => r.data.results || r.data || []),
  })

  const createMutation = useMutation({
    mutationFn: (data) => screensApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['screens'] })
      resetForm()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => screensApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['screens'] })
      resetForm()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => screensApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['screens'] })
    },
  })

  const resetForm = () => {
    setShowForm(false)
    setEditingId(null)
    setFormData({ name: '', slug: '', description: '', screen_type: 'create' })
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    if (editingId) {
      updateMutation.mutate({ id: editingId, ...formData })
    } else {
      createMutation.mutate(formData)
    }
  }

  const startEdit = (screen) => {
    setEditingId(screen.id)
    setFormData({
      name: screen.name,
      slug: screen.slug || '',
      description: screen.description || '',
      screen_type: screen.screen_type || 'create',
    })
    setShowForm(true)
  }

  if (isLoading) {
    return <div className="card p-8 text-center"><LoadingSpinner /></div>
  }

  return (
    <div className="space-y-4">
      {/* Add button */}
      <div className="flex justify-end">
        <button
          onClick={() => {
            resetForm()
            setShowForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          Add Screen
        </button>
      </div>

      {/* Inline form */}
      {showForm && (
        <form onSubmit={handleSubmit} className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Name *
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="input w-full"
                placeholder="e.g., Default Create Screen"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Slug *
              </label>
              <input
                type="text"
                value={formData.slug}
                onChange={(e) => setFormData({ ...formData, slug: e.target.value })}
                className="input w-full"
                placeholder="e.g., default-create-screen"
                required
              />
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Screen Type
              </label>
              <select
                value={formData.screen_type}
                onChange={(e) => setFormData({ ...formData, screen_type: e.target.value })}
                className="input w-full"
              >
                <option value="create">Create</option>
                <option value="edit">Edit</option>
                <option value="view">View</option>
                <option value="transition">Transition</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Description
              </label>
              <input
                type="text"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                className="input w-full"
                placeholder="Brief description"
              />
            </div>
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={resetForm} className="btn">
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || updateMutation.isPending}
              className="btn-primary"
            >
              {editingId ? 'Update' : 'Create'} Screen
            </button>
          </div>
        </form>
      )}

      {/* Screen list */}
      {screens.length === 0 ? (
        <p className="text-gray-500 dark:text-gray-400 text-center py-8">
          No screens defined yet
        </p>
      ) : (
        <div className="divide-y divide-gray-200 dark:divide-gray-700">
          {screens.map((screen) => (
            <div key={screen.id} className="card p-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3 flex-1 min-w-0">
                  <button
                    onClick={() => setExpandedId(expandedId === screen.id ? null : screen.id)}
                    className="btn-icon shrink-0"
                  >
                    {expandedId === screen.id ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                  </button>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <p className="font-medium text-gray-900 dark:text-gray-100">
                        {screen.name}
                      </p>
                      <span className={cn(
                        'px-2 py-0.5 text-xs rounded font-medium',
                        screenTypeColors[screen.screen_type] || screenTypeColors.create
                      )}>
                        {screen.screen_type}
                      </span>
                    </div>
                    {screen.slug && (
                      <p className="text-xs text-gray-400 dark:text-gray-500 font-mono">
                        {screen.slug}
                      </p>
                    )}
                    {screen.description && (
                      <p className="text-sm text-gray-500 dark:text-gray-400 truncate">
                        {screen.description}
                      </p>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-sm text-gray-500 dark:text-gray-400">
                    {Array.isArray(screen.fields) ? screen.fields.length : 0} fields
                  </span>
                  <button onClick={() => startEdit(screen)} className="btn-icon" title="Edit">
                    <Edit2 size={16} />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm('Delete this screen?')) {
                        deleteMutation.mutate(screen.id)
                      }
                    }}
                    className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                    title="Delete"
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>

              {/* Expanded fields */}
              {expandedId === screen.id && (
                <div className="mt-3 ml-10 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                  <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">Fields</h4>
                  {Array.isArray(screen.fields) && screen.fields.length > 0 ? (
                    <div className="space-y-1">
                      {screen.fields.map((field, idx) => (
                        <div
                          key={idx}
                          className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 py-1"
                        >
                          <span className="w-6 text-right text-gray-400 text-xs">{idx + 1}.</span>
                          <span>{typeof field === 'string' ? field : field.name || JSON.stringify(field)}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-gray-400 dark:text-gray-500">No fields configured</p>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// =============================================================================
// Tab 2: Screen Schemes
// =============================================================================
function ScreenSchemesTab() {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [formData, setFormData] = useState({
    name: '',
    slug: '',
    description: '',
    create_screen: '',
    edit_screen: '',
    view_screen: '',
  })

  const { data: schemes = [], isLoading } = useQuery({
    queryKey: ['screen-schemes'],
    queryFn: () => screenSchemesApi.list().then(r => r.data.results || r.data || []),
  })

  const { data: screens = [] } = useQuery({
    queryKey: ['screens'],
    queryFn: () => screensApi.list().then(r => r.data.results || r.data || []),
  })

  const createMutation = useMutation({
    mutationFn: (data) => screenSchemesApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['screen-schemes'] })
      resetForm()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => screenSchemesApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['screen-schemes'] })
      resetForm()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => screenSchemesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['screen-schemes'] })
    },
  })

  const resetForm = () => {
    setShowForm(false)
    setEditingId(null)
    setFormData({ name: '', slug: '', description: '', create_screen: '', edit_screen: '', view_screen: '' })
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    const payload = {
      ...formData,
      create_screen: formData.create_screen || null,
      edit_screen: formData.edit_screen || null,
      view_screen: formData.view_screen || null,
    }
    if (editingId) {
      updateMutation.mutate({ id: editingId, ...payload })
    } else {
      createMutation.mutate(payload)
    }
  }

  const startEdit = (scheme) => {
    setEditingId(scheme.id)
    setFormData({
      name: scheme.name,
      slug: scheme.slug || '',
      description: scheme.description || '',
      create_screen: scheme.create_screen || '',
      edit_screen: scheme.edit_screen || '',
      view_screen: scheme.view_screen || '',
    })
    setShowForm(true)
  }

  const getScreenName = (screenId) => {
    if (!screenId) return 'None'
    const screen = screens.find(s => s.id === screenId)
    return screen ? screen.name : screenId
  }

  if (isLoading) {
    return <div className="card p-8 text-center"><LoadingSpinner /></div>
  }

  return (
    <div className="space-y-4">
      {/* Add button */}
      <div className="flex justify-end">
        <button
          onClick={() => {
            resetForm()
            setShowForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          Add Screen Scheme
        </button>
      </div>

      {/* Inline form */}
      {showForm && (
        <form onSubmit={handleSubmit} className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Name *
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="input w-full"
                placeholder="e.g., Default Screen Scheme"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Slug *
              </label>
              <input
                type="text"
                value={formData.slug}
                onChange={(e) => setFormData({ ...formData, slug: e.target.value })}
                className="input w-full"
                placeholder="e.g., default-screen-scheme"
                required
              />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Description
            </label>
            <input
              type="text"
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              className="input w-full"
              placeholder="Brief description"
            />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                <span className="flex items-center gap-1"><PenSquare size={14} /> Create Screen</span>
              </label>
              <select
                value={formData.create_screen}
                onChange={(e) => setFormData({ ...formData, create_screen: e.target.value })}
                className="input w-full"
              >
                <option value="">None</option>
                {screens.map(s => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                <span className="flex items-center gap-1"><Edit2 size={14} /> Edit Screen</span>
              </label>
              <select
                value={formData.edit_screen}
                onChange={(e) => setFormData({ ...formData, edit_screen: e.target.value })}
                className="input w-full"
              >
                <option value="">None</option>
                {screens.map(s => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                <span className="flex items-center gap-1"><Eye size={14} /> View Screen</span>
              </label>
              <select
                value={formData.view_screen}
                onChange={(e) => setFormData({ ...formData, view_screen: e.target.value })}
                className="input w-full"
              >
                <option value="">None</option>
                {screens.map(s => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={resetForm} className="btn">
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || updateMutation.isPending}
              className="btn-primary"
            >
              {editingId ? 'Update' : 'Create'} Screen Scheme
            </button>
          </div>
        </form>
      )}

      {/* Scheme list */}
      {schemes.length === 0 ? (
        <p className="text-gray-500 dark:text-gray-400 text-center py-8">
          No screen schemes defined yet
        </p>
      ) : (
        <div className="divide-y divide-gray-200 dark:divide-gray-700">
          {schemes.map((scheme) => (
            <div key={scheme.id} className="card p-4">
              <div className="flex items-center justify-between">
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-gray-900 dark:text-gray-100">
                    {scheme.name}
                  </p>
                  {scheme.slug && (
                    <p className="text-xs text-gray-400 dark:text-gray-500 font-mono">
                      {scheme.slug}
                    </p>
                  )}
                  {scheme.description && (
                    <p className="text-sm text-gray-500 dark:text-gray-400">
                      {scheme.description}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-3 mt-2 text-xs text-gray-500 dark:text-gray-400">
                    <span className="flex items-center gap-1">
                      <PenSquare size={12} className="text-green-500" />
                      Create: {getScreenName(scheme.create_screen)}
                    </span>
                    <span className="flex items-center gap-1">
                      <Edit2 size={12} className="text-blue-500" />
                      Edit: {getScreenName(scheme.edit_screen)}
                    </span>
                    <span className="flex items-center gap-1">
                      <Eye size={12} className="text-purple-500" />
                      View: {getScreenName(scheme.view_screen)}
                    </span>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button onClick={() => startEdit(scheme)} className="btn-icon" title="Edit">
                    <Edit2 size={16} />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm('Delete this screen scheme?')) {
                        deleteMutation.mutate(scheme.id)
                      }
                    }}
                    className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                    title="Delete"
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// =============================================================================
// Tab 3: Issue Type Screen Schemes
// =============================================================================
function IssueTypeScreenSchemesTab() {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [expandedId, setExpandedId] = useState(null)
  const [formData, setFormData] = useState({
    name: '',
    slug: '',
    description: '',
  })

  const { data: schemes = [], isLoading } = useQuery({
    queryKey: ['issue-type-screen-schemes'],
    queryFn: () => issueTypeScreenSchemesApi.list().then(r => r.data.results || r.data || []),
  })

  const createMutation = useMutation({
    mutationFn: (data) => issueTypeScreenSchemesApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issue-type-screen-schemes'] })
      resetForm()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }) => issueTypeScreenSchemesApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issue-type-screen-schemes'] })
      resetForm()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id) => issueTypeScreenSchemesApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['issue-type-screen-schemes'] })
    },
  })

  const resetForm = () => {
    setShowForm(false)
    setEditingId(null)
    setFormData({ name: '', slug: '', description: '' })
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    if (editingId) {
      updateMutation.mutate({ id: editingId, ...formData })
    } else {
      createMutation.mutate(formData)
    }
  }

  const startEdit = (scheme) => {
    setEditingId(scheme.id)
    setFormData({
      name: scheme.name,
      slug: scheme.slug || '',
      description: scheme.description || '',
    })
    setShowForm(true)
  }

  if (isLoading) {
    return <div className="card p-8 text-center"><LoadingSpinner /></div>
  }

  return (
    <div className="space-y-4">
      {/* Add button */}
      <div className="flex justify-end">
        <button
          onClick={() => {
            resetForm()
            setShowForm(true)
          }}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={18} />
          Add Issue Type Screen Scheme
        </button>
      </div>

      {/* Inline form */}
      {showForm && (
        <form onSubmit={handleSubmit} className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Name *
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="input w-full"
                placeholder="e.g., Default Issue Type Screen Scheme"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Slug *
              </label>
              <input
                type="text"
                value={formData.slug}
                onChange={(e) => setFormData({ ...formData, slug: e.target.value })}
                className="input w-full"
                placeholder="e.g., default-itss"
                required
              />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Description
            </label>
            <textarea
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              className="input w-full"
              rows={2}
              placeholder="Brief description"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button type="button" onClick={resetForm} className="btn">
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || updateMutation.isPending}
              className="btn-primary"
            >
              {editingId ? 'Update' : 'Create'} Scheme
            </button>
          </div>
        </form>
      )}

      {/* Scheme list */}
      {schemes.length === 0 ? (
        <p className="text-gray-500 dark:text-gray-400 text-center py-8">
          No issue type screen schemes defined yet
        </p>
      ) : (
        <div className="divide-y divide-gray-200 dark:divide-gray-700">
          {schemes.map((scheme) => {
            const mappings = Array.isArray(scheme.mappings) ? scheme.mappings : []
            return (
              <div key={scheme.id} className="card p-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3 flex-1 min-w-0">
                    <button
                      onClick={() => setExpandedId(expandedId === scheme.id ? null : scheme.id)}
                      className="btn-icon shrink-0"
                    >
                      {expandedId === scheme.id ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                    </button>
                    <div className="min-w-0">
                      <p className="font-medium text-gray-900 dark:text-gray-100">
                        {scheme.name}
                      </p>
                      {scheme.slug && (
                        <p className="text-xs text-gray-400 dark:text-gray-500 font-mono">
                          {scheme.slug}
                        </p>
                      )}
                      {scheme.description && (
                        <p className="text-sm text-gray-500 dark:text-gray-400 truncate">
                          {scheme.description}
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className="text-sm text-gray-500 dark:text-gray-400">
                      {mappings.length} mapping{mappings.length !== 1 ? 's' : ''}
                    </span>
                    <button onClick={() => startEdit(scheme)} className="btn-icon" title="Edit">
                      <Edit2 size={16} />
                    </button>
                    <button
                      onClick={() => {
                        if (confirm('Delete this issue type screen scheme?')) {
                          deleteMutation.mutate(scheme.id)
                        }
                      }}
                      className="btn-icon text-red-500 hover:bg-red-50 dark:hover:bg-red-900/30"
                      title="Delete"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </div>

                {/* Expanded mappings */}
                {expandedId === scheme.id && (
                  <div className="mt-3 ml-10 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
                    <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                      Issue Type Mappings
                    </h4>
                    {mappings.length > 0 ? (
                      <div className="space-y-2">
                        {mappings.map((mapping, idx) => (
                          <div
                            key={idx}
                            className="flex items-center justify-between text-sm py-1.5 px-2 rounded bg-white dark:bg-gray-700/50"
                          >
                            <span className="text-gray-900 dark:text-gray-100 font-medium">
                              {mapping.issue_type_name || mapping.issue_type || 'Unknown'}
                            </span>
                            <span className="text-gray-500 dark:text-gray-400">
                              {mapping.screen_scheme_name || mapping.screen_scheme || 'Default'}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="text-sm text-gray-400 dark:text-gray-500">No mappings configured</p>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
