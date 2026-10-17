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
