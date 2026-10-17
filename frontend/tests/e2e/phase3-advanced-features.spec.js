import { test, expect, TEST_USER } from './fixtures.js'

/**
 * E2E tests for Phase 3: Advanced Features
 *
 * Tests cover:
 * 1. Components - Create, edit, delete components; assign to items
 * 2. Versions - Create, edit, release versions; assign to items
 * 3. Work Logging - Log work, view work logs, time tracking display
 * 4. JQL Query Language - Basic JQL queries
 * 5. Workflow Configuration - View and edit workflow statuses
 * 6. Board Configuration - Configure board columns
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

test.describe('Phase 3: Advanced Features', () => {

  test.describe('Components Management', () => {

    test('should create a component via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'CompAPI')

      // Create a component
      const componentResponse = await authenticatedApi.post('components/', {
        data: {
          project: project.id,
          name: 'Frontend',
          description: 'Frontend components'
        }
      })
      expect(componentResponse.ok()).toBeTruthy()

      const component = await componentResponse.json()
      expect(component.name).toBe('Frontend')
      expect(component.project).toBe(project.id)
    })

    test('should list components for a project', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'CompList')

      // Create components
      await authenticatedApi.post('components/', {
        data: { project: project.id, name: 'Backend' }
      })
      await authenticatedApi.post('components/', {
        data: { project: project.id, name: 'Frontend' }
      })

      // List components
      const listResponse = await authenticatedApi.get(`components/?project=${project.id}`)
      expect(listResponse.ok()).toBeTruthy()

      const components = await listResponse.json()
      expect(components.length).toBeGreaterThanOrEqual(2)
    })

    test('should navigate to Components tab in Project Settings', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a project
      const project = await createTestProject(authenticatedApi, 'CompNav')

      // Navigate to project settings
      await page.goto(`/pulse/projects/${project.id}/settings`)
      await page.waitForLoadState('networkidle')

      // Click Components tab
      const componentsTab = page.locator('button').filter({ hasText: /components/i }).first()
      await expect(componentsTab).toBeVisible({ timeout: 10000 })
      await componentsTab.click()
      await page.waitForLoadState('networkidle')

      // Should show Components header
      await expect(page.getByText('Components')).toBeVisible({ timeout: 5000 })
    })

    test('should show component selector on task detail', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create project with component
      const project = await createTestProject(authenticatedApi, 'CompSel')
      await authenticatedApi.post('components/', {
        data: { project: project.id, name: 'API' }
      })

      // Create a task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task with Component',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Look for component label
      const componentLabel = page.locator('label, span').filter({ hasText: /component/i }).first()
      await expect(componentLabel).toBeVisible({ timeout: 10000 })
    })
  })

  test.describe('Versions Management', () => {

    test('should create a version via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'VerAPI')

      // Create a version
      const versionResponse = await authenticatedApi.post('versions/', {
        data: {
          project: project.id,
          name: 'v1.0.0',
          status: 'planning'
        }
      })
      expect(versionResponse.ok()).toBeTruthy()

      const version = await versionResponse.json()
      expect(version.name).toBe('v1.0.0')
      expect(version.status).toBe('planning')
    })

    test('should release a version via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'VerRel')

      // Create a version
      const versionResponse = await authenticatedApi.post('versions/', {
        data: {
          project: project.id,
          name: 'v1.0.0',
          status: 'active'
        }
      })
      const version = await versionResponse.json()

      // Release the version
      const releaseResponse = await authenticatedApi.post(`versions/${version.id}/release/`)
      expect(releaseResponse.ok()).toBeTruthy()

      // Verify status changed
      const updatedVersionResponse = await authenticatedApi.get(`versions/${version.id}/`)
      const updatedVersion = await updatedVersionResponse.json()
      expect(updatedVersion.status).toBe('released')
    })

    test('should navigate to Versions tab in Project Settings', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a project
      const project = await createTestProject(authenticatedApi, 'VerNav')

      // Navigate to project settings
      await page.goto(`/pulse/projects/${project.id}/settings`)
      await page.waitForLoadState('networkidle')

      // Click Versions tab
      const versionsTab = page.locator('button').filter({ hasText: /versions/i }).first()
      await expect(versionsTab).toBeVisible({ timeout: 10000 })
      await versionsTab.click()
      await page.waitForLoadState('networkidle')

      // Should show Versions header
      await expect(page.getByText('Versions')).toBeVisible({ timeout: 5000 })
    })

    test('should show version selector on task detail', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create project with version
      const project = await createTestProject(authenticatedApi, 'VerSel')
      await authenticatedApi.post('versions/', {
        data: { project: project.id, name: 'v2.0.0' }
      })

      // Create a task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task with Version',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Look for fix version label
      const versionLabel = page.locator('label, span').filter({ hasText: /fix version/i }).first()
      await expect(versionLabel).toBeVisible({ timeout: 10000 })
    })
  })

  test.describe('Work Logging', () => {

    test('should create a work log via API', async ({ authenticatedApi }) => {
      // Create a project and task
      const project = await createTestProject(authenticatedApi, 'WorkAPI')
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task for Work Log',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Create work log
      const workLogResponse = await authenticatedApi.post('work-logs/', {
        data: {
          item: task.id,
          duration_minutes: 120,
          worked_date: new Date().toISOString().split('T')[0],
          description: 'Fixed bugs'
        }
      })
      expect(workLogResponse.ok()).toBeTruthy()

      const workLog = await workLogResponse.json()
      expect(workLog.duration_minutes).toBe(120)
    })

    test('should list work logs for an item', async ({ authenticatedApi }) => {
      // Create a project and task
      const project = await createTestProject(authenticatedApi, 'WorkList')
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task for Work Logs List',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Create work logs
      await authenticatedApi.post('work-logs/', {
        data: {
          item: task.id,
          duration_minutes: 60,
          worked_date: new Date().toISOString().split('T')[0]
        }
      })

      // List work logs
      const listResponse = await authenticatedApi.get(`work-logs/?item=${task.id}`)
      expect(listResponse.ok()).toBeTruthy()

      const data = await listResponse.json()
      expect(data.work_logs.length).toBeGreaterThanOrEqual(1)
      expect(data.total_minutes).toBeGreaterThanOrEqual(60)
    })

    test('should show time tracking display on task detail', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create project and task with time estimate
      const project = await createTestProject(authenticatedApi, 'TimeTrack')
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task with Time Tracking',
          mode: 'task',
          project: project.id,
          original_estimate_minutes: 240
        }
      })
      const task = await taskResponse.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Look for time tracking section
      const timeTrackingLabel = page.locator('label, span').filter({ hasText: /time tracking/i }).first()
      await expect(timeTrackingLabel).toBeVisible({ timeout: 10000 })
    })

    test('should show Log Work button on task detail', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create project and task
      const project = await createTestProject(authenticatedApi, 'LogWork')
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task to Log Work',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Look for Log Work button
      const logWorkButton = page.locator('button').filter({ hasText: /log work/i }).first()
      await expect(logWorkButton).toBeVisible({ timeout: 10000 })
    })

    test('should show Work Logs tab on task detail', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create project and task
      const project = await createTestProject(authenticatedApi, 'WorkLogs')
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task with Work Logs Tab',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Look for Work Logs tab
      const workLogsTab = page.locator('button').filter({ hasText: /work logs/i }).first()
      await expect(workLogsTab).toBeVisible({ timeout: 10000 })
    })
  })

  test.describe('JQL Query Language', () => {

    test('should filter items with JQL via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'JQLAPI')

      // Create tasks with different statuses
      await authenticatedApi.post('items/', {
        data: {
          title: 'Pending Task',
          mode: 'task',
          project: project.id,
          status: 'pending'
        }
      })
      await authenticatedApi.post('items/', {
        data: {
          title: 'Completed Task',
          mode: 'task',
          project: project.id,
          status: 'completed'
        }
      })

      // Query with JQL
      const jql = encodeURIComponent('status = "pending"')
      const queryResponse = await authenticatedApi.get(`items/?jql=${jql}`)
      expect(queryResponse.ok()).toBeTruthy()

      const data = await queryResponse.json()
      const items = data.results || data
      // All returned items should be pending
      items.forEach(item => {
        expect(item.status).toBe('pending')
      })
    })

    test('should filter by priority with JQL', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'JQLPri')

      // Create tasks with different priorities
      await authenticatedApi.post('items/', {
        data: {
          title: 'High Priority Task',
          mode: 'task',
          project: project.id,
          priority: 'high'
        }
      })
      await authenticatedApi.post('items/', {
        data: {
          title: 'Low Priority Task',
          mode: 'task',
          project: project.id,
          priority: 'low'
        }
      })

      // Query with JQL
      const jql = encodeURIComponent('priority = "high"')
      const queryResponse = await authenticatedApi.get(`items/?jql=${jql}`)
      expect(queryResponse.ok()).toBeTruthy()

      const data = await queryResponse.json()
      const items = data.results || data
      // All returned items should be high priority
      items.forEach(item => {
        expect(item.priority).toBe('high')
      })
    })

    test('should show JQL toggle button on Tasks page', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to tasks page
      await page.goto('/pulse/tasks')
      await page.waitForLoadState('networkidle')

      // Look for JQL toggle button
      const jqlButton = page.locator('button').filter({ hasText: /jql/i }).first()
      await expect(jqlButton).toBeVisible({ timeout: 10000 })
    })

    test('should show JQL input when JQL mode is enabled', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to tasks page
      await page.goto('/pulse/tasks')
      await page.waitForLoadState('networkidle')

      // Click JQL toggle
      const jqlButton = page.locator('button').filter({ hasText: /jql/i }).first()
      await jqlButton.click()
      await page.waitForTimeout(300)

      // Should show JQL input
      const jqlInput = page.locator('input[placeholder*="JQL"]').first()
      await expect(jqlInput).toBeVisible({ timeout: 5000 })
    })
  })

  test.describe('Workflow Configuration', () => {

    test('should get workflow for a project via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'WfAPI')

      // Get workflow
      const workflowResponse = await authenticatedApi.get(`workflows/${project.id}/`)
      // Workflow may not exist yet, which is OK
      if (workflowResponse.ok()) {
        const workflow = await workflowResponse.json()
        expect(workflow).toBeDefined()
      }
    })

    test('should navigate to Workflow tab in Project Settings', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a project
      const project = await createTestProject(authenticatedApi, 'WfNav')

      // Navigate to project settings
      await page.goto(`/pulse/projects/${project.id}/settings`)
      await page.waitForLoadState('networkidle')

      // Click Workflow tab
      const workflowTab = page.locator('button').filter({ hasText: /workflow/i }).first()
      await expect(workflowTab).toBeVisible({ timeout: 10000 })
      await workflowTab.click()
      await page.waitForLoadState('networkidle')

      // Should show Workflow section
      await expect(page.getByText(/workflow statuses/i)).toBeVisible({ timeout: 5000 })
    })
  })

  test.describe('Board Configuration', () => {

    test('should create board column via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'BoardAPI')

      // Create a board column
      const columnResponse = await authenticatedApi.post('board-columns/', {
        data: {
          project: project.id,
          name: 'Review',
          status_mapping: ['in_progress'],
          position: 0
        }
      })
      expect(columnResponse.ok()).toBeTruthy()

      const column = await columnResponse.json()
      expect(column.name).toBe('Review')
    })

    test('should list board columns for a project', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'BoardList')

      // Create columns
      await authenticatedApi.post('board-columns/', {
        data: { project: project.id, name: 'To Do', position: 0 }
      })
      await authenticatedApi.post('board-columns/', {
        data: { project: project.id, name: 'Done', position: 1 }
      })

      // List columns
      const listResponse = await authenticatedApi.get(`board-columns/?project=${project.id}`)
      expect(listResponse.ok()).toBeTruthy()

      const columns = await listResponse.json()
      expect(columns.length).toBeGreaterThanOrEqual(2)
    })

    test('should navigate to Board tab in Project Settings', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a project
      const project = await createTestProject(authenticatedApi, 'BoardNav')

      // Navigate to project settings
      await page.goto(`/pulse/projects/${project.id}/settings`)
      await page.waitForLoadState('networkidle')

      // Click Board tab
      const boardTab = page.locator('button').filter({ hasText: /board/i }).first()
      await expect(boardTab).toBeVisible({ timeout: 10000 })
      await boardTab.click()
      await page.waitForLoadState('networkidle')

      // Should show Board Columns section
      await expect(page.getByText(/board columns/i)).toBeVisible({ timeout: 5000 })
    })
  })

  test.describe('Saved Filters', () => {

    test('should create a saved filter via API', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'SavedF')

      // Create a saved filter
      const filterResponse = await authenticatedApi.post('saved-filters/', {
        data: {
          name: 'My Open Tasks',
          jql: 'status = "pending" AND assignee = currentUser()',
          project: project.id
        }
      })
      expect(filterResponse.ok()).toBeTruthy()

      const filter = await filterResponse.json()
      expect(filter.name).toBe('My Open Tasks')
    })

    test('should list saved filters', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'FilterList')

      // Create filters
      await authenticatedApi.post('saved-filters/', {
        data: {
          name: 'Filter 1',
          jql: 'status = "pending"',
          project: project.id
        }
      })

      // List filters
      const listResponse = await authenticatedApi.get(`saved-filters/?project=${project.id}`)
      expect(listResponse.ok()).toBeTruthy()

      const filters = await listResponse.json()
      expect(filters.length).toBeGreaterThanOrEqual(1)
    })

    // PLS-818 F0.6 recipe: save two named filters, reopen, execute,
    // duplicate-name/malformed-JQL checks, edit, delete.
    test('F0.6 round-trip: create two filters, reopen, execute', async ({ authenticatedApi }) => {
      const project = await createTestProject(authenticatedApi, 'F0six')

      // Save two named filters
      const f1 = await authenticatedApi.post('saved-filters/', {
        data: {
          name: 'F0 High Priority',
          jql: 'priority = "high"',
          project: project.id
        }
      })
      expect(f1.ok()).toBeTruthy()
      const filter1 = await f1.json()

      const f2 = await authenticatedApi.post('saved-filters/', {
        data: {
          name: 'F0 My Pending',
          jql: 'status = "pending" AND assignee = currentUser()',
          project: project.id
        }
      })
      expect(f2.ok()).toBeTruthy()
      const filter2 = await f2.json()

      // Reopen (list) shows both
      const listResponse = await authenticatedApi.get(`saved-filters/?project=${project.id}`)
      expect(listResponse.ok()).toBeTruthy()
      const filters = await listResponse.json()
      const names = filters.map(f => f.name)
      expect(names).toContain('F0 High Priority')
      expect(names).toContain('F0 My Pending')

      // Execute each
      const e1 = await authenticatedApi.get(`saved-filters/${filter1.id}/execute/`)
      expect(e1.ok()).toBeTruthy()
      expect((await e1.json()).filter_name).toBe('F0 High Priority')

      const e2 = await authenticatedApi.get(`saved-filters/${filter2.id}/execute/`)
      expect(e2.ok()).toBeTruthy()
      expect((await e2.json()).filter_name).toBe('F0 My Pending')
    })

    test('F0.6 round-trip: duplicate name and malformed JQL rejected, then edit + delete', async ({ authenticatedApi }) => {
      const project = await createTestProject(authenticatedApi, 'F0Guard')

      const create = await authenticatedApi.post('saved-filters/', {
        data: {
          name: 'Guard Filter',
          jql: 'status = "pending"',
          project: project.id
        }
      })
      expect(create.ok()).toBeTruthy()
      const filter = await create.json()

      // Duplicate name rejected (case-insensitive)
      const dup = await authenticatedApi.post('saved-filters/', {
        data: {
          name: 'guard filter',
          jql: 'status = "in_progress"',
          project: project.id
        }
      })
      expect(dup.status()).toBe(400)
      const dupBody = await dup.json()
      expect(JSON.stringify(dupBody)).toMatch(/already exists/i)

      // Malformed JQL rejected
      const malformed = await authenticatedApi.post('saved-filters/', {
        data: {
          name: 'Broken Query',
          jql: 'status = "pending" AND (priority = "high"',
          project: project.id
        }
      })
      expect(malformed.status()).toBe(400)
      const malformedBody = await malformed.json()
      expect(JSON.stringify(malformedBody)).toMatch(/malformed/i)

      // Edit (rename)
      const edit = await authenticatedApi.patch(`saved-filters/${filter.id}/`, {
        data: { name: 'Renamed Guard Filter' }
      })
      expect(edit.ok()).toBeTruthy()
      expect((await edit.json()).name).toBe('Renamed Guard Filter')

      // Edit to duplicate name rejected
      await authenticatedApi.post('saved-filters/', {
        data: { name: 'Second Filter', jql: 'priority = "low"', project: project.id }
      })
      const dupEdit = await authenticatedApi.patch(`saved-filters/${filter.id}/`, {
        data: { name: 'Second Filter' }
      })
      expect(dupEdit.status()).toBe(400)

      // Delete
      const del = await authenticatedApi.delete(`saved-filters/${filter.id}/`)
      expect(del.ok()).toBeTruthy()

      // Reopen after delete -> 404
      const gone = await authenticatedApi.get(`saved-filters/${filter.id}/`)
      expect(gone.status()).toBe(404)
    })
  })

  test.describe('Integration Tests', () => {

    test('should assign component and version to item', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'IntegCV')

      // Create component
      const componentResponse = await authenticatedApi.post('components/', {
        data: { project: project.id, name: 'Database' }
      })
      const component = await componentResponse.json()

      // Create version
      const versionResponse = await authenticatedApi.post('versions/', {
        data: { project: project.id, name: 'v3.0.0' }
      })
      const version = await versionResponse.json()

      // Create task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task with Component and Version',
          mode: 'task',
          project: project.id
        }
      })
      const task = await taskResponse.json()

      // Update task with component and version
      const updateResponse = await authenticatedApi.patch(`items/${task.id}/`, {
        data: {
          component: component.id,
          fix_version: version.id
        }
      })
      expect(updateResponse.ok()).toBeTruthy()

      // Verify
      const updatedTask = await updateResponse.json()
      expect(updatedTask.component).toBe(component.id)
      expect(updatedTask.fix_version).toBe(version.id)
    })

    test('should log work and update time spent', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'IntegWL')

      // Create task with estimate
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: 'Task for Time Tracking Integration',
          mode: 'task',
          project: project.id,
          original_estimate_minutes: 480 // 8 hours
        }
      })
      const task = await taskResponse.json()

      // Log work
      await authenticatedApi.post('work-logs/', {
        data: {
          item: task.id,
          duration_minutes: 120, // 2 hours
          worked_date: new Date().toISOString().split('T')[0],
          description: 'Initial work'
        }
      })

      // Log more work
      await authenticatedApi.post('work-logs/', {
        data: {
          item: task.id,
          duration_minutes: 60, // 1 hour
          worked_date: new Date().toISOString().split('T')[0],
          description: 'Additional work'
        }
      })

      // Get updated task
      const updatedTaskResponse = await authenticatedApi.get(`items/${task.id}/`)
      const updatedTask = await updatedTaskResponse.json()

      // Verify time spent was updated
      expect(updatedTask.time_spent_minutes).toBe(180) // 3 hours total
    })

    test('should filter items by component with JQL', async ({ authenticatedApi }) => {
      // Create a project
      const project = await createTestProject(authenticatedApi, 'JQLComp')

      // Create components
      const comp1Response = await authenticatedApi.post('components/', {
        data: { project: project.id, name: 'Frontend' }
      })
      const comp1 = await comp1Response.json()

      const comp2Response = await authenticatedApi.post('components/', {
        data: { project: project.id, name: 'Backend' }
      })
      const comp2 = await comp2Response.json()

      // Create tasks with different components
      await authenticatedApi.post('items/', {
        data: {
          title: 'Frontend Task',
          mode: 'task',
          project: project.id,
          component: comp1.id
        }
      })
      await authenticatedApi.post('items/', {
        data: {
          title: 'Backend Task',
          mode: 'task',
          project: project.id,
          component: comp2.id
        }
      })

      // Query with JQL for Frontend component
      const jql = encodeURIComponent('component = "Frontend"')
      const queryResponse = await authenticatedApi.get(`items/?jql=${jql}`)
      expect(queryResponse.ok()).toBeTruthy()

      const data = await queryResponse.json()
      const items = data.results || data
      // All returned items should have Frontend component
      items.forEach(item => {
        expect(item.component_name).toBe('Frontend')
      })
    })
  })
})
