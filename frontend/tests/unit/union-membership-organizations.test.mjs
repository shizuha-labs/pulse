/**
 * PLS-1162: regression test — the Hive empty-org guided onboarding hands off
 * a numeric org id (?org=468) for an org with ZERO projects. The project-
 * derived org set cannot surface empty orgs, so the handoff fail-closed with
 * "Organization scope unavailable" for every org. The fix unions the JWT's
 * organization_memberships claim into the set so membership alone resolves.
 *
 * Run: node frontend/tests/unit/union-membership-organizations.test.mjs
 */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const src = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../src/utils/deriveOrganizationsFromProjects.js',
)
const { deriveOrganizationsFromProjects, unionMembershipOrganizations } = await import(pathToFileURL(src).href)

// ---- empty org from the membership claim resolves (the Hive handoff case) ----
{
  // nami's seat: zero projects anywhere, member of orgs 1 and 468 (owner).
  const projects = []
  const orgs = unionMembershipOrganizations(
    deriveOrganizationsFromProjects(projects),
    { 1: 'member', 468: 'owner' },
  )
  assert.equal(orgs.length, 2, 'membership-only orgs enter the set')
  const byId = new Map(orgs.map((o) => [String(o.id), o]))
  assert.equal(byId.get('468').slug, 'org-468')
  assert.equal(byId.get('468').name, 'Organization 468')
  assert.equal(byId.get('468').membership_role, 'owner')
  // The context matcher resolves both forms: String(org.id) === '468' ✓
  assert.ok(orgs.some((o) => String(o.id) === '468'), 'numeric-id handoff resolves')
}

// ---- project-derived orgs are preserved and not duplicated ----
{
  const projects = [
    { id: 'p1', organization_id: 10, organization_name: 'Acme', organization_slug: 'acme' },
  ]
  const orgs = unionMembershipOrganizations(
    deriveOrganizationsFromProjects(projects),
    { 10: 'member', 468: 'owner' },
  )
  assert.equal(orgs.length, 2, 'org 10 kept once, org 468 added')
  const acme = orgs.find((o) => String(o.id) === '10')
  assert.equal(acme.name, 'Acme', 'project-derived name wins')
  assert.equal(acme.slug, 'acme')
  assert.equal(acme.membership_role, undefined, 'project-derived orgs carry no membership_role')
}

// ---- no memberships: unchanged behavior (PLS-779 path intact) ----
{
  const projects = [
    { id: 'p1', organization_id: 10, organization_name: 'Acme', organization_slug: 'acme' },
  ]
  const orgs = unionMembershipOrganizations(deriveOrganizationsFromProjects(projects), undefined)
  assert.equal(orgs.length, 1)
  assert.deepEqual(orgs[0], { id: 10, name: 'Acme', slug: 'acme' })
  const empty = unionMembershipOrganizations(deriveOrganizationsFromProjects([]), {})
  assert.deepEqual(empty, [])
}

// ---- non-numeric membership keys are still unioned by their string form ----
{
  const orgs = unionMembershipOrganizations([], { 'personal-16': 'owner' })
  assert.equal(orgs.length, 1)
  assert.equal(orgs[0].id, 'personal-16')
  assert.equal(orgs[0].slug, 'org-personal-16')
}

console.log('union-membership-organizations: all assertions passed')
