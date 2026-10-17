import axios from 'axios'
import { workflowPendingMessage } from '../utils/workflowOperation'

// API base URL - uses VITE_API_BASE for deployment flexibility
// When accessed via /pulse/ prefix, this should be /pulse/api
// When accessed directly (standalone), this should be /api
const API_BASE = import.meta.env.VITE_API_BASE || '/api'

// Admin API base URL - organizations are managed by shizuha-admin
// This resolves to /admin/api when accessed via nginx proxy
const ADMIN_API_BASE = import.meta.env.VITE_ADMIN_API_BASE || '/admin/api'

const api = axios.create({
  baseURL: API_BASE,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Admin API instance for organization management
const adminApi = axios.create({
  baseURL: ADMIN_API_BASE,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Request interceptor for debugging
api.interceptors.request.use(
  (config) => {
    console.debug(`[API] ${config.method?.toUpperCase()} ${config.url}`)
    return config
  },
  (error) => Promise.reject(error)
)

adminApi.interceptors.request.use(
  (config) => {
    console.debug(`[Admin API] ${config.method?.toUpperCase()} ${config.url}`)
    return config
  },
  (error) => Promise.reject(error)
)

// Response interceptor for error handling
// Only log 5xx server errors and unexpected errors (not 4xx client errors like 404)
api.interceptors.response.use(
  (response) => {
    const message = workflowPendingMessage(response)
    if (message) window.dispatchEvent(new CustomEvent('pulse:workflow-pending', { detail: message }))
    return response
  },
  (error) => {
    const status = error.response?.status
    // Only log server errors (5xx) or network errors, not client errors (4xx)
    if (!status || status >= 500) {
      console.error('[API Error]', error.response?.data || error.message)
    }
    return Promise.reject(error)
  }
)

adminApi.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    if (!status || status >= 500) {
      console.error('[Admin API Error]', error.response?.data || error.message)
    }
    return Promise.reject(error)
  }
)

// Synchronize auth headers between api instances
// When AuthContext sets Authorization header on api, sync to adminApi
const originalSetHeader = Object.getOwnPropertyDescriptor(api.defaults.headers, 'common')
if (originalSetHeader) {
  // Watch for changes to Authorization header and sync to adminApi
  const syncAuthHeader = () => {
    const authHeader = api.defaults.headers.common['Authorization']
    if (authHeader) {
      adminApi.defaults.headers.common['Authorization'] = authHeader
    }
  }

  // Initial sync
  syncAuthHeader()

  // Add interceptor to sync on each request
  api.interceptors.request.use(
    (config) => {
      syncAuthHeader()
      return config
    },
    (error) => Promise.reject(error)
  )

  // Sync admin API before each request
  adminApi.interceptors.request.use(
    (config) => {
      // Copy auth header from main api if not set
      if (!config.headers['Authorization'] && api.defaults.headers.common['Authorization']) {
        config.headers['Authorization'] = api.defaults.headers.common['Authorization']
      }
      return config
    },
    (error) => Promise.reject(error)
  )
}

// Export adminApi for direct use if needed
export { adminApi }

// Items API
export const itemsApi = {
  // List items with filters
  list: (params = {}, config = {}) => api.get('/items/', { ...config, params }),

  // Get single item
  get: (id) => api.get(`/items/${id}/`),

  // Get item by key (e.g., PROJ-123)
  getByKey: (itemKey) => api.get(`/items/by-key/${itemKey}/`),

  // Create item
  create: (data) => api.post('/items/', data),

  // Update item
  update: (id, data) => api.patch(`/items/${id}/`, data),

  // Delete item
  delete: (id) => api.delete(`/items/${id}/`),

  // Get filtered lists
  getTasks: (params = {}) => api.get('/items/tasks/', { params }),
  getActions: (params = {}) => api.get('/items/actions/', { params }),
  getAlerts: (params = {}) => api.get('/items/alerts/', { params }),
  getPending: (config = {}) => api.get('/items/pending/', config),
  getOverdue: (config = {}) => api.get('/items/overdue/', config),
  getDueSoon: (hours = 24) => api.get('/items/due_soon/', { params: { hours } }),
  getStatistics: (config = {}) => api.get('/items/statistics/', config),

  // Item actions
  complete: (id) => api.post(`/items/${id}/complete/`),
  reopen: (id) => api.post(`/items/${id}/reopen/`),
  approve: (id, data = {}) => api.post(`/items/${id}/approve/`, data),
  reject: (id, data) => api.post(`/items/${id}/reject/`, data),
  acknowledge: (id, data = {}) => api.post(`/items/${id}/acknowledge/`, data),
  resolve: (id, data = {}) => api.post(`/items/${id}/resolve/`, data),
  silence: (id, data) => api.post(`/items/${id}/silence/`, data),
  assign: (id, data) => api.post(`/items/${id}/assign/`, data),

  // Team visibility
  teamTasks: (params = {}) => api.get('/items/team_tasks/', { params }),
  agentOverview: () => api.get('/items/agent_overview/'),
  assignableUsers: (params = {}) => api.get('/items/assignable_users/', { params }),
  searchOrgMembers: (params = {}) => api.get('/items/search-org-members/', { params }),
  userWorkload: (params = {}) => api.get('/items/user_workload/', { params }),
  // PLS-274 analytics: per-team stage dwell + throughput (PLS-13 metrics)
  teamThroughput: (params = {}) => api.get('/items/team_throughput/', { params }),
  // PLS-362 analytics: per-item cycle/lead time for completed items
  cycleTimes: (params = {}) => api.get('/items/cycle_times/', { params }),
  // PLS-361 analytics: end-of-day status counts for the cumulative-flow diagram
  cumulativeFlow: (params = {}) => api.get('/items/cumulative_flow/', { params }),

  // Activity history
  getActivity: (id, params = {}) => api.get(`/items/${id}/activity/`, { params }),

  // Issue links
  getLinks: (id) => api.get(`/items/${id}/links/`),
  createLink: (id, data) => api.post(`/items/${id}/links/`, data),
  deleteLink: (id, linkId) => api.delete(`/items/${id}/links/${linkId}/`),
  // Mark as duplicate of another task — creates link + closes to terminal status
  markDuplicate: (id, data) => api.post(`/items/${id}/mark-duplicate/`, data),
  // Undo mark-as-duplicate — removes link and restores previous status
  unmarkDuplicate: (id) => api.post(`/items/${id}/unmark-duplicate/`),

  // === Export / Import ===
  // Returns a Blob of the exported bundle/CSV. Pass the same filters as list().
  exportBundle: (params = {}) => api.get('/items/export/bundle/', { params, responseType: 'blob' }),
  exportCsv: (params = {}) => api.get('/items/export/csv/', { params, responseType: 'blob' }),
  // Import — pass FormData with "file" field (+ optional target_project_id)
  importBundle: (formData) => api.post('/items/import/bundle/', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }),
  importCsv: (formData) => api.post('/items/import/csv/', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }),
  // Workflow transitions
  getAvailableTransitions: (id) => api.get(`/items/${id}/available-transitions/`),
  executeTransition: (id, data) => api.post(`/items/${id}/execute-transition/`, data),
  // Manually fire a recurring task's next cycle (only valid from Scheduled/Done)
  runNow: (id, payload) => api.post(`/items/${id}/run-now/`, payload),
  // Park a recurring task in Scheduled until the given time (inverse of runNow).
  // `payload` must be { next_fire_at: ISO } or { duration_seconds: N }.
  snooze: (id, payload) => api.post(`/items/${id}/snooze/`, payload),
  // Swap an item's workflow. Accepts { workflow: 'name-or-slug', status?: 'slug' }.
  // Server auto-maps current status into the target workflow if `status` is omitted.
  changeWorkflow: (id, payload) => api.post(`/items/${id}/change-workflow/`, payload),

  // Watchers
  getWatchers: (id) => api.get(`/items/${id}/watchers/`),
  watch: (id) => api.post(`/items/${id}/watchers/`),
  unwatch: (id) => api.delete(`/items/${id}/watchers/`),

  // Attachments (via item endpoint)
  getAttachments: (id) => api.get(`/items/${id}/attachments/`),

  // Teams (for assignment dropdown)
  teams: (params = {}) => api.get('/teams/', { params }),
  teamRosterMember: (email, projectId) => api.get('/admin/team-roster/members/', {
    params: { email, project_id: projectId },
  }),
}

