import assert from 'node:assert/strict'
import {
  buildTeamScope,
  formatOrganizationName,
  formatScopedTeamName,
  getTeamRoutingKey,
  normalizeTeamOption,
} from '../../src/utils/teamLabels.js'

const personalAccounting = normalizeTeamOption({
  canonical_name: 'accounting',
  canonical_key: 'personal-3:accounting',
  name: 'Accounting',
  organization_id: 4,
  organization_slug: 'personal-3',
  organization_name: "Hritik's Workspace",
})

assert.equal(getTeamRoutingKey(personalAccounting), 'accounting')
assert.equal(personalAccounting.canonical_key, 'personal-3:accounting')
assert.equal(formatScopedTeamName(personalAccounting), "Hritik's Workspace › Accounting")
assert.equal(formatOrganizationName({ organization_slug: 'shizuha-digital' }), 'Shizuha Digital')
assert.equal(formatOrganizationName({ organization_id: 4 }), 'Organization #4')

const taskFallback = normalizeTeamOption(
  { key: 'accounting', name: 'Accounting' },
  {
    organization_id: 2,
    organization_slug: 'shizuha-digital',
    organization_name: 'Shizuha Digital LLP',
  },
)
assert.equal(taskFallback.team_key, 'accounting')
assert.equal(taskFallback.organization_id, 2)
assert.equal(formatScopedTeamName(taskFallback), 'Shizuha Digital LLP › Accounting')

const sharedPlatformTeam = normalizeTeamOption(
  {
    canonical_name: 'review',
    organization_slug: 'shizuha',
  },
  {
    organization_id: 4,
    organization_slug: 'personal-3',
    organization_name: "Hritik's Workspace",
  },
)
assert.equal(formatScopedTeamName(sharedPlatformTeam), 'Shizuha › Review')

const personalScope = buildTeamScope({
  organizationId: 4,
  organizationSlug: 'personal-3',
  projectId: 357,
})
const globalScope = buildTeamScope()
assert.equal(personalScope.cacheKey, 'id:4')
assert.deepEqual(personalScope.params, {
  project_id: 357,
})
assert.equal(personalScope.isScoped, true)
assert.equal(globalScope.cacheKey, 'all')
assert.equal(globalScope.isScoped, false)

console.log('teamLabels tests passed')
