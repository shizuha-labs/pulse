import { test, expect, TEST_USER } from './fixtures.js'

/**
 * E2E tests for Phase 4: Calendar View & Issue Links
 *
 * Tests cover:
 * 1. Calendar View - Navigation, filtering, task display
 * 2. Issue Links - Create, list, delete links between items
 */

// Helper function to get or create an organization
async function getOrCreateOrganization(authenticatedApi) {
  const projectsResponse = await authenticatedApi.get('projects/')
  const projectsData = await projectsResponse.json()

  if (projectsData.results && projectsData.results.length > 0) {
    const firstProject = projectsData.results[0]
    if (firstProject.organization) {
      return { id: firstProject.organization, slug: firstProject.organization_slug }
    }
  }

  return null
}

// Helper function to create a project
async function createTestProject(authenticatedApi, namePrefix) {
  const org = await getOrCreateOrganization(authenticatedApi)
  const randomSuffix = Math.random().toString(36).substring(2, 6).toUpperCase()
  const projectKey = `${namePrefix.substring(0, 3).toUpperCase()}${randomSuffix}`.substring(0, 10)

  const projectData = {
    name: `${namePrefix}${Date.now()}`,
    key: projectKey,
    project_type: 'software',
    access_level: 'open'
  }

  if (org && org.id) {
    projectData.organization = org.id
  }

  const projectResponse = await authenticatedApi.post('projects/', {
    data: projectData
  })

  if (!projectResponse.ok()) {
    const errorText = await projectResponse.text()
    console.error(`Failed to create project: ${errorText}`)
  }

  return await projectResponse.json()
}

// Helper function to create a link between items
async function createLink(authenticatedApi, sourceItemId, targetItemId, linkType) {
  const linkResponse = await authenticatedApi.post(`items/${sourceItemId}/links/`, {
    data: {
      target_item_id: targetItemId,
      link_type: linkType
    }
  })
  return linkResponse
}