// Tasks API (convenience wrapper)
export const tasksApi = {
  list: (params = {}) => api.get('/tasks/', { params }),
  get: (id) => api.get(`/tasks/${id}/`),
  create: (data, organizationId = null) => api.post('/tasks/', data, {
    headers: organizationId
      ? { 'X-Organization-ID': String(organizationId) }
      : undefined,
  }),
  update: (id, data) => api.patch(`/tasks/${id}/`, data),
  delete: (id) => api.delete(`/tasks/${id}/`),
  complete: (id) => api.post(`/tasks/${id}/complete/`),
  snooze: (id, data) => api.post(`/items/${id}/snooze/`, data),
  setReminder: (id, data) => api.post(`/items/${id}/set_reminder/`, data),

  // Bulk operations
  bulkComplete: (ids) => api.post('/tasks/bulk-complete/', { item_ids: ids }),
  bulkUpdate: (ids, data) => api.post('/tasks/bulk-update/', { item_ids: ids, ...data }),
  bulkDelete: (ids) => api.post('/tasks/bulk-delete/', { item_ids: ids }),

  // Team visibility
  teamTasks: (params = {}) => api.get('/items/team_tasks/', { params }),
  agentOverview: () => api.get('/items/agent_overview/'),
  // PLS-305: agent coordination console read model (roster + WIP workstreams
  // + blockers/PRs/next transitions over canonical Pulse state)
  agentActivity: (params = {}) => api.get('/items/agent-activity/', { params }),
  // PLAT-320/B1 org-chart 2.0 snapshot — nodes, teams, workload (admin/staff only)
  orgChartSnapshot: () => api.get('/admin/org-chart/snapshot/'),
  assignableUsers: (params = {}) => api.get('/items/assignable_users/', { params }),
  searchOrgMembers: (params = {}) => api.get('/items/search-org-members/', { params }),
  userWorkload: (params = {}) => api.get('/items/user_workload/', { params }),
}

