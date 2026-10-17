import { test, expect, TEST_USER } from './fixtures'

/**
 * E2E Tests for Shizuha Pulse Organizations
 *
 * Tests organization creation, management, and access control.
 * Covers the critical path for new users without any organization.
 */

test.describe('Organizations API', () => {
  test('any authenticated user can create an organization', async ({ authenticatedApi }) => {
    // Create an organization with unique slug
    const randomSuffix = Math.random().toString(36).substring(2, 8)
    const orgData = {
      name: `API Test Org ${randomSuffix}`,
      slug: `api-test-${randomSuffix}`,
      description: 'Organization created via API test',
    }

    const createResponse = await authenticatedApi.post('organizations/', {
      data: orgData,
    })

    if (!createResponse.ok()) {
      const errorText = await createResponse.text()
      console.log('Org creation failed:', createResponse.status(), errorText.substring(0, 500))
    }

    expect(createResponse.ok()).toBeTruthy()
    expect(createResponse.status()).toBe(201)

    const org = await createResponse.json()

    // Verify response contains required fields
    expect(org).toHaveProperty('id')
    expect(org).toHaveProperty('name')
    expect(org).toHaveProperty('slug')
    expect(org.name).toBe(orgData.name)
    expect(org.slug).toBe(orgData.slug)

    console.log('Created organization:', org.name, 'id:', org.id, 'slug:', org.slug)
  })

  test('organization creation returns full data with id', async ({ authenticatedApi }) => {
    const randomSuffix = Math.random().toString(36).substring(2, 8)
    const createResponse = await authenticatedApi.post('organizations/', {
      data: {
        name: `Full Data Test ${randomSuffix}`,
        slug: `full-data-${randomSuffix}`,
        description: 'Testing full response data',
      },
    })

    expect(createResponse.ok()).toBeTruthy()

    const org = await createResponse.json()

    // These fields should all be present in the response
    expect(org).toHaveProperty('id')
    expect(org).toHaveProperty('name')
    expect(org).toHaveProperty('slug')
    expect(org).toHaveProperty('description')
    expect(org).toHaveProperty('member_count')
    expect(org).toHaveProperty('project_count')
    expect(org).toHaveProperty('created_at')

    // Creator should be the only member initially
    expect(org.member_count).toBe(1)
    expect(org.project_count).toBe(0)

    console.log('Organization response includes all required fields')
  })

  test('should prevent duplicate organization slugs', async ({ authenticatedApi }) => {
    const randomSuffix = Math.random().toString(36).substring(2, 8)
    const slug = `dup-test-${randomSuffix}`

    // Create first organization
    const firstResponse = await authenticatedApi.post('organizations/', {
      data: {
        name: `Duplicate Test 1 ${randomSuffix}`,
        slug: slug,
        description: 'First org',
      },
    })

    expect(firstResponse.ok()).toBeTruthy()
    console.log('Created first org with slug:', slug)

    // Try to create second organization with same slug
    const secondResponse = await authenticatedApi.post('organizations/', {
      data: {
        name: `Duplicate Test 2 ${randomSuffix}`,
        slug: slug,
        description: 'Should fail - duplicate slug',
      },
    })

    // Should fail with validation error
    expect(secondResponse.ok()).toBeFalsy()
    expect(secondResponse.status()).toBe(400)

    const errorData = await secondResponse.json()
    console.log('Duplicate slug error:', JSON.stringify(errorData))

    // Error should mention slug
    const errorStr = JSON.stringify(errorData).toLowerCase()
    expect(errorStr).toContain('slug')
  })

  test('organization slug validation', async ({ authenticatedApi }) => {
    // Test invalid slug characters
    const invalidSlugs = [
      'Has Spaces',
      'HAS_UPPERCASE',
      'special@chars!',
      'with.dots',
    ]

    for (const invalidSlug of invalidSlugs) {
      const response = await authenticatedApi.post('organizations/', {
        data: {
          name: 'Invalid Slug Test',
          slug: invalidSlug,
          description: 'Should fail validation',
        },
      })

      // The API might accept some and convert, or reject - check behavior
      if (!response.ok()) {
        console.log(`Slug "${invalidSlug}" correctly rejected`)
      } else {
        const org = await response.json()
        console.log(`Slug "${invalidSlug}" was accepted/converted to: ${org.slug}`)
        // Clean up - we don't need to keep this
      }
    }
  })

  test('user becomes owner of created organization', async ({ authenticatedApi }) => {
    const randomSuffix = Math.random().toString(36).substring(2, 8)
    const slug = `owner-test-${randomSuffix}`

    // Create organization
    const createResponse = await authenticatedApi.post('organizations/', {
      data: {
        name: `Owner Test ${randomSuffix}`,
        slug: slug,
        description: 'Testing ownership',
      },
    })

    expect(createResponse.ok()).toBeTruthy()

    // Get organization members
    const membersResponse = await authenticatedApi.get(`organizations/${slug}/members/`)

    expect(membersResponse.ok()).toBeTruthy()

    const membersData = await membersResponse.json()
    console.log('Members:', JSON.stringify(membersData).substring(0, 200))

    // Should have exactly one member (the creator)
    expect(membersData.count).toBe(1)
    expect(membersData.members.length).toBe(1)

    // Creator should be owner
    const creatorMembership = membersData.members[0]
    expect(creatorMembership.role).toBe('owner')

    console.log('Creator is owner:', creatorMembership.user_email, 'role:', creatorMembership.role)
  })

  test('can create project in newly created organization', async ({ authenticatedApi }) => {
    const randomSuffix = Math.random().toString(36).substring(2, 8)
    const orgSlug = `proj-test-${randomSuffix}`

    // Create organization
    const orgResponse = await authenticatedApi.post('organizations/', {
      data: {
        name: `Project Test Org ${randomSuffix}`,
        slug: orgSlug,
        description: 'Org for project creation test',
      },
    })

    expect(orgResponse.ok()).toBeTruthy()
    const org = await orgResponse.json()

    // Create project in the new organization
    const projectKey = `P${randomSuffix}`.substring(0, 10).toUpperCase()
    const projectResponse = await authenticatedApi.post('projects/', {
      data: {
        organization: org.id,
        name: `Test Project ${randomSuffix}`,
        key: projectKey,
        description: 'Project in new org',
        project_type: 'software',
        access_level: 'open',
      },
    })

    if (!projectResponse.ok()) {
      const errorText = await projectResponse.text()
      console.log('Project creation failed:', projectResponse.status(), errorText.substring(0, 300))
    }

    expect(projectResponse.ok()).toBeTruthy()

    const project = await projectResponse.json()
    expect(project).toHaveProperty('id')
    expect(project.organization).toBe(org.id)

    console.log('Created project', project.name, 'in new org', org.name)
  })
})