test.describe('Phase 4: Calendar View & Issue Links', () => {

  test.describe('Calendar View', () => {

    test('should navigate to calendar page', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Should show calendar header
      await expect(page.getByRole('heading', { name: /calendar/i })).toBeVisible({ timeout: 10000 })
    })

    test('should display month navigation controls', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Should show Previous/Next buttons
      await expect(page.locator('button[title="Previous month"]')).toBeVisible({ timeout: 5000 })
      await expect(page.locator('button[title="Next month"]')).toBeVisible({ timeout: 5000 })

      // Should show Today button
      await expect(page.getByRole('button', { name: /today/i })).toBeVisible()

      // Should show current month/year
      const monthYear = new Date().toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
      await expect(page.getByText(monthYear)).toBeVisible()
    })

    test('should navigate to previous month', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Get current month
      const currentDate = new Date()
      const currentMonthYear = currentDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
      await expect(page.getByText(currentMonthYear)).toBeVisible()

      // Click previous month
      await page.locator('button[title="Previous month"]').click()
      await page.waitForTimeout(300)

      // Should show previous month
      const prevDate = new Date(currentDate)
      prevDate.setMonth(prevDate.getMonth() - 1)
      const prevMonthYear = prevDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
      await expect(page.getByText(prevMonthYear)).toBeVisible()
    })

    test('should navigate to next month', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Get current month
      const currentDate = new Date()
      const currentMonthYear = currentDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
      await expect(page.getByText(currentMonthYear)).toBeVisible()

      // Click next month
      await page.locator('button[title="Next month"]').click()
      await page.waitForTimeout(300)

      // Should show next month
      const nextDate = new Date(currentDate)
      nextDate.setMonth(nextDate.getMonth() + 1)
      const nextMonthYear = nextDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
      await expect(page.getByText(nextMonthYear)).toBeVisible()
    })

    test('should return to today when clicking Today button', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Navigate away from current month
      await page.locator('button[title="Previous month"]').click()
      await page.waitForTimeout(300)
      await page.locator('button[title="Previous month"]').click()
      await page.waitForTimeout(300)

      // Click Today button
      await page.getByRole('button', { name: /today/i }).click()
      await page.waitForTimeout(300)

      // Should be back to current month
      const currentMonthYear = new Date().toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
      await expect(page.getByText(currentMonthYear)).toBeVisible()
    })

    test('should display filter controls', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Should show project filter
      await expect(page.locator('select').filter({ hasText: /all projects/i })).toBeVisible({ timeout: 5000 })

      // Should show priority filter
      await expect(page.locator('select').filter({ hasText: /all priorities/i })).toBeVisible()

      // Should show completed checkbox
      await expect(page.getByText(/show completed/i)).toBeVisible()
    })

    test('should display weekday headers', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Should show all weekday headers
      const weekdays = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
      for (const day of weekdays) {
        await expect(page.getByText(day, { exact: true })).toBeVisible()
      }
    })

    test('should display calendar grid', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Should show 7-column grid
      const grid = page.locator('.grid.grid-cols-7')
      await expect(grid.first()).toBeVisible({ timeout: 5000 })
    })

    test('should highlight current day', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Should highlight today with brand color
      const todayCell = page.locator('.bg-brand-600')
      await expect(todayCell).toBeVisible({ timeout: 5000 })
    })

    test('should display priority legend', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to calendar
      await page.goto('/pulse/calendar')
      await page.waitForLoadState('networkidle')

      // Should show priority legend (case-insensitive search for Priority: label)
      await expect(page.getByText('Priority:')).toBeVisible({ timeout: 5000 })
    })

    test('should show calendar in navigation sidebar', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to home
      await page.goto('/pulse/')
      await page.waitForLoadState('networkidle')

      // Should show Calendar link in sidebar navigation (use first() since both sidebar and mobile nav have it)
      const calendarLink = page.locator('a[href*="calendar"]').first()
      await expect(calendarLink).toBeVisible({ timeout: 10000 })
    })
  })

  test.describe('Issue Links', () => {

    test('should create an item link via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'LinkAPI')

      // Create two tasks
      const task1Response = await authenticatedApi.post('items/', {
        data: {
          title: 'Blocking Task',
          mode: 'task',
          project: project.id
        }
      })
      const task1 = await task1Response.json()

      const task2Response = await authenticatedApi.post('items/', {
        data: {
          title: 'Blocked Task',
          mode: 'task',
          project: project.id
        }
      })
      const task2 = await task2Response.json()

      // Create link via item's links endpoint
      const linkResponse = await createLink(authenticatedApi, task1.id, task2.id, 'blocks')
      expect(linkResponse.ok()).toBeTruthy()

      const link = await linkResponse.json()
      expect(link.link_type).toBe('blocks')
      expect(link.source_item).toBe(task1.id)
      expect(link.target_item).toBe(task2.id)
    })

    test('should list links for an item via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'LinkList')

      // Create tasks
      const task1Response = await authenticatedApi.post('items/', {
        data: {
          title: 'Source Task',
          mode: 'task',
          project: project.id
        }
      })
      const task1 = await task1Response.json()

      const task2Response = await authenticatedApi.post('items/', {
        data: {
          title: 'Related Task',
          mode: 'task',
          project: project.id
        }
      })
      const task2 = await task2Response.json()

      // Create link
      await createLink(authenticatedApi, task1.id, task2.id, 'relates_to')

      // List links via item's links endpoint
      const listResponse = await authenticatedApi.get(`items/${task1.id}/links/`)
      expect(listResponse.ok()).toBeTruthy()

      const data = await listResponse.json()
      expect(data.outward_links.length).toBeGreaterThanOrEqual(1)
    })

    test('should delete an item link via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'LinkDel')

      // Create tasks
      const task1Response = await authenticatedApi.post('items/', {
        data: {
          title: 'Task A',
          mode: 'task',
          project: project.id
        }
      })
      const task1 = await task1Response.json()

      const task2Response = await authenticatedApi.post('items/', {
        data: {
          title: 'Task B',
          mode: 'task',
          project: project.id
        }
      })
      const task2 = await task2Response.json()

      // Create link
      const linkResponse = await createLink(authenticatedApi, task1.id, task2.id, 'duplicates')
      const link = await linkResponse.json()

      // Delete link via item's delete_link action
      const deleteResponse = await authenticatedApi.delete(`items/${task1.id}/links/${link.id}/`)
      expect(deleteResponse.status()).toBe(204)

      // Verify link is deleted
      const listResponse = await authenticatedApi.get(`items/${task1.id}/links/`)
      const data = await listResponse.json()
      const deletedLink = data.outward_links.find(l => l.id === link.id)
      expect(deletedLink).toBeUndefined()
    })

    test('should support different link types', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'LinkTypes')

      // Create tasks
      const parentResponse = await authenticatedApi.post('items/', {
        data: { title: 'Parent Task', mode: 'task', project: project.id }
      })
      const parent = await parentResponse.json()

      const childResponse = await authenticatedApi.post('items/', {
        data: { title: 'Child Task', mode: 'task', project: project.id }
      })
      const child = await childResponse.json()

      // Test parent_of link type
      const linkResponse = await createLink(authenticatedApi, parent.id, child.id, 'parent_of')
      expect(linkResponse.ok()).toBeTruthy()

      const link = await linkResponse.json()
      expect(link.link_type).toBe('parent_of')
    })

    test('should show Links tab on task detail', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create project and task
      const project = await createTestProject(authenticatedApi, 'LinkUI')
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task with Links Tab',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Should show Links tab button (the button contains icon + text "Links")
      const linksTab = page.locator('button').filter({ hasText: 'Links' }).first()
      await expect(linksTab).toBeVisible({ timeout: 10000 })
    })

    test('should show Link Issue button when Links tab is active', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create project and task
      const project = await createTestProject(authenticatedApi, 'LinkBtn')
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task with Link Button',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Click the Links tab
      const linksTab = page.locator('button').filter({ hasText: 'Links' }).first()
      await linksTab.click()
      await page.waitForTimeout(500)

      // Should show Link Issue button
      const linkButton = page.locator('button').filter({ hasText: /link issue/i })
      await expect(linkButton).toBeVisible({ timeout: 10000 })
    })

    test('should display linked issues on task detail', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create project
      const project = await createTestProject(authenticatedApi, 'LinkDisp')

      // Create tasks
      const task1Response = await authenticatedApi.post('items/', {
        data: { title: 'Main Task', mode: 'task', project: project.id }
      })
      const task1 = await task1Response.json()

      const task2Response = await authenticatedApi.post('items/', {
        data: { title: 'Linked Related Task', mode: 'task', project: project.id }
      })
      const task2 = await task2Response.json()

      // Create link
      await createLink(authenticatedApi, task1.id, task2.id, 'relates_to')

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task1.item_key || task1.id}`)
      await page.waitForLoadState('networkidle')

      // Click the Links tab
      const linksTab = page.locator('button').filter({ hasText: 'Links' }).first()
      await linksTab.click()
      await page.waitForTimeout(500)

      // Should show the linked task
      await expect(page.getByText('Linked Related Task')).toBeVisible({ timeout: 10000 })
    })
  })

  test.describe('Integration Tests', () => {

    test('should fetch tasks for calendar date range via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'CalRange')

      // Create task with due date
      const today = new Date().toISOString().split('T')[0]
      await authenticatedApi.post('items/', {
        data: {
          title: 'Task in Range',
          mode: 'task',
          project: project.id,
          due_date: today
        }
      })

      // Calculate date range (first and last of current month)
      const startOfMonth = new Date()
      startOfMonth.setDate(1)
      const endOfMonth = new Date(startOfMonth)
      endOfMonth.setMonth(endOfMonth.getMonth() + 1)
      endOfMonth.setDate(0)

      const dueAfter = startOfMonth.toISOString().split('T')[0]
      const dueBefore = endOfMonth.toISOString().split('T')[0]

      // Query items with date range
      const queryResponse = await authenticatedApi.get(
        `items/?mode=task&due_after=${dueAfter}&due_before=${dueBefore}&page_size=100`
      )
      expect(queryResponse.ok()).toBeTruthy()

      const data = await queryResponse.json()
      const items = data.results || data
      // Should include our task
      const found = items.find(item => item.title === 'Task in Range')
      expect(found).toBeDefined()
    })

    test('should support bidirectional link relationships', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'LinkBi')

      // Create tasks
      const task1Response = await authenticatedApi.post('items/', {
        data: { title: 'Blocker', mode: 'task', project: project.id }
      })
      const task1 = await task1Response.json()

      const task2Response = await authenticatedApi.post('items/', {
        data: { title: 'Blocked', mode: 'task', project: project.id }
      })
      const task2 = await task2Response.json()

      // Create blocks link from task1 to task2
      await createLink(authenticatedApi, task1.id, task2.id, 'blocks')

      // Check links from task1 perspective (outgoing)
      const task1LinksResponse = await authenticatedApi.get(`items/${task1.id}/links/`)
      const task1Data = await task1LinksResponse.json()
      const outgoingLink = task1Data.outward_links.find(l =>
        l.source_item === task1.id && l.target_item === task2.id
      )
      expect(outgoingLink).toBeDefined()
      expect(outgoingLink.link_type).toBe('blocks')

      // Check links from task2 perspective (incoming)
      const task2LinksResponse = await authenticatedApi.get(`items/${task2.id}/links/`)
      const task2Data = await task2LinksResponse.json()
      const incomingLink = task2Data.inward_links.find(l =>
        l.source_item === task1.id && l.target_item === task2.id
      )
      expect(incomingLink).toBeDefined()
    })
  })
})