// Alerts API
export const alertsApi = {
  submit: (data) => api.post('/alerts/', data),
  bulkAcknowledge: (data) => api.post('/alerts/bulk-acknowledge/', data),
  bulkResolve: (data) => api.post('/alerts/bulk-resolve/', data),
  bulkDelete: (ids) => api.post('/alerts/bulk-delete/', { item_ids: ids }),
}

// Actions API
export const actionsApi = {
  submit: (data) => api.post('/actions/submit/', data),
}

// Rules API
export const rulesApi = {
  // Approval rules
  approval: {
    list: () => api.get('/rules/approval/'),
    get: (id) => api.get(`/rules/approval/${id}/`),
    create: (data) => api.post('/rules/approval/', data),
    update: (id, data) => api.patch(`/rules/approval/${id}/`, data),
    delete: (id) => api.delete(`/rules/approval/${id}/`),
    test: (id, data) => api.post(`/rules/approval/${id}/test/`, data),
  },

  // Silence rules
  silence: {
    list: () => api.get('/rules/silence/'),
    get: (id) => api.get(`/rules/silence/${id}/`),
    create: (data) => api.post('/rules/silence/', data),
    update: (id, data) => api.patch(`/rules/silence/${id}/`, data),
    delete: (id) => api.delete(`/rules/silence/${id}/`),
    test: (id, data) => api.post(`/rules/silence/${id}/test/`, data),
  },

  // Escalation rules
  escalation: {
    list: () => api.get('/rules/escalation/'),
    get: (id) => api.get(`/rules/escalation/${id}/`),
    create: (data) => api.post('/rules/escalation/', data),
    update: (id, data) => api.patch(`/rules/escalation/${id}/`, data),
    delete: (id) => api.delete(`/rules/escalation/${id}/`),
  },

  // Inhibition rules
  inhibition: {
    list: () => api.get('/rules/inhibition/'),
    get: (id) => api.get(`/rules/inhibition/${id}/`),
    create: (data) => api.post('/rules/inhibition/', data),
    update: (id, data) => api.patch(`/rules/inhibition/${id}/`, data),
    delete: (id) => api.delete(`/rules/inhibition/${id}/`),
    test: (id, data) => api.post(`/rules/inhibition/${id}/test/`, data),
  },
}

// Subscriptions API
export const subscriptionsApi = {
  list: () => api.get('/subscriptions/'),
  get: (id) => api.get(`/subscriptions/${id}/`),
  create: (data) => api.post('/subscriptions/', data),
  update: (id, data) => api.patch(`/subscriptions/${id}/`, data),
  delete: (id) => api.delete(`/subscriptions/${id}/`),
}

// Fingerprint configs API
export const fingerprintConfigsApi = {
  list: () => api.get('/fingerprint-configs/'),
  get: (source) => api.get(`/fingerprint-configs/${source}/`),
  create: (data) => api.post('/fingerprint-configs/', data),
  update: (source, data) => api.patch(`/fingerprint-configs/${source}/`, data),
}

// Notification Channels API
export const notificationChannelsApi = {
  // List all channels (can filter by user_id, channel_type, is_active)
  list: (params = {}) => api.get('/notification-channels/', { params }),

  // Get channels for a specific user
  byUser: (userId) => api.get('/notification-channels/by_user/', { params: { user_id: userId } }),

  // Get single channel
  get: (id) => api.get(`/notification-channels/${id}/`),

  // Create channel
  create: (data) => api.post('/notification-channels/', data),

  // Update channel
  update: (id, data) => api.patch(`/notification-channels/${id}/`, data),

  // Delete channel
  delete: (id) => api.delete(`/notification-channels/${id}/`),

  // Test channel (sends a test notification)
  test: (id, message = null) =>
    api.post(`/notification-channels/${id}/test/`, message ? { message } : {}),

  // Toggle channel active status
  toggle: (id) => api.post(`/notification-channels/${id}/toggle/`),
}