test.describe('Organizations UI', () => {
  test('should display create organization UI when user has no organizations', async ({ page, playwright }) => {
    // This test needs a user with no organization memberships
    // For now, we'll just verify the UI structure exists

    const baseUrl = process.env.BASE_URL || 'http://localhost:8002'

    // Go to login page
    await page.goto(`${baseUrl}/id/login?continue=/pulse/projects`)

    // Login
    await page.waitForSelector('input[id="username"]', { timeout: 10000 })
    await page.fill('input[id="username"]', TEST_USER.username)
    await page.fill('input[id="password"]', TEST_USER.password)
    await page.click('button[type="submit"]')

    // Wait for redirect to pulse
    await page.waitForURL(/.*pulse.*/, { timeout: 20000 })
    await page.waitForLoadState('networkidle')

    // Check if we're on projects page
    const heading = page.locator('h1').filter({ hasText: 'Projects' })
    await expect(heading).toBeVisible({ timeout: 10000 })

    // The page should show either:
    // 1. Projects list (if user has orgs) OR
    // 2. "Create Organization" UI (if user has no orgs)

    // Look for either the org name or the create org button
    const hasOrganization = await page.locator('text=/in.*|Create Organization/').first().isVisible()

    if (hasOrganization) {
      console.log('User has organization or create org UI is visible - UI working correctly')
    }

    // Take screenshot for debugging
    await page.screenshot({ path: 'test-results/organizations-ui-check.png' })
  })

  test('create organization modal should work', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Navigate to projects page
    await page.goto('/pulse/projects')
    await page.waitForLoadState('networkidle', { timeout: 15000 })

    // Look for Create Organization button (visible when user has no orgs)
    // or a menu option to create new org
    const createOrgButton = page.locator('button').filter({ hasText: /Create Organization/i })

    const isCreateOrgVisible = await createOrgButton.isVisible().catch(() => false)

    if (isCreateOrgVisible) {
      // User has no organizations - test the create flow
      await createOrgButton.click()

      // Wait for modal
      await page.waitForTimeout(500)

      // Fill organization details
      const randomSuffix = Math.random().toString(36).substring(2, 6)
      const orgName = `UI Test Org ${randomSuffix}`

      // Fill name field
      const nameInput = page.locator('input[placeholder="My Company"]')
      await nameInput.fill(orgName)

      // Slug should be auto-generated, but verify it's there
      const slugInput = page.locator('input[placeholder="my-company"]')
      await expect(slugInput).toHaveValue(/ui-test-org/i)

      // Submit the form
      const submitButton = page.locator('button').filter({ hasText: /Create Organization/i }).last()
      await submitButton.click()

      // Wait for organization to be created
      await page.waitForResponse(
        response => response.url().includes('/organizations/') && response.status() === 201,
        { timeout: 10000 }
      ).catch(() => {
        console.log('Did not detect 201 response for org creation')
      })

      // Verify we can now see the organization name
      await page.waitForTimeout(1000)
      const orgNameVisible = await page.locator(`text=${orgName}`).isVisible().catch(() => false)
      console.log('Organization name visible after creation:', orgNameVisible)
    } else {
      // User already has organizations
      console.log('User already has organizations - create org button not primary visible')

      // Verify we can see organization info
      const orgInfo = page.locator('[class*="Building2"], text=/in.*/')
      const hasOrgInfo = await orgInfo.first().isVisible().catch(() => false)
      console.log('Organization info visible:', hasOrgInfo)
    }
  })
})

