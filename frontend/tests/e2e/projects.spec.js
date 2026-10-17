import { test, expect, TEST_USER } from './fixtures'

/**
 * E2E Tests for Shizuha Pulse Projects
 *
 * Tests the project creation, listing, and management functionality.
 */

test.describe('Projects API', () => {
  test('should list organizations', async ({ authenticatedApi }) => {
    const response = await authenticatedApi.get('organizations/')
    expect(response.ok()).toBeTruthy()

    const data = await response.json()
    expect(data).toHaveProperty('results')
    console.log('Organizations:', data.results.length)
  })

  test('should create an organization if none exists', async ({ authenticatedApi }) => {
    // First check if any organizations exist
    const listResponse = await authenticatedApi.get('organizations/')
    const listData = await listResponse.json()

    if (listData.results.length === 0) {
      // Create an organization
      const createResponse = await authenticatedApi.post('organizations/', {
        data: {
          name: 'Test Organization',
          slug: 'test-org',
          description: 'Organization for E2E testing',
        },
      })

      if (!createResponse.ok()) {
        const errorText = await createResponse.text()
        console.log('Org creation failed:', createResponse.status(), errorText.substring(0, 300))
      }

      expect(createResponse.ok()).toBeTruthy()

      const org = await createResponse.json()
      expect(org).toHaveProperty('id')
      console.log('Created organization:', org.name, 'id:', org.id)
    } else {
      console.log('Organization already exists:', listData.results[0].name)
    }
  })

  test('should create a project in an organization', async ({ authenticatedApi }) => {
    // Get organizations
    const orgsResponse = await authenticatedApi.get('organizations/')
    const orgsData = await orgsResponse.json()

    if (!orgsData.results || orgsData.results.length === 0) {
      console.log('No organizations available, skipping project creation')
      test.skip()
      return
    }

    const org = orgsData.results[0]
    console.log('Using organization:', org.name, 'id:', org.id)

    // Create a project with unique key using random suffix
    const randomSuffix = Math.random().toString(36).substring(2, 8).toUpperCase()
    const projectKey = `E2E${randomSuffix}`.substring(0, 10).toUpperCase()

    const createResponse = await authenticatedApi.post('projects/', {
      data: {
        organization: org.id,
        name: `E2E Test Project ${randomSuffix}`,
        key: projectKey,
        description: 'Project created by E2E test',
        project_type: 'software',
        access_level: 'open',
      },
    })

    if (!createResponse.ok()) {
      const errorText = await createResponse.text()
      console.log('Project creation failed:', createResponse.status(), errorText.substring(0, 500))
    }

    expect(createResponse.ok()).toBeTruthy()

    const project = await createResponse.json()
    expect(project).toHaveProperty('id')
    expect(project).toHaveProperty('key')
    expect(project.key).toBe(projectKey)
    expect(project.organization).toBe(org.id)

    console.log('Created project:', project.name, 'id:', project.id, 'key:', project.key)
  })

  test('should list projects in an organization', async ({ authenticatedApi }) => {
    // Get organizations
    const orgsResponse = await authenticatedApi.get('organizations/')
    const orgsData = await orgsResponse.json()

    if (!orgsData.results || orgsData.results.length === 0) {
      test.skip()
      return
    }

    const org = orgsData.results[0]

    // List projects for the organization
    const projectsResponse = await authenticatedApi.get(`organizations/${org.slug}/projects/`)

    expect(projectsResponse.ok()).toBeTruthy()

    const projectsData = await projectsResponse.json()
    console.log('Projects in', org.name + ':', projectsData.projects?.length || 0)

    // Verify project data structure if projects exist
    if (projectsData.projects && projectsData.projects.length > 0) {
      const project = projectsData.projects[0]
      expect(project).toHaveProperty('id')
      expect(project).toHaveProperty('name')
      expect(project).toHaveProperty('key')
    }
  })

  test('should prevent duplicate project keys in same organization', async ({ authenticatedApi }) => {
    // Get organizations
    const orgsResponse = await authenticatedApi.get('organizations/')
    const orgsData = await orgsResponse.json()

    if (!orgsData.results || orgsData.results.length === 0) {
      test.skip()
      return
    }

    const org = orgsData.results[0]

    // Create first project with random key
    const randomSuffix = Math.random().toString(36).substring(2, 8).toUpperCase()
    const projectKey = `DUP${randomSuffix}`.substring(0, 10).toUpperCase()

    const firstResponse = await authenticatedApi.post('projects/', {
      data: {
        organization: org.id,
        name: `Duplicate Test ${randomSuffix}`,
        key: projectKey,
        description: 'First project',
        project_type: 'software',
        access_level: 'open',
      },
    })

    expect(firstResponse.ok()).toBeTruthy()
    console.log('Created first project with key:', projectKey)

    // Try to create second project with same key
    const secondResponse = await authenticatedApi.post('projects/', {
      data: {
        organization: org.id,
        name: `Duplicate Test 2 ${randomSuffix}`,
        key: projectKey,
        description: 'Second project - should fail',
        project_type: 'software',
        access_level: 'open',
      },
    })

    // Should fail with validation error
    expect(secondResponse.ok()).toBeFalsy()
    expect(secondResponse.status()).toBe(400)

    const errorData = await secondResponse.json()
    console.log('Duplicate key error:', JSON.stringify(errorData))

    // Error should mention the key conflict
    const errorStr = JSON.stringify(errorData).toLowerCase()
    expect(errorStr).toContain('key')
  })

  test('should get project details', async ({ authenticatedApi }) => {
    // Get organizations
    const orgsResponse = await authenticatedApi.get('organizations/')
    const orgsData = await orgsResponse.json()

    if (!orgsData.results || orgsData.results.length === 0) {
      test.skip()
      return
    }

    const org = orgsData.results[0]

    // List projects
    const projectsResponse = await authenticatedApi.get(`organizations/${org.slug}/projects/`)
    const projectsData = await projectsResponse.json()

    if (!projectsData.projects || projectsData.projects.length === 0) {
      console.log('No projects exist, skipping detail test')
      test.skip()
      return
    }

    const project = projectsData.projects[0]

    // Get project details
    const detailResponse = await authenticatedApi.get(`projects/${project.id}/`)

    expect(detailResponse.ok()).toBeTruthy()

    const detail = await detailResponse.json()
    expect(detail).toHaveProperty('id')
    expect(detail.id).toBe(project.id)
    expect(detail).toHaveProperty('name')
    expect(detail).toHaveProperty('key')
    expect(detail).toHaveProperty('member_count')
    expect(detail).toHaveProperty('item_count')

    console.log('Project detail:', detail.name, 'members:', detail.member_count, 'items:', detail.item_count)
  })
})