// Organizations API - managed by shizuha-admin
// All organization CRUD operations go through /admin/api/organizations/
export const organizationsApi = {
  // List organizations user belongs to
  list: (params = {}) => adminApi.get('/organizations/', { params }),

  // Get single organization by slug
  get: (slug) => adminApi.get(`/organizations/${slug}/`),

  // Create organization
  create: (data) => adminApi.post('/organizations/', data),

  // Update organization
  update: (slug, data) => adminApi.patch(`/organizations/${slug}/`, data),

  // Delete organization
  delete: (slug) => adminApi.delete(`/organizations/${slug}/`),

  // Get organization members
  members: (slug) => adminApi.get(`/organizations/${slug}/members/`),

  // Invite member to organization
  addMember: (slug, data) => adminApi.post(`/organizations/${slug}/members/`, data),

  // Get organization projects
  projects: (slug) => adminApi.get(`/organizations/${slug}/projects/`),
}

// Projects API
export const projectsApi = {
  // List accessible projects (can filter by organization, organization_slug, include_archived)
  list: (params = {}) => api.get('/projects/', { params }),

  // Load the complete accessible project collection for a scope. Project
  // pickers must not silently inherit DRF's first-page default: newer projects
  // would disappear and an exact-org picker could incorrectly look empty.
  listAll: async (params = {}) => {
    const filters = { ...params }
    delete filters.page
    delete filters.page_size

    const projects = []
    let page = 1
    while (true) {
      const response = await api.get('/projects/', {
        params: { ...filters, page, page_size: 200 },
      })
      const data = response.data
      if (Array.isArray(data)) {
        projects.push(...data)
        break
      }
      projects.push(...(data.results || []))
      if (!data.next) break
      page += 1
    }
    return projects
  },

  // Get single project
  get: (id) => api.get(`/projects/${id}/`),

  // Create project
  create: (data) => api.post('/projects/', data),

  // Update project
  update: (id, data) => api.patch(`/projects/${id}/`, data),

  // Delete project
  delete: (id) => api.delete(`/projects/${id}/`),

  // Get project members
  members: (id) => api.get(`/projects/${id}/members/`),

  // Add member to project
  addMember: (id, data) => api.post(`/projects/${id}/members/`, data),

  // Get project items
  items: (id, params = {}) => api.get(`/projects/${id}/items/`, { params }),

  // Archive project
  archive: (id) => api.post(`/projects/${id}/archive/`),

  // Unarchive project
  unarchive: (id) => api.post(`/projects/${id}/unarchive/`),

  // Bulk delete projects
  bulkDelete: (ids) => api.post('/projects/bulk-delete/', { project_ids: ids }),

  // Search organization members for assignee dropdown
  searchOrgMembers: (id, search = '') =>
    api.get(`/projects/${id}/org-members/`, { params: search ? { search } : {} }),
}

// Comments API
export const commentsApi = {
  // List comments for an item
  list: (itemId) => api.get('/comments/', { params: { item: itemId } }),

  // List every comment page for an item. The comment UI renders a full thread,
  // so loading only page 1 hides newer comments once a task has more than the
  // default page size.
  listAll: async (itemId) => {
    const comments = []
    let res = await api.get('/comments/', { params: { item: itemId } })

    while (true) {
      const data = res.data
      const page = data?.results || data
      if (Array.isArray(page)) {
        comments.push(...page)
      }

      if (!data?.next) {
        break
      }

      const nextUrl = new URL(data.next, window.location.origin)
      res = await api.get(`${nextUrl.pathname}${nextUrl.search}`)
    }

    return comments
  },

  // Get single comment
  get: (id) => api.get(`/comments/${id}/`),

  // Create comment
  create: (data) => api.post('/comments/', data),

  // Update comment
  update: (id, data) => api.patch(`/comments/${id}/`, data),

  // Delete comment
  delete: (id) => api.delete(`/comments/${id}/`),

  // Reactions
  getReactions: (id) => api.get(`/comments/${id}/reactions/`),
  addReaction: (id, reaction) => api.post(`/comments/${id}/reactions/`, { reaction }),
  removeReaction: (id, reaction) => api.delete(`/comments/${id}/reactions/`, { params: { reaction } }),
}

// Attachments API
export const attachmentsApi = {
  // List attachments for an item
  list: (itemId) => api.get('/attachments/', { params: { item: itemId } }),

  // Get single attachment
  get: (id) => api.get(`/attachments/${id}/`),

  // Upload attachment (multipart form data)
  upload: (formData) => api.post('/attachments/', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }),

  // Delete attachment
  delete: (id) => api.delete(`/attachments/${id}/`),

  // Get download URL
  download: (id) => api.get(`/attachments/${id}/download/`),
}

// Auth API
export const authApi = {
  login: (data) => api.post('/auth/login/', data),
  logout: (data) => api.post('/auth/logout/', data),
  register: (data) => api.post('/auth/register/', data),
  refresh: (data) => api.post('/auth/refresh/', data),
  getUser: () => api.get('/auth/user/'),
  updateUser: (data) => api.patch('/auth/user/', data),
  changePassword: (data) => api.post('/auth/password/change/', data),
  getOAuthStatus: () => api.get('/auth/oauth/status/'),
  getGoogleUrl: () => api.get('/auth/social/google/'),
  getGitHubUrl: () => api.get('/auth/social/github/'),
  socialCallback: (data) => api.post('/auth/social/callback/', data),
}

