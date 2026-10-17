import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { organizationsApi, projectsApi } from '../services/api'
import { deriveOrganizationsFromProjects } from '../utils/deriveOrganizationsFromProjects'
import { restoredOrganization } from '../utils/createTaskRequest'
import { useAuth } from './AuthContext'

const OrganizationContext = createContext(null)

// Storage keys
const CURRENT_ORG_KEY = 'shizuha_current_org'
const CURRENT_PROJECT_KEY = 'shizuha_current_project'

export function OrganizationProvider({ children }) {
  const { isAuthenticated, user } = useAuth()
  const [organizations, setOrganizations] = useState([])
  const [currentOrganization, setCurrentOrganization] = useState(null)
  const [projects, setProjects] = useState([])
  const [currentProject, setCurrentProject] = useState(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isOrganizationResolved, setIsOrganizationResolved] = useState(false)
  const [organizationResolutionError, setOrganizationResolutionError] = useState(null)

  // Load organizations when authenticated
  useEffect(() => {
    if (isAuthenticated) {
      loadOrganizations()
    } else {
      setOrganizations([])
      setCurrentOrganization(null)
      setProjects([])
      setCurrentProject(null)
      setIsOrganizationResolved(false)
      setOrganizationResolutionError(null)
      setIsLoading(false)
    }
  }, [isAuthenticated])

  // Load projects when organization changes
  useEffect(() => {
    if (!isAuthenticated || !isOrganizationResolved) {
      setProjects([])
      setCurrentProject(null)
    } else if (currentOrganization) {
      loadProjects(currentOrganization.slug)
    } else {
      // All Organizations mode — load all accessible projects
      loadAllProjects()
    }
  }, [currentOrganization, isAuthenticated, isOrganizationResolved])

  const loadOrganizations = async () => {
    try {
      setIsLoading(true)
      setIsOrganizationResolved(false)
      setOrganizationResolutionError(null)

      // Organizations are managed by shizuha-admin (the Admin API). Non-admin
      // authenticated users (customers, and agent seats without the `admin`
      // service) get 403 on /admin/api/organizations/ (force-assigned PLS-779:
      // "Organization scope unavailable" hard-fails Tasks for them). Fall back to
      // deriving the org set from the member-scoped /pulse/api/projects/ response,
      // which carries organization_id/name/slug per project and works for every
      // authenticated user.
      const orgs = []
      try {
        let page = 1
        let hasNextPage = true
        while (hasNextPage) {
          const response = await organizationsApi.list({ page, page_size: 100 })
          const responseOrgs = response.data.results || response.data
          orgs.push(...responseOrgs)
          hasNextPage = Boolean(response.data.results && response.data.next)
          page += 1
        }
        if (orgs.length === 0) throw new Error('empty org list from admin')
      } catch (orgErr) {
        // Admin org list unavailable (403 service_access_denied for non-admins,
        // or empty). Derive the accessible set from the projects the user can
        // actually see — these are the only orgs any reasoned scope can target.
        const projects = await projectsApi.listAll()
        orgs.push(...deriveOrganizationsFromProjects(projects))
        if (orgs.length === 0) {
          console.warn('PLS-779: no org source available for this user', orgErr)
        }
      }
      setOrganizations(orgs)

      // Hive hands the selected organization to Pulse as ?org=<slug>. Honor
      // that explicit handoff before the locally persisted selection so a
      // first-run journey cannot silently fall back to "All Organizations".
      const searchParams = new URLSearchParams(window.location.search)
      const requestedOrg = (
        searchParams.get('org') ||
        searchParams.get('organization') ||
        searchParams.get('organization_id') ||
        ''
      ).trim()
      if (requestedOrg) {
        const matchedOrg = orgs.find((org) => (
          org.slug === requestedOrg || String(org.id) === requestedOrg
        ))
        if (matchedOrg) {
          setCurrentOrganization(matchedOrg)
          localStorage.setItem(CURRENT_ORG_KEY, matchedOrg.slug)
          setIsOrganizationResolved(true)
          return
        }

        // An explicit handoff is a scope boundary, not a hint. If it cannot be
        // resolved, stay fail-closed rather than falling through to a saved org
        // or the global selection.
        setCurrentOrganization(null)
        setOrganizationResolutionError(
          'The requested organization is unavailable. Check your access or choose another organization.'
        )
        return
      }

      // A person with one workspace should land in it. "All Organizations"
      // stays the choice only when they picked it, or when they belong to
      // more than one. A missing saved slug used to post creates with no
      // organization header, and the dialog then sat on a silent 400.
      const savedOrgSlug = localStorage.getItem(CURRENT_ORG_KEY)
      const restored = restoredOrganization(orgs, savedOrgSlug)
      setCurrentOrganization(restored.org)
      if (restored.persistSlug) {
        localStorage.setItem(CURRENT_ORG_KEY, restored.persistSlug)
      }
      setIsOrganizationResolved(true)
    } catch (error) {
      console.error('Failed to load organizations:', error)
      setCurrentOrganization(null)
      setOrganizationResolutionError(
        'Organizations could not be loaded. Retry before viewing scoped tasks.'
      )
    } finally {
      setIsLoading(false)
    }
  }

  const loadAllProjects = async () => {
    try {
      const projectList = await projectsApi.listAll()
      setProjects(projectList)

      // Try to restore current project from localStorage
      const savedProjectId = localStorage.getItem(CURRENT_PROJECT_KEY)
      if (savedProjectId) {
        const savedProject = projectList.find(p => p.id === parseInt(savedProjectId))
        if (savedProject) {
          setCurrentProject(savedProject)
        } else {
          setCurrentProject(null)
          localStorage.removeItem(CURRENT_PROJECT_KEY)
        }
      }
    } catch (error) {
      console.error('Failed to load all projects:', error)
      setProjects([])
    }
  }

  const loadProjects = async (orgSlug) => {
    try {
      // Get organization ID from slug to filter projects
      const org = organizations.find(o => o.slug === orgSlug)
      if (!org) {
        setProjects([])
        return
      }
      // Use Pulse's projects API with organization filter
      const projectList = await projectsApi.listAll({ organization: org.id })
      setProjects(projectList)

      // Try to restore current project from localStorage
      const savedProjectId = localStorage.getItem(CURRENT_PROJECT_KEY)
      if (savedProjectId) {
        const savedProject = projectList.find(p => p.id === parseInt(savedProjectId))
        if (savedProject) {
          setCurrentProject(savedProject)
        } else {
          // Saved project not found in this org
          setCurrentProject(null)
          localStorage.removeItem(CURRENT_PROJECT_KEY)
        }
      }
    } catch (error) {
      console.error('Failed to load projects:', error)
      setProjects([])
    }
  }

  const switchOrganization = useCallback((org) => {
    setOrganizationResolutionError(null)
    setIsOrganizationResolved(true)
    setCurrentOrganization(org)
    setCurrentProject(null)
    localStorage.setItem(CURRENT_ORG_KEY, org ? org.slug : '__all__')
    localStorage.removeItem(CURRENT_PROJECT_KEY)
  }, [])

  const switchProject = useCallback((project) => {
    setCurrentProject(project)
    if (project) {
      localStorage.setItem(CURRENT_PROJECT_KEY, project.id.toString())
    } else {
      localStorage.removeItem(CURRENT_PROJECT_KEY)
    }
  }, [])

  const createOrganization = useCallback(async (data) => {
    const response = await organizationsApi.create(data)
    await loadOrganizations()
    return response.data
  }, [])

  const updateOrganization = useCallback(async (slug, data) => {
    const response = await organizationsApi.update(slug, data)
    await loadOrganizations()
    return response.data
  }, [])

  const reloadProjects = async () => {
    if (currentOrganization) {
      await loadProjects(currentOrganization.slug)
    } else {
      await loadAllProjects()
    }
  }

  const createProject = useCallback(async (data) => {
    const response = await projectsApi.create(data)
    await reloadProjects()
    return response.data
  }, [currentOrganization])

  const updateProject = useCallback(async (id, data) => {
    const response = await projectsApi.update(id, data)
    await reloadProjects()
    return response.data
  }, [currentOrganization])

  const archiveProject = useCallback(async (id) => {
    const response = await projectsApi.archive(id)
    await reloadProjects()
    if (currentProject?.id === id) {
      setCurrentProject(null)
      localStorage.removeItem(CURRENT_PROJECT_KEY)
    }
    return response.data
  }, [currentOrganization, currentProject])

  const deleteProjects = useCallback(async (ids) => {
    const response = await projectsApi.bulkDelete(ids)
    await reloadProjects()
    if (currentProject && ids.includes(currentProject.id)) {
      setCurrentProject(null)
      localStorage.removeItem(CURRENT_PROJECT_KEY)
    }
    return response.data
  }, [currentOrganization, currentProject])

  const refreshOrganizations = useCallback(() => {
    return loadOrganizations()
  }, [])

  const refreshProjects = useCallback(() => {
    return reloadProjects()
  }, [currentOrganization])

  const value = {
    // Organizations
    organizations,
    currentOrganization,
    switchOrganization,
    createOrganization,
    updateOrganization,
    refreshOrganizations,

    // Projects
    projects,
    currentProject,
    switchProject,
    createProject,
    updateProject,
    archiveProject,
    deleteProjects,
    refreshProjects,

    // Loading state
    isLoading,
    // `currentOrganization === null` is a valid, intentional "All
    // Organizations" selection once loading completes. Consumers that issue
    // scoped requests must gate on this flag so they cannot mistake the
    // unresolved handoff state for the global selection.
    isOrganizationResolved,
    organizationResolutionError,
  }

  return (
    <OrganizationContext.Provider value={value}>
      {children}
    </OrganizationContext.Provider>
  )
}

export function useOrganization() {
  const context = useContext(OrganizationContext)
  if (!context) {
    throw new Error('useOrganization must be used within an OrganizationProvider')
  }
  return context
}

export default OrganizationContext
