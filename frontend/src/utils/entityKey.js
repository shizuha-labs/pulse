const LEGAL_SUFFIX_WORDS = new Set([
  'company',
  'co',
  'private',
  'pvt',
  'limited',
  'ltd',
  'llp',
  'inc',
  'incorporated',
])

const KNOWN_PROJECT_ENTITIES = {
  BKSG: { key: 'SG', label: 'Shizuha Global' },
  BKST: { key: 'ST', label: 'Shizuha Trading' },
}

const EXPLICIT_KEY_FIELDS = [
  'entity_key',
  'organization_key',
  'org_key',
  'company_key',
  'mini_org_key',
]

function cleanKey(value) {
  if (!value) return ''
  return String(value)
    .trim()
    .replace(/[^a-z0-9]/gi, '')
    .toUpperCase()
    .slice(0, 4)
}

function humanizeSlug(slug) {
  if (!slug) return ''
  return String(slug)
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function initialsFromSlug(slug) {
  if (!slug) return ''
  const parts = String(slug)
    .toLowerCase()
    .split(/[-_\s]+/)
    .filter(Boolean)
    .filter((part) => !LEGAL_SUFFIX_WORDS.has(part))

  if (parts.length === 0) return ''
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return parts.slice(0, 2).map((part) => part[0]).join('').toUpperCase()
}

function explicitEntityKey(item) {
  for (const field of EXPLICIT_KEY_FIELDS) {
    const key = cleanKey(item?.[field])
    if (key) return key
  }

  const metadata = item?.metadata || item?.custom_fields || {}
  for (const field of EXPLICIT_KEY_FIELDS) {
    const key = cleanKey(metadata?.[field])
    if (key) return key
  }

  return ''
}

export function getEntityBadge(item = {}) {
  const explicitKey = explicitEntityKey(item)
  if (explicitKey) {
    return {
      key: explicitKey,
      label: item.organization_name || item.project_name || humanizeSlug(item.organization_slug),
      source: 'explicit',
    }
  }

  const projectKey = cleanKey(item.project_key || item.project?.key)
  if (KNOWN_PROJECT_ENTITIES[projectKey]) {
    return {
      ...KNOWN_PROJECT_ENTITIES[projectKey],
      source: 'project',
    }
  }

  const bksProjectSuffix = projectKey.match(/^BKS([A-Z0-9]{1,4})$/)?.[1]
  if (bksProjectSuffix) {
    return {
      key: bksProjectSuffix,
      label: item.project_name || projectKey,
      source: 'project',
    }
  }

  const itemProjectKey = cleanKey(String(item.item_key || '').split('-')[0])
  if (KNOWN_PROJECT_ENTITIES[itemProjectKey]) {
    return {
      ...KNOWN_PROJECT_ENTITIES[itemProjectKey],
      source: 'item_key',
    }
  }

  const bksItemSuffix = itemProjectKey.match(/^BKS([A-Z0-9]{1,4})$/)?.[1]
  if (bksItemSuffix) {
    return {
      key: bksItemSuffix,
      label: item.project_name || itemProjectKey,
      source: 'item_key',
    }
  }

  const slugKey = initialsFromSlug(item.organization_slug || item.organization?.slug)
  if (slugKey) {
    return {
      key: slugKey,
      label: item.organization_name || humanizeSlug(item.organization_slug || item.organization?.slug),
      source: 'organization',
    }
  }

  return null
}

export function getEntityBadgeTitle(item = {}, badge = getEntityBadge(item)) {
  if (!badge) return ''
  const details = [badge.label, item.project_key, item.organization_slug]
    .filter(Boolean)
    .filter((value, index, values) => values.indexOf(value) === index)
  return details.length ? details.join(' | ') : `Entity ${badge.key}`
}