// Webhook Keys API
export const webhookKeysApi = {
  // List user's webhook keys
  list: () => api.get('/webhook-keys/'),

  // Get single webhook key
  get: (id) => api.get(`/webhook-keys/${id}/`),

  // Create webhook key
  create: (data) => api.post('/webhook-keys/', data),

  // Update webhook key
  update: (id, data) => api.patch(`/webhook-keys/${id}/`, data),

  // Delete webhook key
  delete: (id) => api.delete(`/webhook-keys/${id}/`),

  // Get webhook setup info
  webhookInfo: () => api.get('/webhook-keys/webhook_info/'),
}

// Sprints API
export const sprintsApi = {
  // List sprints (can filter by project)
  list: (params = {}) => api.get('/sprints/', { params }),

  // Get single sprint
  get: (id) => api.get(`/sprints/${id}/`),

  // Create sprint
  create: (data) => api.post('/sprints/', data),

  // Update sprint
  update: (id, data) => api.patch(`/sprints/${id}/`, data),

  // Delete sprint
  delete: (id) => api.delete(`/sprints/${id}/`),

  // Start sprint
  start: (id) => api.post(`/sprints/${id}/start/`),

  // Complete sprint
  complete: (id, data = {}) => api.post(`/sprints/${id}/complete/`, data),

  // Get burndown data for sprint
  burndown: (id) => api.get(`/sprints/${id}/burndown/`),

  // Get velocity data for a project
  velocity: (params = {}) => api.get('/sprints/velocity/', { params }),

  // Get items in sprint
  items: (id, params = {}) => api.get(`/sprints/${id}/items/`, { params }),

  // Get active sprint for a project
  active: (projectId) => api.get('/sprints/active/', { params: { project: projectId } }),
}

// =============================================================================
// Phase 3: Components API
// =============================================================================
export const componentsApi = {
  // List components (can filter by project)
  list: (params = {}) => api.get('/components/', { params }),

  // Get single component
  get: (id) => api.get(`/components/${id}/`),

  // Create component
  create: (data) => api.post('/components/', data),

  // Update component
  update: (id, data) => api.patch(`/components/${id}/`, data),

  // Delete component
  delete: (id) => api.delete(`/components/${id}/`),

  // Get items in component
  items: (id, params = {}) => api.get(`/components/${id}/items/`, { params }),
}

// =============================================================================
// Phase 3: Versions API
// =============================================================================
export const versionsApi = {
  // List versions (can filter by project, status, exclude_archived)
  list: (params = {}) => api.get('/versions/', { params }),

  // Get single version
  get: (id) => api.get(`/versions/${id}/`),

  // Create version
  create: (data) => api.post('/versions/', data),

  // Update version
  update: (id, data) => api.patch(`/versions/${id}/`, data),

  // Delete version
  delete: (id) => api.delete(`/versions/${id}/`),

  // Release a version
  release: (id) => api.post(`/versions/${id}/release/`),

  // Get items targeting this version (fix_version)
  items: (id, params = {}) => api.get(`/versions/${id}/items/`, { params }),

  // Get items affecting this version
  affectedItems: (id) => api.get(`/versions/${id}/affected_items/`),
}

// =============================================================================
// Phase 3: Work Logs API
// =============================================================================
export const workLogsApi = {
  // List work logs (can filter by item, user_id, date_from, date_to)
  list: (params = {}) => api.get('/work-logs/', { params }),

  // Get single work log
  get: (id) => api.get(`/work-logs/${id}/`),

  // Create work log
  create: (data) => api.post('/work-logs/', data),

  // Update work log
  update: (id, data) => api.patch(`/work-logs/${id}/`, data),

  // Delete work log
  delete: (id) => api.delete(`/work-logs/${id}/`),

  // Get current user's work logs
  myLogs: (params = {}) => api.get('/work-logs/my_logs/', { params }),

  // Get work logs for a specific item
  byItem: (itemId) => api.get('/work-logs/by_item/', { params: { item: itemId } }),
}

// =============================================================================
// Phase 3: Workflows API
// =============================================================================
// Status catalog — the shared list of status slugs (todo / in_progress / done)
// used for multi-select filters.
export const statusesApi = {
  list: (params = {}) => api.get('/statuses/', { params }),
}

