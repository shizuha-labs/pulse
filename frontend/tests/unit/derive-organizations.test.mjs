/**
 * PLS-779: regression test for the org-scope fallback — derive the accessible
 * org set from the member-scoped /pulse/api/projects/ response when the Admin
 * org list is unavailable (403 service_access_denied for non-admin users).
 *
 * Covers the reviewer's required cases (PLAT-1305 test evidence):
 *   - derives the org set from org-bearing projects (dedupes by org id)
 *   - skips projects without organization_id
 *   - falls back to `Organization <id>` / `org-<id>` when name/slug are missing
 *   - empty/null input yields [] (no hard error — the caller warns, not fails)
 *
 * Run: node frontend/tests/unit/derive-organizations.test.mjs
 */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const src = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../src/utils/deriveOrganizationsFromProjects.js',
)
const { deriveOrganizationsFromProjects } = await import(pathToFileURL(src).href)

// ---- derives the org set from org-bearing projects ----
{
  const projects = [
    { id: 'p1', organization_id: 10, organization_name: 'Acme', organization_slug: 'acme' },
    { id: 'p2', organization_id: 20, organization_name: 'Globex', organization_slug: 'globex' },
  ]
  const orgs = deriveOrganizationsFromProjects(projects)
  assert.equal(orgs.length, 2, 'one org per distinct organization_id')
  assert.deepEqual(orgs[0], { id: 10, name: 'Acme', slug: 'acme' })
  assert.deepEqual(orgs[1], { id: 20, name: 'Globex', slug: 'globex' })
}

// ---- dedupes multiple projects in the same org ----
{
  const projects = [
    { id: 'p1', organization_id: 10, organization_name: 'Acme', organization_slug: 'acme' },
    { id: 'p2', organization_id: 10, organization_name: 'Acme', organization_slug: 'acme' },
    { id: 'p3', organization_id: 10, organization_name: 'Acme', organization_slug: 'acme' },
  ]
  const orgs = deriveOrganizationsFromProjects(projects)
  assert.equal(orgs.length, 1, 'projects in the same org collapse to one org')
}

// ---- skips projects without organization_id ----
{
  const projects = [
    { id: 'p1', organization_id: 10, organization_name: 'Acme', organization_slug: 'acme' },
    { id: 'p2' }, // no org
    { id: 'p3', organization_id: null },
  ]
  const orgs = deriveOrganizationsFromProjects(projects)
  assert.equal(orgs.length, 1, 'org-less projects are skipped')
  assert.equal(orgs[0].slug, 'acme')
}

// ---- falls back to Organization <id> / org-<id> when name/slug missing ----
{
  const projects = [{ id: 'p1', organization_id: 7 }]
  const orgs = deriveOrganizationsFromProjects(projects)
  assert.equal(orgs.length, 1)
  assert.deepEqual(orgs[0], { id: 7, name: 'Organization 7', slug: 'org-7' })
}

// ---- empty / null input yields [] (no hard error) ----
{
  assert.deepEqual(deriveOrganizationsFromProjects([]), [])
  assert.deepEqual(deriveOrganizationsFromProjects(null), [])
  assert.deepEqual(deriveOrganizationsFromProjects(undefined), [])
}

console.log('derive-organizations.test.mjs ok')