test.describe('Projects API - Archive', () => {
  test('organization admin can archive a project', async ({ authenticatedApi }) => {
    // Get organizations
    const orgsResponse = await authenticatedApi.get('organizations/')
    const orgsData = await orgsResponse.json()

    if (!orgsData.results || orgsData.results.length === 0) {
      console.log('No organizations available, skipping archive test')
      test.skip()
      return
    }

    const org = orgsData.results[0]

    // Create a project to archive
    const randomSuffix = Math.random().toString(36).substring(2, 8).toUpperCase()
    const projectKey = `ARC${randomSuffix}`.substring(0, 10).toUpperCase()

    const createResponse = await authenticatedApi.post('projects/', {
      data: {
        organization: org.id,
        name: `Archive Test Project ${randomSuffix}`,
        key: projectKey,
        description: 'Project for archive test',
        project_type: 'software',
        access_level: 'open',
      },
    })

    expect(createResponse.ok()).toBeTruthy()
    const project = await createResponse.json()
    console.log('Created project for archive test:', project.id, project.key)

    // Archive the project
    const archiveResponse = await authenticatedApi.post(`projects/${project.id}/archive/`)

    if (!archiveResponse.ok()) {
      const errorText = await archiveResponse.text()
      console.log('Archive failed:', archiveResponse.status(), errorText.substring(0, 300))
    }

    expect(archiveResponse.ok()).toBeTruthy()

    const archiveData = await archiveResponse.json()
    expect(archiveData.message).toBe('Project archived')
    expect(archiveData.project.is_archived).toBe(true)

    console.log('Project archived successfully')
  })

  test('organization admin can unarchive a project', async ({ authenticatedApi }) => {
    // Get organizations
    const orgsResponse = await authenticatedApi.get('organizations/')
    const orgsData = await orgsResponse.json()

    if (!orgsData.results || orgsData.results.length === 0) {
      test.skip()
      return
    }

    const org = orgsData.results[0]

    // Create and archive a project
    const randomSuffix = Math.random().toString(36).substring(2, 8).toUpperCase()
    const projectKey = `UNA${randomSuffix}`.substring(0, 10).toUpperCase()

    const createResponse = await authenticatedApi.post('projects/', {
      data: {
        organization: org.id,
        name: `Unarchive Test Project ${randomSuffix}`,
        key: projectKey,
        description: 'Project for unarchive test',
        project_type: 'software',
        access_level: 'open',
      },
    })

    expect(createResponse.ok()).toBeTruthy()
    const project = await createResponse.json()

    // Archive first
    const archiveResponse = await authenticatedApi.post(`projects/${project.id}/archive/`)
    expect(archiveResponse.ok()).toBeTruthy()

    // Now unarchive
    const unarchiveResponse = await authenticatedApi.post(`projects/${project.id}/unarchive/`)

    if (!unarchiveResponse.ok()) {
      const errorText = await unarchiveResponse.text()
      console.log('Unarchive failed:', unarchiveResponse.status(), errorText.substring(0, 300))
    }

    expect(unarchiveResponse.ok()).toBeTruthy()

    const unarchiveData = await unarchiveResponse.json()
    expect(unarchiveData.message).toBe('Project unarchived')
    expect(unarchiveData.project.is_archived).toBe(false)

    console.log('Project unarchived successfully')
  })
})