export const workflowsApi = {
  // List workflows (can filter by project)
  list: (params = {}) => api.get('/workflows/', { params }),

  // Get single workflow
  get: (id) => api.get(`/workflows/${id}/`),

  // Update workflow
  update: (id, data) => api.patch(`/workflows/${id}/`, data),

  // Add status to workflow
  addStatus: (id, data) => api.post(`/workflows/${id}/add_status/`, data),

  // Remove status from workflow
  removeStatus: (id, statusName) => api.post(`/workflows/${id}/remove_status/`, { name: statusName }),

  // Reorder statuses
  reorderStatuses: (id, statuses) => api.post(`/workflows/${id}/reorder_statuses/`, { statuses }),

  // Get available transitions from a status
  availableTransitions: (id, fromStatus) =>
    api.get(`/workflows/${id}/available_transitions/`, { params: { from_status: fromStatus } }),
  createTransition: (data) => api.post("/workflow-transitions/", data),
  updateTransition: (id, data) => api.patch(`/workflow-transitions/${id}/`, data),
  deleteTransition: (id) => api.delete(`/workflow-transitions/${id}/`),
  // Workflows (full status + transition + knowledge article details)
  listJiraWorkflows: (params = {}) => api.get("/workflows/", { params }),
  getJiraWorkflow: (id) => api.get(`/workflows/${encodeURIComponent(id)}/`),
}

// =============================================================================
// Phase 3: Board Columns API
// =============================================================================
export const boardColumnsApi = {
  // List columns (can filter by project)
  list: (params = {}) => api.get('/board-columns/', { params }),

  // Get single column
  get: (id) => api.get(`/board-columns/${id}/`),

  // Create column
  create: (data) => api.post('/board-columns/', data),

  // Update column
  update: (id, data) => api.patch(`/board-columns/${id}/`, data),

  // Delete column
  delete: (id) => api.delete(`/board-columns/${id}/`),

  // Reorder columns
  reorder: (columnIds) => api.post('/board-columns/reorder/', { column_ids: columnIds }),

  // Create default columns for a project
  createDefaults: (projectId) => api.post('/board-columns/create_defaults/', { project: projectId }),
}

// =============================================================================
// Phase 3: Saved Filters API (JQL)
// =============================================================================
export const savedFiltersApi = {
  // List saved filters (can filter by project)
  list: (params = {}) => api.get('/saved-filters/', { params }),

  // Get single filter
  get: (id) => api.get(`/saved-filters/${id}/`),

  // Create filter
  create: (data) => api.post('/saved-filters/', data),

  // Update filter
  update: (id, data) => api.patch(`/saved-filters/${id}/`, data),

  // Delete filter
  delete: (id) => api.delete(`/saved-filters/${id}/`),

  // Toggle share status
  toggleShare: (id) => api.post(`/saved-filters/${id}/toggle_share/`),

  // Execute filter and get results
  execute: (id) => api.get(`/saved-filters/${id}/execute/`),
}

// =============================================================================
// Notifications API (in-app notification history)
// =============================================================================
export const notificationsApi = {
  list: (params = {}) => api.get('/notifications/', { params }),
  get: (id) => api.get(`/notifications/${id}/`),
  unreadCount: () => api.get('/notifications/unread_count/'),
  read: (id) => api.post(`/notifications/${id}/read/`),
  readAll: () => api.post('/notifications/read_all/'),
  delete: (id) => api.delete(`/notifications/${id}/`),
  clearAll: () => api.post('/notifications/clear_all/'),
  clearRead: () => api.post('/notifications/clear_read/'),
}

// =============================================================================
// Phase 5: Issue Types API
// =============================================================================
export const issueTypesApi = {
  list: (params = {}) => api.get('/issue-types/', { params }),
  get: (id) => api.get(`/issue-types/${id}/`),
  create: (data) => api.post('/issue-types/', data),
  update: (id, data) => api.patch(`/issue-types/${id}/`, data),
  delete: (id) => api.delete(`/issue-types/${id}/`),
}

// =============================================================================
// Phase 5: Issue Type Schemes API
// =============================================================================
export const issueTypeSchemesApi = {
  list: (params = {}) => api.get('/issue-type-schemes/', { params }),
  get: (id) => api.get(`/issue-type-schemes/${id}/`),
  create: (data) => api.post('/issue-type-schemes/', data),
  update: (id, data) => api.patch(`/issue-type-schemes/${id}/`, data),
  delete: (id) => api.delete(`/issue-type-schemes/${id}/`),
  addMapping: (id, data) => api.post(`/issue-type-schemes/${id}/add_mapping/`, data),
  removeMapping: (id, data) => api.post(`/issue-type-schemes/${id}/remove_mapping/`, data),
}

// =============================================================================
// Phase 5: Custom Fields API
// =============================================================================
export const customFieldsApi = {
  list: (params = {}) => api.get('/custom-fields/', { params }),
  get: (id) => api.get(`/custom-fields/${id}/`),
  create: (data) => api.post('/custom-fields/', data),
  update: (id, data) => api.patch(`/custom-fields/${id}/`, data),
  delete: (id) => api.delete(`/custom-fields/${id}/`),
}

