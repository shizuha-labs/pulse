const TEAM_LABELS = {
  'admin-ops': 'Admin Ops',
  'ai-models': 'AI Models',
  'ceo-decisions': 'CEO Decisions',
  devops: 'DevOps',
  hr: 'HR',
  mcp: 'MCP',
  qa: 'QA',
  scs: 'SCS',
}

const ACRONYMS = new Set(['ai', 'api', 'ci', 'ceo', 'cto', 'hr', 'mcp', 'qa', 'scs', 'ui'])

const firstPresent = (...values) => values.find(value => value !== undefined && value !== null && String(value).trim() !== '')

export function formatTeamName(teamName) {
  if (!teamName) return ''

  const raw = String(teamName).trim()
  if (!raw) return ''
  if (raw === 'Unassigned') return raw

  const normalized = raw.toLowerCase().replace(/_/g, '-')
  if (TEAM_LABELS[normalized]) return TEAM_LABELS[normalized]

  return normalized
    .split('-')
    .filter(Boolean)
    .map((part) => {
      if (ACRONYMS.has(part)) return part.toUpperCase()
      return part.charAt(0).toUpperCase() + part.slice(1)
    })
    .join(' ')
}

export function formatOrganizationName(organization) {
  if (!organization) return ''
  if (typeof organization === 'string') {
    return formatTeamName(organization)
  }

  const explicitName = firstPresent(organization.organization_name, organization.name)
  if (explicitName) return String(explicitName).trim()

  const slug = firstPresent(organization.organization_slug, organization.slug)
  if (slug) return formatTeamName(slug)

  const id = firstPresent(organization.organization_id, organization.id)
  return id !== undefined ? `Organization #${id}` : ''
}

export function getTeamRoutingKey(team) {
  if (!team) return ''
  if (typeof team === 'string') return team.trim()
  return String(firstPresent(
    // The server's canonical routing name is the value accepted by the assign
    // endpoint. canonical_key is an org-qualified identity for UI/cache use.
    team.canonical_name,
    team.routing_key,
    team.team_key,
    team.key,
    team.slug,
    team.team_name,
    team.canonical_key,
    // Compatibility with the pre-org-scoped endpoint. New responses send an
    // explicit canonical_name so display labels never become routing keys.
    team.name,
  ) || '').trim()
}

export function normalizeTeamOption(team, fallbackOrganization = {}) {
  const source = team || {}
  const teamKey = getTeamRoutingKey(source)
  const displayName = String(firstPresent(
    source.display_name,
    source.team_name,
    source.name,
    teamKey,
  ) || '').trim()
  const organizationId = firstPresent(
    source.organization_id,
    source.organization?.id,
    fallbackOrganization.organization_id,
    fallbackOrganization.id,
  )
  const organizationSlug = String(firstPresent(
    source.organization_slug,
    source.organization?.slug,
    fallbackOrganization.organization_slug,
    fallbackOrganization.slug,
  ) || '').trim()
  const hasExplicitOrganizationScope = firstPresent(
    source.organization_id,
    source.organization?.id,
    source.organization_slug,
    source.organization?.slug,
  ) !== undefined
  const organizationName = String(firstPresent(
    source.organization_name,
    source.organization?.name,
    !hasExplicitOrganizationScope ? fallbackOrganization.organization_name : undefined,
    !hasExplicitOrganizationScope ? fallbackOrganization.name : undefined,
  ) || '').trim()

  return {
    ...source,
    team_key: teamKey,
    display_name: displayName,
    organization_id: organizationId ?? null,
    organization_slug: organizationSlug,
    organization_name: organizationName,
  }
}

export function formatScopedTeamName(team, fallbackOrganization = {}) {
  const option = normalizeTeamOption(team, fallbackOrganization)
  const teamName = formatTeamName(option.display_name || option.team_key)
  const organizationName = formatOrganizationName({
    organization_name: option.organization_name,
    organization_slug: option.organization_slug,
  })
  return [organizationName, teamName].filter(Boolean).join(' › ')
}

export function buildTeamScope({ organizationId, organizationSlug, projectId } = {}) {
  const id = organizationId !== undefined && organizationId !== null && String(organizationId).trim() !== ''
    ? organizationId
    : null
  const slug = String(organizationSlug || '').trim()
  const project = projectId !== undefined && projectId !== null && String(projectId).trim() !== ''
    ? projectId
    : null

  return {
    isScoped: id !== null || Boolean(slug),
    cacheKey: id !== null ? `id:${id}` : slug ? `slug:${slug}` : 'all',
    // The server derives the owning organization from this authorized project;
    // caller-supplied organization selectors are intentionally not accepted.
    params: project !== null ? { project_id: project } : {},
  }
}
