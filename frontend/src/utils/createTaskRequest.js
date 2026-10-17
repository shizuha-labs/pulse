// A task create with no organization header is rejected
// ("A task must belong to an organization project") and the dialog used to
// stay open with no message. One workspace is enough to name the header.

function usableOrganizations(organizations) {
  const orgs = Array.isArray(organizations) ? organizations : []
  return orgs.filter((org) => org?.id != null && org.id !== '')
}

function isPersonalWorkspace(org) {
  if (org?.is_personal === true) return true
  return String(org?.slug || '').startsWith('personal-')
}

export function organizationIdForNewTask(currentOrganization, organizations) {
  if (currentOrganization?.id != null && currentOrganization.id !== '') {
    return currentOrganization.id
  }
  const orgs = usableOrganizations(organizations)
  if (orgs.length === 1) return orgs[0].id
  // All Organizations plus a personal workspace and a company: a blank
  // required Workspace select never submits, so Create looks frozen.
  // The personal workspace is the default; the select still lists the rest.
  const personal = orgs.filter(isPersonalWorkspace)
  if (personal.length === 1) return personal[0].id
  return null
}

export function restoredOrganization(organizations, savedSlug) {
  const orgs = Array.isArray(organizations) ? organizations : []
  if (savedSlug === '__all__') return { org: null, persistSlug: null }
  if (savedSlug) {
    const saved = orgs.find((org) => org.slug === savedSlug) || null
    if (saved) return { org: saved, persistSlug: null }
    return { org: null, persistSlug: '__all__' }
  }
  if (orgs.length === 1 && orgs[0]?.slug) {
    return { org: orgs[0], persistSlug: orgs[0].slug }
  }
  return { org: null, persistSlug: null }
}

export function formatApiError(err, fallback) {
  const data = err?.response?.data ?? err?.data
  if (typeof data === 'string' && data.trim()) return data.trim()
  if (data && typeof data === 'object') {
    if (typeof data.detail === 'string' && data.detail.trim()) return data.detail.trim()
    if (typeof data.error === 'string' && data.error.trim()) return data.error.trim()
    const parts = []
    for (const [key, value] of Object.entries(data)) {
      const text = Array.isArray(value)
        ? value.map((entry) => (typeof entry === 'string' ? entry : '')).filter(Boolean).join(' ')
        : (typeof value === 'string' ? value : '')
      if (!text) continue
      parts.push(key === 'non_field_errors' || key === 'detail' ? text : text)
    }
    if (parts.length) return parts.join(' ')
  }
  if (typeof err?.message === 'string' && err.message.trim() && !err.message.startsWith('Request failed')) {
    return err.message.trim()
  }
  return fallback
}