export const customFieldContextsApi = {
  list: (params = {}) => api.get('/custom-field-contexts/', { params }),
  get: (id) => api.get(`/custom-field-contexts/${id}/`),
  create: (data) => api.post('/custom-field-contexts/', data),
  update: (id, data) => api.patch(`/custom-field-contexts/${id}/`, data),
  delete: (id) => api.delete(`/custom-field-contexts/${id}/`),
}

export const customFieldValuesApi = {
  list: (params = {}) => api.get('/custom-field-values/', { params }),
  get: (id) => api.get(`/custom-field-values/${id}/`),
  create: (data) => api.post('/custom-field-values/', data),
  update: (id, data) => api.patch(`/custom-field-values/${id}/`, data),
  delete: (id) => api.delete(`/custom-field-values/${id}/`),
}

// =============================================================================
// Phase 5: Screens API
// =============================================================================
export const screensApi = {
  list: (params = {}) => api.get('/screens/', { params }),
  get: (id) => api.get(`/screens/${id}/`),
  create: (data) => api.post('/screens/', data),
  update: (id, data) => api.patch(`/screens/${id}/`, data),
  delete: (id) => api.delete(`/screens/${id}/`),
}

export const screenSchemesApi = {
  list: (params = {}) => api.get('/screen-schemes/', { params }),
  get: (id) => api.get(`/screen-schemes/${id}/`),
  create: (data) => api.post('/screen-schemes/', data),
  update: (id, data) => api.patch(`/screen-schemes/${id}/`, data),
  delete: (id) => api.delete(`/screen-schemes/${id}/`),
}

export const issueTypeScreenSchemesApi = {
  list: (params = {}) => api.get('/issue-type-screen-schemes/', { params }),
  get: (id) => api.get(`/issue-type-screen-schemes/${id}/`),
  create: (data) => api.post('/issue-type-screen-schemes/', data),
  update: (id, data) => api.patch(`/issue-type-screen-schemes/${id}/`, data),
  delete: (id) => api.delete(`/issue-type-screen-schemes/${id}/`),
}

// =============================================================================
// Phase 5: Security Schemes API
// =============================================================================
export const securitySchemesApi = {
  list: (params = {}) => api.get('/security-schemes/', { params }),
  get: (id) => api.get(`/security-schemes/${id}/`),
  create: (data) => api.post('/security-schemes/', data),
  update: (id, data) => api.patch(`/security-schemes/${id}/`, data),
  delete: (id) => api.delete(`/security-schemes/${id}/`),
  addLevel: (id, data) => api.post(`/security-schemes/${id}/add_level/`, data),
  removeLevel: (id, data) => api.post(`/security-schemes/${id}/remove_level/`, data),
}

// =============================================================================
// Phase 5: Permission Schemes API
// =============================================================================
export const permissionSchemesApi = {
  list: (params = {}) => api.get('/permission-schemes/', { params }),
  get: (id) => api.get(`/permission-schemes/${id}/`),
  create: (data) => api.post('/permission-schemes/', data),
  update: (id, data) => api.patch(`/permission-schemes/${id}/`, data),
  delete: (id) => api.delete(`/permission-schemes/${id}/`),
  addGrant: (id, data) => api.post(`/permission-schemes/${id}/add_grant/`, data),
  removeGrant: (id, data) => api.post(`/permission-schemes/${id}/remove_grant/`, data),
}

// =============================================================================
// Phase 5: Notification Schemes API
// =============================================================================
export const notificationSchemesApi = {
  list: (params = {}) => api.get('/notification-schemes/', { params }),
  get: (id) => api.get(`/notification-schemes/${id}/`),
  create: (data) => api.post('/notification-schemes/', data),
  update: (id, data) => api.patch(`/notification-schemes/${id}/`, data),
  delete: (id) => api.delete(`/notification-schemes/${id}/`),
  addEvent: (id, data) => api.post(`/notification-schemes/${id}/add_event/`, data),
  removeEvent: (id, data) => api.post(`/notification-schemes/${id}/remove_event/`, data),
}

// =============================================================================
// Phase 5: SLA API
// =============================================================================
export const slaDefinitionsApi = {
  list: (params = {}) => api.get('/sla-definitions/', { params }),
  get: (id) => api.get(`/sla-definitions/${id}/`),
  create: (data) => api.post('/sla-definitions/', data),
  update: (id, data) => api.patch(`/sla-definitions/${id}/`, data),
  delete: (id) => api.delete(`/sla-definitions/${id}/`),
}

export const businessCalendarsApi = {
  list: (params = {}) => api.get('/business-calendars/', { params }),
  get: (id) => api.get(`/business-calendars/${id}/`),
  create: (data) => api.post('/business-calendars/', data),
  update: (id, data) => api.patch(`/business-calendars/${id}/`, data),
  delete: (id) => api.delete(`/business-calendars/${id}/`),
}

export const slaInstancesApi = {
  list: (params = {}) => api.get('/sla-instances/', { params }),
  get: (id) => api.get(`/sla-instances/${id}/`),
}