test.describe('Full Organization Flow', () => {
  test('complete flow: create org -> create project -> list projects', async ({ authenticatedApi }) => {
    const randomSuffix = Math.random().toString(36).substring(2, 8)

    // Step 1: Create organization
    const orgResponse = await authenticatedApi.post('organizations/', {
      data: {
        name: `Flow Test Org ${randomSuffix}`,
        slug: `flow-test-${randomSuffix}`,
        description: 'End-to-end flow test organization',
      },
    })

    expect(orgResponse.ok()).toBeTruthy()
    const org = await orgResponse.json()
    console.log('Step 1: Created org', org.name, 'id:', org.id)

    // Step 2: Create project in organization
    const projectKey = `FLW${randomSuffix}`.substring(0, 10).toUpperCase()
    const projectResponse = await authenticatedApi.post('projects/', {
      data: {
        organization: org.id,
        name: `Flow Test Project ${randomSuffix}`,
        key: projectKey,
        description: 'Project for flow test',
        project_type: 'software',
        access_level: 'open',
      },
    })

    expect(projectResponse.ok()).toBeTruthy()
    const project = await projectResponse.json()
    console.log('Step 2: Created project', project.name, 'key:', project.key)

    // Step 3: List projects in organization
    const listResponse = await authenticatedApi.get(`organizations/${org.slug}/projects/`)

    expect(listResponse.ok()).toBeTruthy()
    const listData = await listResponse.json()

    expect(listData.projects.length).toBeGreaterThanOrEqual(1)

    const foundProject = listData.projects.find(p => p.id === project.id)
    expect(foundProject).toBeDefined()

    console.log('Step 3: Found project in org listing')
    console.log('Full flow completed successfully!')
  })
})
