/**
 * PLS-779: derive the accessible org set from the member-scoped
 * /pulse/api/projects/ response.
 *
 * The Admin org list (/admin/api/organizations/) 403s with
 * service_access_denied for any authenticated user whose JWT enabled_services
 * lacks `admin` (customers, and agent seats without the admin service). The
 * projects a user can actually see carry organization_id/name/slug per project,
 * so the reasoned scopes they can target are exactly the orgs their projects
 * live in.
 *
 * Pure — no I/O, unit-testable in plain node.
 *
 * @param {Array} projects projects from projectsApi.listAll()
 * @returns {Array<{id: number|string, name: string, slug: string}>} derived orgs
 */
export function deriveOrganizationsFromProjects(projects) {
  const seen = new Map()
  for (const p of projects || []) {
    if (!p || !p.organization_id) continue
    seen.set(String(p.organization_id), {
      id: p.organization_id,
      name: p.organization_name || `Organization ${p.organization_id}`,
      slug: p.organization_slug || `org-${p.organization_id}`,
    })
  }
  return [...seen.values()]
}

/**
 * PLS-1162: union the JWT's organization_memberships claim into a
 * project-derived org set. Projects alone cannot surface EMPTY orgs, and the
 * Hive empty-org guided onboarding hands off exactly such an org by numeric
 * id (?org=468 with zero projects). Membership-only orgs get the same
 * `Organization <id>` / `org-<id>` fallback naming the project path uses.
 *
 * @param {Array<{id: number|string, name: string, slug: string}>} orgs orgs derived from projects
 * @param {Record<string, string>|undefined} memberships user.organization_memberships ({orgId: role})
 * @returns {Array<{id: number|string, name: string, slug: string, membership_role?: string}>}
 */
export function unionMembershipOrganizations(orgs, memberships) {
  const out = [...(orgs || [])]
  for (const [orgId, role] of Object.entries(memberships || {})) {
    const id = String(orgId)
    if (out.some((o) => String(o.id) === id)) continue
    out.push({
      id: orgId,
      name: `Organization ${id}`,
      slug: `org-${id}`,
      membership_role: typeof role === 'string' ? role : undefined,
    })
  }
  return out
}