// =============================================================================
// Phase 5: Boards API
// =============================================================================
export const boardsApi = {
  list: (params = {}) => api.get('/boards/', { params }),
  get: (id) => api.get(`/boards/${id}/`),
  create: (data) => api.post('/boards/', data),
  update: (id, data) => api.patch(`/boards/${id}/`, data),
  delete: (id) => api.delete(`/boards/${id}/`),
  addQuickFilter: (id, data) => api.post(`/boards/${id}/add_quick_filter/`, data),
  removeQuickFilter: (id, data) => api.post(`/boards/${id}/remove_quick_filter/`, data),
}

export const boardQuickFiltersApi = {
  list: (params = {}) => api.get('/board-quick-filters/', { params }),
  get: (id) => api.get(`/board-quick-filters/${id}/`),
  create: (data) => api.post('/board-quick-filters/', data),
  update: (id, data) => api.patch(`/board-quick-filters/${id}/`, data),
  delete: (id) => api.delete(`/board-quick-filters/${id}/`),
}

// =============================================================================
// Phase 5: Dashboards API
// =============================================================================
export const dashboardsApi = {
  list: (params = {}) => api.get('/dashboards/', { params }),
  get: (id) => api.get(`/dashboards/${id}/`),
  create: (data) => api.post('/dashboards/', data),
  update: (id, data) => api.patch(`/dashboards/${id}/`, data),
  delete: (id) => api.delete(`/dashboards/${id}/`),
}

export const dashboardGadgetsApi = {
  list: (params = {}) => api.get('/dashboard-gadgets/', { params }),
  get: (id) => api.get(`/dashboard-gadgets/${id}/`),
  create: (data) => api.post('/dashboard-gadgets/', data),
  update: (id, data) => api.patch(`/dashboard-gadgets/${id}/`, data),
  delete: (id) => api.delete(`/dashboard-gadgets/${id}/`),
}

// =============================================================================
// Phase 5: Automation Rules API
// =============================================================================
export const automationRulesApi = {
  list: (params = {}) => api.get('/automation-rules/', { params }),
  get: (id) => api.get(`/automation-rules/${id}/`),
  create: (data) => api.post('/automation-rules/', data),
  update: (id, data) => api.patch(`/automation-rules/${id}/`, data),
  delete: (id) => api.delete(`/automation-rules/${id}/`),
  toggle: (id) => api.post(`/automation-rules/${id}/toggle/`),
  logs: (id, params = {}) => api.get(`/automation-rules/${id}/logs/`, { params }),
}

export const automationRuleLogsApi = {
  list: (params = {}) => api.get('/automation-rule-logs/', { params }),
  get: (id) => api.get(`/automation-rule-logs/${id}/`),
}

// =============================================================================
// Phase 5: Project Configuration API
// =============================================================================
export const projectConfigApi = {
  list: (params = {}) => api.get('/project-configurations/', { params }),
  get: (id) => api.get(`/project-configurations/${id}/`),
  create: (data) => api.post('/project-configurations/', data),
  update: (id, data) => api.patch(`/project-configurations/${id}/`, data),
  delete: (id) => api.delete(`/project-configurations/${id}/`),
}

// =============================================================================
// Phase 5: Bulk Operations API
// =============================================================================
export const bulkOperationsApi = {
  execute: (data) => api.post('/items/bulk/', data),
}

export default api

// Public customer request portal (PLS-273). These endpoints intentionally do
// not require the authenticated API interceptor; they still use the same base URL.
export const customerPortalApi = {
  list: (projectRef, params = {}) => api.get(`/customer-portal/${encodeURIComponent(projectRef)}/requests/`, { params }),
  submit: (projectRef, data) => api.post(`/customer-portal/${encodeURIComponent(projectRef)}/requests/`, data),
  vote: (projectRef, requestId, data = {}) => api.post(`/customer-portal/${encodeURIComponent(projectRef)}/requests/${requestId}/vote/`, data),
  comments: (projectRef, requestId) => api.get(`/customer-portal/${encodeURIComponent(projectRef)}/requests/${requestId}/comments/`),
  comment: (projectRef, requestId, data) => api.post(`/customer-portal/${encodeURIComponent(projectRef)}/requests/${requestId}/comments/`, data),
  approve: (requestId, data = {}) => api.post(`/customer-portal/requests/${requestId}/approve/`, data),
}

// Lane-health observability surface (PLS-1033 epic / PLS-1036 UI).
// Contract pin v9: GET /pulse/api/lane-health/ — bare `lanes` array, field names
// as-served (§3.1). `?team=<slug>` returns the single lane object directly.
// Read-only; any authenticated seat (IsAuthenticated).
export const laneHealthApi = {
  list: () => api.get('/lane-health/'),
  get: (team) => api.get('/lane-health/', { params: { team } }),
}