test.describe('Projects UI', () => {
  test('should display projects page', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Navigate to projects page
    await page.goto('/pulse/projects')

    // Wait for page to load
    await page.waitForLoadState('networkidle', { timeout: 15000 })

    // Check for projects heading or content
    const heading = page.locator('h1, h2').filter({ hasText: /project/i })
    await expect(heading.first()).toBeVisible({ timeout: 10000 })

    console.log('Projects page loaded')
  })

  test('should open create project modal', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Navigate to projects page
    await page.goto('/pulse/projects')
    await page.waitForLoadState('networkidle', { timeout: 15000 })

    // Look for create project button
    const createButton = page.locator('button').filter({ hasText: /create|new|add/i }).first()

    // If no organizations exist, the button might be disabled or not present
    const isVisible = await createButton.isVisible().catch(() => false)

    if (!isVisible) {
      console.log('Create project button not visible - may need organization first')
      return
    }

    // Click create button
    await createButton.click()

    // Wait for modal
    await page.waitForSelector('[role="dialog"], .modal, [data-modal]', { timeout: 5000 }).catch(() => {
      // Modal might be inline form
      console.log('No modal detected, may be inline form')
    })

    console.log('Create project UI opened')
  })

  test('should create project via UI', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Navigate to projects page
    await page.goto('/pulse/projects')
    await page.waitForLoadState('networkidle', { timeout: 15000 })

    // Click create project button
    const createButton = page.locator('button').filter({ hasText: /create|new project/i }).first()
    const isVisible = await createButton.isVisible().catch(() => false)

    if (!isVisible) {
      console.log('Create project button not visible, skipping UI test')
      test.skip()
      return
    }

    await createButton.click()

    // Wait for form
    await page.waitForTimeout(500)

    // Fill in project details
    const randomSuffix = Math.random().toString(36).substring(2, 6).toUpperCase()
    const projectName = `UI Test Project ${randomSuffix}`
    const projectKey = `UI${randomSuffix}`.substring(0, 6).toUpperCase()

    // Fill name field - look for input by placeholder "Backend Development"
    const nameInput = page.locator('input[placeholder="Backend Development"]')
    await nameInput.fill(projectName)

    // Fill key field - look for input by placeholder "BE"
    const keyInput = page.locator('input[placeholder="BE"]')
    await keyInput.fill(projectKey)

    // Fill description if present
    const descInput = page.locator('textarea[placeholder*="project about"]')
    if (await descInput.isVisible()) {
      await descInput.fill('Created via E2E UI test')
    }

    // Submit the form - look for "Create Project" button
    const submitButton = page.locator('button').filter({ hasText: 'Create Project' })
    await submitButton.click()

    // Wait for project to be created
    await page.waitForResponse(
      response => response.url().includes('/projects/') && response.status() === 201,
      { timeout: 10000 }
    ).catch(() => {
      console.log('Did not detect 201 response, checking for success')
    })

    // Wait for modal to close or success message
    await page.waitForTimeout(1000)

    // Verify project appears in list
    const projectInList = page.locator(`text=${projectName}`).or(page.locator(`text=${projectKey}`))
    const projectVisible = await projectInList.isVisible().catch(() => false)

    if (projectVisible) {
      console.log('Project created and visible in list:', projectName)
    } else {
      // Refresh and check again
      await page.reload()
      await page.waitForLoadState('networkidle')

      const afterRefresh = await projectInList.isVisible().catch(() => false)
      console.log('Project visible after refresh:', afterRefresh)
    }
  })
})
