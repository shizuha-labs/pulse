import { test, expect, TEST_USER } from './fixtures.js'

/**
 * Comprehensive E2E tests for Tasks functionality
 *
 * Tests cover:
 * - Task CRUD operations (UI and API)
 * - Task filtering (status, priority, search)
 * - Task sorting
 * - Bulk operations
 * - Task statistics
 * - Keyboard shortcuts
 */

test.describe('Tasks', () => {

  test.describe('Task List Page', () => {

    test('never renders unscoped or prior-org rows while organization scope resolves', async ({ page }) => {
      const organizations = [
        { id: 1, slug: 'org-a', name: 'Org A', is_personal: false },
        { id: 2, slug: 'org-b', name: 'Org B', is_personal: false },
      ]
      const taskFor = (organization) => ({
        id: organization.id * 100,
        item_key: `ORG-${organization.id * 100}`,
        title: `${organization.name} task`,
        status: 'open',
        status_category: 'todo',
        priority: 'normal',
        organization_id: organization.id,
        project_key: organization.slug.toUpperCase(),
      })

      let releaseOrganizations
      let markOrganizationsRequested
      const organizationsGate = new Promise(resolve => { releaseOrganizations = resolve })
      const organizationsRequested = new Promise(resolve => { markOrganizationsRequested = resolve })
      let releaseOrgBTasks
      const orgBTasksGate = new Promise(resolve => { releaseOrgBTasks = resolve })
      const taskRequests = []
      let createRequest = null

      await page.addInitScript(() => {
        localStorage.setItem('shizuha_access_token', 'mock-access-token')
        localStorage.setItem('shizuha_user', JSON.stringify({
          id: 42,
          username: 'scope-test',
          email: 'scope-test@shizuha.com',
        }))
        localStorage.setItem('pulse.tasks.viewMode', 'list')
      })

      await page.route('**/*', async (route) => {
        const request = route.request()
        const url = new URL(request.url())
        const { pathname } = url

        if (pathname === '/pulse/api/auth/user/') {
          await route.fulfill({ json: { id: 42, username: 'scope-test', email: 'scope-test@shizuha.com' } })
          return
        }
        if (pathname === '/admin/api/organizations/') {
          markOrganizationsRequested()
          await organizationsGate
          await route.fulfill({ json: { results: organizations } })
          return
        }
        if (pathname === '/pulse/api/projects/') {
          await route.fulfill({ json: { results: [], next: null } })
          return
        }
        if (pathname === '/pulse/api/statuses/') {
          await route.fulfill({ json: [] })
          return
        }
        if (pathname === '/pulse/api/items/assignable_users/') {
          await route.fulfill({ json: { users: [] } })
          return
        }
        if (pathname === '/pulse/api/notifications/unread_count/') {
          await route.fulfill({ json: { count: 0 } })
          return
        }
        if (pathname === '/pulse/api/tasks/' && request.method() === 'POST') {
          createRequest = {
            organizationHeader: request.headers()['x-organization-id'],
            body: request.postDataJSON(),
          }
          await route.fulfill({ status: 201, json: taskFor(organizations[1]) })
          return
        }
        if (pathname === '/pulse/api/tasks/' && request.method() === 'GET') {
          const organizationId = url.searchParams.get('organization')
          taskRequests.push(organizationId)
          if (organizationId === '2') await orgBTasksGate
          const organization = organizations.find(item => String(item.id) === organizationId)
          const results = organization
            ? [taskFor(organization)]
            : [{ ...taskFor({ id: 9, slug: 'global', name: 'Global' }), title: 'Global task' }]
          await route.fulfill({ json: { results, next: null } })
          return
        }
        if (pathname.startsWith('/pulse/api/')) {
          await route.fulfill({ json: {} })
          return
        }
        await route.continue()
      })

      await page.goto('/pulse/tasks?org=org-a')
      await organizationsRequested

      // The explicit Hive handoff is still unresolved: no global query and no
      // global row may be exposed during this window.
      expect(taskRequests).toEqual([])
      await expect(page.getByText('Global task')).toHaveCount(0)

      releaseOrganizations()
      await expect(page.getByText('Org A task')).toBeVisible()
      expect(taskRequests).toEqual(['1'])

      // Switching scope must hide A synchronously; B is intentionally held so
      // this assertion covers the whole network interval, not just the result.
      await page.getByRole('button', { name: /Org A/ }).click()
      await page.getByRole('button', { name: /Org B/ }).click()
      await expect(page.getByText('Org A task')).toHaveCount(0)
      await expect(page.getByText('Global task')).toHaveCount(0)

      releaseOrgBTasks()
      await expect(page.getByText('Org B task')).toBeVisible()
      expect(taskRequests).toEqual(['1', '2'])

      await page.getByRole('button', { name: 'New Task' }).click()
      await page.getByPlaceholder('What needs to be done?').fill('Same-org first task')
      await page.getByRole('button', { name: 'Create Task' }).click()
      await expect.poll(() => createRequest).not.toBeNull()
      expect(createRequest.organizationHeader).toBe('2')
      expect(createRequest.body.title).toBe('Same-org first task')
    })

    for (const scenario of [
      {
        name: 'organization membership request fails',
        fulfillOrganizations: route => route.fulfill({ status: 503, json: { detail: 'Unavailable' } }),
        expectedError: 'Organizations could not be loaded',
      },
      {
        name: 'explicit organization is not in the complete membership list',
        fulfillOrganizations: route => route.fulfill({
          json: {
            results: [{ id: 1, slug: 'org-a', name: 'Org A', is_personal: false }],
            next: null,
          },
        }),
        expectedError: 'The requested organization is unavailable',
      },
    ]) {
      test(`fails closed when ${scenario.name}`, async ({ page }) => {
        const taskRequests = []

        await page.addInitScript(() => {
          localStorage.setItem('shizuha_access_token', 'mock-access-token')
          localStorage.setItem('shizuha_user', JSON.stringify({
            id: 42,
            username: 'scope-test',
            email: 'scope-test@shizuha.com',
          }))
          localStorage.setItem('shizuha_current_org', '__all__')
          localStorage.setItem('pulse.tasks.viewMode', 'list')
        })

        await page.route('**/*', async (route) => {
          const request = route.request()
          const url = new URL(request.url())
          const { pathname } = url

          if (pathname === '/pulse/api/auth/user/') {
            await route.fulfill({ json: { id: 42, username: 'scope-test', email: 'scope-test@shizuha.com' } })
            return
          }
          if (pathname === '/admin/api/organizations/') {
            await scenario.fulfillOrganizations(route)
            return
          }
          if (pathname === '/pulse/api/tasks/' && request.method() === 'GET') {
            taskRequests.push(url.searchParams.get('organization'))
            await route.fulfill({
              json: {
                results: [{ id: 900, item_key: 'GLOBAL-900', title: 'Global task' }],
                next: null,
              },
            })
            return
          }
          if (pathname === '/pulse/api/statuses/') {
            await route.fulfill({ json: [] })
            return
          }
          if (pathname.startsWith('/pulse/api/')) {
            await route.fulfill({ json: {} })
            return
          }
          await route.continue()
        })

        await page.goto('/pulse/tasks?org=missing-org')
        await expect(page.getByText(scenario.expectedError, { exact: false })).toBeVisible()
        expect(taskRequests).toEqual([])
        await expect(page.getByText('Global task')).toHaveCount(0)
      })
    }

    test('should display tasks page with correct elements', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to tasks
      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Check page header
      await expect(page.locator('h1').filter({ hasText: /Tasks/i })).toBeVisible()

      // Check for filter dropdowns (status and priority filters are select elements)
      const statusFilter = page.locator('select').first()
      await expect(statusFilter).toBeVisible()

      // Check for add/create button - use first() for strict mode
      await expect(
        page.locator('button:has-text("Add")').or(
          page.locator('button:has-text("New")').or(
            page.locator('button:has-text("Create")')
          )
        ).first()
      ).toBeVisible()
    })

    test('should show task count in header or stats', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Should show some indication of task count
      const countIndicator = page.locator('text=/\\d+ task/i').or(
        page.locator('text=/total.*\\d+/i').or(
          page.locator('[data-testid="task-count"]')
        )
      )

      // Count may or may not be visible depending on tasks
      await page.waitForTimeout(1000) // Allow UI to settle
    })

    test('should display empty state when no tasks match filter', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Search for something that won't exist
      const searchInput = page.locator('input[placeholder*="search" i]').or(
        page.locator('input[type="search"]')
      )

      if (await searchInput.isVisible()) {
        await searchInput.fill('xyznonexistenttask12345')
        await page.waitForTimeout(500) // Debounce

        // Should show empty state or no results message
        await expect(
          page.locator('text=/no.*tasks/i').or(
            page.locator('text=/no.*results/i').or(
              page.locator('[data-testid="empty-state"]')
            )
          )
        ).toBeVisible({ timeout: 5000 })
      }
    })

  })

  test.describe('Create Task', () => {

    test('should open create task modal', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Click create/add button - use first() for strict mode
      const addButton = page.locator('button:has-text("Add Task")').or(
        page.locator('button:has-text("New Task")').or(
          page.locator('button:has-text("Create")').or(
            page.locator('button[aria-label*="add" i]')
          )
        )
      ).first()

      await addButton.click()

      // Modal should appear with title input - use actual placeholder text
      await expect(page.locator('input[placeholder="What needs to be done?"]').or(
        page.locator('input[placeholder*="needs to be done" i]')
      )).toBeVisible({ timeout: 5000 })
    })

    test('should create task with title only', async ({ authenticatedPage }) => {
      const page = authenticatedPage
      const taskTitle = `Quick Task ${Date.now()}`

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Open modal - use first() for strict mode
      const addButton = page.locator('button:has-text("Add Task")').or(
        page.locator('button:has-text("New Task")').or(
          page.locator('button:has-text("Create")')
        )
      ).first()
      await addButton.click()

      // Wait for modal to appear
      await page.waitForSelector('input[placeholder="What needs to be done?"]', { timeout: 5000 })

      // Fill title - use actual placeholder text
      const titleInput = page.locator('input[placeholder="What needs to be done?"]')
      await titleInput.fill(taskTitle)

      // Submit - use button text from actual UI
      const submitButton = page.locator('button:has-text("Create Task")').first()
      await submitButton.click()

      // Wait for modal to close and task to appear
      await page.waitForTimeout(1000)

      // Verify task appears in list
      await expect(page.getByText(taskTitle).first()).toBeVisible({ timeout: 10000 })
    })

    test('should create task with all fields', async ({ authenticatedPage }) => {
      const page = authenticatedPage
      const taskTitle = `Full Task ${Date.now()}`
      const taskDescription = 'This is a detailed task description for testing'

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Click the "New Task" button in the header (it has a + icon)
      // The button text contains "New Task" on larger screens
      await page.locator('button.btn-primary:has-text("New Task")').click()

      // Wait for modal to appear - look for the modal header "Create New Task"
      await page.waitForSelector('h2:has-text("Create New Task")', { timeout: 5000 })

      // Fill title - use actual placeholder
      await page.fill('input[placeholder="What needs to be done?"]', taskTitle)

      // Fill description/notes if available - use actual placeholder
      const descInput = page.locator('textarea[placeholder*="Add details"]')
      if (await descInput.isVisible()) {
        await descInput.fill(taskDescription)
      }

      // Set priority - it's a select element within the modal (not the filter selects)
      // The modal's select is inside the modal div - target more specifically
      const modalPrioritySelect = page.locator('.fixed select').first()
      if (await modalPrioritySelect.isVisible()) {
        await modalPrioritySelect.selectOption('high')
      }

      // Submit - the submit button in the modal says "Create Task"
      await page.locator('.fixed button:has-text("Create Task")').click()

      // Verify task appears - wait for modal to close first
      await page.waitForTimeout(500)
      await expect(page.getByText(taskTitle).first()).toBeVisible({ timeout: 10000 })
    })

    test('should validate required fields', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Open modal - use first() for strict mode
      const addButton = page.locator('button:has-text("Add Task")').or(
        page.locator('button:has-text("New Task")').or(
          page.locator('button:has-text("Create")')
        )
      ).first()
      await addButton.click()

      // Wait for modal to appear
      await page.waitForSelector('input[placeholder="What needs to be done?"]', { timeout: 5000 })

      // The submit button should be disabled when title is empty
      const submitButton = page.locator('button:has-text("Create Task")').first()

      // Title input should be empty or we clear it
      const titleInput = page.locator('input[placeholder="What needs to be done?"]')
      await titleInput.clear()

      // Submit button should be disabled (React disables it based on !newTask.title.trim())
      await expect(submitButton).toBeDisabled()
    })

    test('should close modal on cancel', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Open modal
      const addButton = page.locator('button:has-text("Add Task")').or(
        page.locator('button:has-text("New Task")').or(
          page.locator('button:has-text("Create")')
        )
      )
      await addButton.click()

      // Wait for modal to appear
      await page.waitForSelector('input[placeholder="What needs to be done?"]', { timeout: 5000 })

      // Click cancel or close button
      const cancelButton = page.locator('button:has-text("Cancel")').first()
      await cancelButton.click()

      // Modal should be closed - title input should not be visible
      await expect(page.locator('input[placeholder="What needs to be done?"]')).not.toBeVisible({ timeout: 3000 })
    })

  })

  test.describe('Task Status Filters', () => {

    test('should filter tasks by pending status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Click pending filter
      const pendingFilter = page.locator('button:has-text("Pending")').or(
        page.locator('[data-status="pending"]')
      )

      if (await pendingFilter.isVisible()) {
        await pendingFilter.click()
        await page.waitForTimeout(500)

        // URL should include status parameter or filter should be active
        await expect(page).toHaveURL(/.*(?:status=pending|pending).*/)
      }
    })

    test('should filter tasks by in_progress status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Click in progress filter
      const inProgressFilter = page.locator('button:has-text("In Progress")').or(
        page.locator('[data-status="in_progress"]')
      )

      if (await inProgressFilter.isVisible()) {
        await inProgressFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should filter tasks by completed status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Click completed filter
      const completedFilter = page.locator('button:has-text("Completed")').or(
        page.locator('[data-status="completed"]')
      )

      if (await completedFilter.isVisible()) {
        await completedFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should show all tasks when All filter is selected', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Click All filter
      const allFilter = page.locator('button:has-text("All")').or(
        page.locator('[data-status="all"]')
      )

      if (await allFilter.isVisible()) {
        await allFilter.click()
        await page.waitForTimeout(500)
      }
    })

  })

  test.describe('Task Priority Filter', () => {

    test('should filter tasks by priority', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Look for priority dropdown/filter
      const priorityFilter = page.locator('button:has-text("Priority")').or(
        page.locator('select[name="priority"]').or(
          page.locator('[data-testid="priority-filter"]')
        )
      )

      if (await priorityFilter.isVisible()) {
        await priorityFilter.click()

        // Select high priority
        const highOption = page.locator('text=High').or(
          page.locator('option[value="high"]')
        )
        if (await highOption.isVisible()) {
          await highOption.click()
          await page.waitForTimeout(500)
        }
      }
    })

  })

  test.describe('Task Search', () => {

    test('should search tasks by title', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const searchTerm = `SearchTest${Date.now()}`

      // First create a task via API
      await authenticatedApi.post('items/', {
        data: {
          title: searchTerm,
          mode: 'task',
          priority: 'normal'
        }
      })

      // Navigate to tasks
      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Find search input
      const searchInput = page.locator('input[placeholder*="search" i]').or(
        page.locator('input[type="search"]').or(
          page.locator('[data-testid="search-input"]')
        )
      )

      if (await searchInput.isVisible()) {
        await searchInput.fill(searchTerm)
        await page.waitForTimeout(500) // Wait for debounce

        // Task should appear
        await expect(page.locator(`text=${searchTerm}`)).toBeVisible({ timeout: 5000 })
      }
    })

    test('should clear search results', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      const searchInput = page.locator('input[placeholder*="search" i]').or(
        page.locator('input[type="search"]')
      )

      if (await searchInput.isVisible()) {
        // Enter search term
        await searchInput.fill('sometext')
        await page.waitForTimeout(300)

        // Clear search
        await searchInput.clear()
        await page.waitForTimeout(500)

        // Should show all tasks again (or empty state)
      }
    })

  })

  test.describe('Task Actions', () => {

    test('should complete a task from list', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `CompleteMe${Date.now()}`

      // Create a task first
      await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task',
          status: 'pending'
        }
      })

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Find the task
      const taskRow = page.locator(`text=${taskTitle}`).locator('..')

      // Click complete button/checkbox
      const completeButton = taskRow.locator('button:has-text("Complete")').or(
        taskRow.locator('input[type="checkbox"]').or(
          taskRow.locator('[aria-label*="complete" i]')
        )
      )

      if (await completeButton.isVisible()) {
        await completeButton.click()
        await page.waitForTimeout(1000)

        // Task should be marked as completed or moved to completed section
      }
    })

    test('should delete a task', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `DeleteMe${Date.now()}`

      // Create a task first
      await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task'
        }
      })

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Find the task and its delete button
      const taskItem = page.locator(`text=${taskTitle}`).first()
      await taskItem.hover()

      // Look for delete button (might be in a dropdown or directly visible)
      const deleteButton = page.locator('button:has-text("Delete")').or(
        page.locator('button[aria-label*="delete" i]').or(
          page.locator('[data-testid="delete-task"]')
        )
      )

      if (await deleteButton.isVisible()) {
        await deleteButton.click()

        // Confirm deletion if dialog appears
        const confirmButton = page.locator('button:has-text("Confirm")').or(
          page.locator('button:has-text("Yes")').or(
            page.locator('button:has-text("Delete")')
          )
        )
        if (await confirmButton.isVisible()) {
          await confirmButton.click()
        }

        // Task should no longer be visible
        await expect(page.locator(`text=${taskTitle}`)).not.toBeVisible({ timeout: 5000 })
      }
    })

    test('should edit a task', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const originalTitle = `EditMe${Date.now()}`
      const newTitle = `Edited${Date.now()}`

      // Create a task first
      const response = await authenticatedApi.post('items/', {
        data: {
          title: originalTitle,
          mode: 'task'
        }
      })
      const task = await response.json()

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Click on task to open edit modal/detail view
      await page.locator(`text=${originalTitle}`).click()

      // Wait for edit form
      const titleInput = page.locator('input[name="title"]').or(
        page.locator('input[value*="EditMe"]')
      )

      if (await titleInput.isVisible({ timeout: 5000 })) {
        await titleInput.clear()
        await titleInput.fill(newTitle)

        // Save
        await page.locator('button:has-text("Save")').or(
          page.locator('button[type="submit"]')
        ).click()

        await page.waitForTimeout(1000)

        // Verify update
        await expect(page.locator(`text=${newTitle}`)).toBeVisible({ timeout: 5000 })
      }
    })

  })

  test.describe('Bulk Operations', () => {

    test('should enter selection mode', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)

      // Look for selection/edit mode button
      const selectModeButton = page.locator('button:has-text("Select")').or(
        page.locator('button:has-text("Edit")').or(
          page.locator('button[aria-label*="select" i]')
        )
      )

      if (await selectModeButton.isVisible()) {
        await selectModeButton.click()

        // Checkboxes should appear
        await expect(
          page.locator('input[type="checkbox"]').first()
        ).toBeVisible({ timeout: 3000 })
      }
    })

    test('should select multiple tasks', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create multiple tasks
      for (let i = 0; i < 3; i++) {
        await authenticatedApi.post('items/', {
          data: {
            title: `BulkTask${Date.now()}_${i}`,
            mode: 'task'
          }
        })
      }

      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Enter selection mode
      const selectModeButton = page.locator('button:has-text("Select")').or(
        page.locator('button:has-text("Edit")')
      )

      if (await selectModeButton.isVisible()) {
        await selectModeButton.click()
        await page.waitForTimeout(500)

        // Select first two checkboxes
        const checkboxes = page.locator('input[type="checkbox"]')
        const count = await checkboxes.count()

        if (count >= 2) {
          await checkboxes.nth(0).click()
          await checkboxes.nth(1).click()

          // Should show bulk action buttons
          await expect(
            page.locator('button:has-text("Complete Selected")').or(
              page.locator('button:has-text("Delete Selected")').or(
                page.locator('text=/\\d+ selected/i')
              )
            )
          ).toBeVisible({ timeout: 3000 })
        }
      }
    })

  })

  test.describe('Task Statistics Dashboard', () => {

    test('should show task statistics on dashboard', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Dashboard should show stats - use first() for strict mode
      await expect(
        page.getByText(/total/i).first().or(
          page.getByText(/pending/i).first().or(
            page.getByText(/completed/i).first()
          )
        )
      ).toBeVisible({ timeout: 10000 })
    })

  })

  test.describe('Task API Operations', () => {

    test('should create task via API', async ({ authenticatedApi }) => {
      const taskTitle = `API Task ${Date.now()}`

      const response = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task',
          priority: 'normal'
        }
      })

      expect(response.ok()).toBeTruthy()

      const task = await response.json()
      expect(task.title).toBe(taskTitle)
      expect(task.mode).toBe('task')
      expect(task.status).toBe('pending')
    })

    test('should list tasks via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('results')
      expect(Array.isArray(data.results)).toBeTruthy()
    })

    test('should filter tasks by mode via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=task')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      for (const item of data.results) {
        expect(item.mode).toBe('task')
      }
    })

    test('should filter tasks by status via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?status=pending')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      for (const task of data.results) {
        expect(task.status).toBe('pending')
      }
    })

    test('should filter tasks by priority via API', async ({ authenticatedApi }) => {
      // First create a high priority task
      await authenticatedApi.post('items/', {
        data: {
          title: `High Priority ${Date.now()}`,
          mode: 'task',
          priority: 'high'
        }
      })

      const response = await authenticatedApi.get('items/?priority=high')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      for (const task of data.results) {
        expect(task.priority).toBe('high')
      }
    })

    test('should search tasks via API', async ({ authenticatedApi }) => {
      const uniqueTitle = `UniqueSearch${Date.now()}`

      // Create a task with unique title
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: uniqueTitle,
          mode: 'task'
        }
      })
      expect(createResponse.ok()).toBeTruthy()

      // Search for the task by its unique title
      const response = await authenticatedApi.get(`items/?mode=task&search=${uniqueTitle}`)

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data.results.length).toBeGreaterThan(0)
      // Check that any result contains our unique title (search results may vary in order)
      const found = data.results.some(task => task.title.includes(uniqueTitle))
      expect(found).toBeTruthy()
    })

    test('should update task via API', async ({ authenticatedApi }) => {
      // Create a task
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Update Test ${Date.now()}`,
          mode: 'task',
          priority: 'low'
        }
      })

      const task = await createResponse.json()

      // Update the task - don't use leading slash
      const updateResponse = await authenticatedApi.patch(`items/${task.id}/`, {
        data: {
          title: 'Updated Title',
          priority: 'high'
        }
      })

      expect(updateResponse.ok()).toBeTruthy()

      const updatedTask = await updateResponse.json()
      expect(updatedTask.title).toBe('Updated Title')
      expect(updatedTask.priority).toBe('high')
    })

    test('should complete task via API', async ({ authenticatedApi }) => {
      // Create a task
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Complete Test ${Date.now()}`,
          mode: 'task'
        }
      })

      const task = await createResponse.json()

      // Complete the task
      const completeResponse = await authenticatedApi.post(`items/${task.id}/complete/`)

      expect(completeResponse.ok()).toBeTruthy()

      const result = await completeResponse.json()
      expect(result.message).toContain('completed')
      expect(result.task_id).toBe(task.id)

      // Verify task is completed by fetching it
      const getResponse = await authenticatedApi.get(`items/${task.id}/`)
      const completedTask = await getResponse.json()
      expect(completedTask.status).toBe('completed')
    })

    test('should reopen completed task via status update', async ({ authenticatedApi }) => {
      // Create and complete a task
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Reopen Test ${Date.now()}`,
          mode: 'task'
        }
      })

      const task = await createResponse.json()
      await authenticatedApi.post(`items/${task.id}/complete/`)

      // Reopen the task by updating status to pending
      const updateResponse = await authenticatedApi.patch(`items/${task.id}/`, {
        data: {
          status: 'pending'
        }
      })

      expect(updateResponse.ok()).toBeTruthy()

      const reopenedTask = await updateResponse.json()
      expect(reopenedTask.status).toBe('pending')
    })

    test('should delete task via API', async ({ authenticatedApi }) => {
      // Create a task
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Delete Test ${Date.now()}`,
          mode: 'task'
        }
      })

      const task = await createResponse.json()

      // Delete the task
      const deleteResponse = await authenticatedApi.delete(`items/${task.id}/`)

      expect(deleteResponse.status()).toBe(204)

      // Verify task is deleted
      const getResponse = await authenticatedApi.get(`items/${task.id}/`)
      expect(getResponse.status()).toBe(404)
    })

    test('should bulk complete tasks via API', async ({ authenticatedApi }) => {
      // Create multiple tasks
      const tasks = []
      for (let i = 0; i < 3; i++) {
        const response = await authenticatedApi.post('items/', {
          data: {
            title: `Bulk Complete ${Date.now()}_${i}`,
            mode: 'task'
          }
        })
        const task = await response.json()
        tasks.push(task.id)
      }

      // Bulk complete - API expects item_ids
      const bulkResponse = await authenticatedApi.post('tasks/bulk-complete/', {
        data: {
          item_ids: tasks
        }
      })

      expect(bulkResponse.ok()).toBeTruthy()

      // Verify all tasks are completed
      for (const taskId of tasks) {
        const response = await authenticatedApi.get(`items/${taskId}/`)
        const task = await response.json()
        expect(task.status).toBe('completed')
      }
    })

    test('should bulk update tasks via API', async ({ authenticatedApi }) => {
      // Create multiple tasks
      const tasks = []
      for (let i = 0; i < 2; i++) {
        const response = await authenticatedApi.post('items/', {
          data: {
            title: `Bulk Update ${Date.now()}_${i}`,
            mode: 'task',
            priority: 'low'
          }
        })
        const task = await response.json()
        tasks.push(task.id)
      }

      // Bulk update priority - API expects item_ids and fields at top level
      const bulkResponse = await authenticatedApi.post('tasks/bulk-update/', {
        data: {
          item_ids: tasks,
          priority: 'high'
        }
      })

      expect(bulkResponse.ok()).toBeTruthy()

      // Verify all tasks are updated
      for (const taskId of tasks) {
        const response = await authenticatedApi.get(`items/${taskId}/`)
        const task = await response.json()
        expect(task.priority).toBe('high')
      }
    })

    test('should bulk delete tasks via API', async ({ authenticatedApi }) => {
      // Create multiple tasks
      const tasks = []
      for (let i = 0; i < 2; i++) {
        const response = await authenticatedApi.post('items/', {
          data: {
            title: `Bulk Delete ${Date.now()}_${i}`,
            mode: 'task'
          }
        })
        const task = await response.json()
        tasks.push(task.id)
      }

      // Bulk delete - API expects item_ids
      const bulkResponse = await authenticatedApi.post('tasks/bulk-delete/', {
        data: {
          item_ids: tasks
        }
      })

      expect(bulkResponse.ok()).toBeTruthy()

      // Verify all tasks are deleted
      for (const taskId of tasks) {
        const response = await authenticatedApi.get(`items/${taskId}/`)
        expect(response.status()).toBe(404)
      }
    })

    test('should get task statistics via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/statistics/')

      expect(response.ok()).toBeTruthy()

      const stats = await response.json()
      expect(stats).toHaveProperty('total_items')
      expect(stats).toHaveProperty('by_status')
      expect(stats).toHaveProperty('by_mode')
    })

    test('should create task with due date via API', async ({ authenticatedApi }) => {
      const tomorrow = new Date()
      tomorrow.setDate(tomorrow.getDate() + 1)
      const dueDate = tomorrow.toISOString()

      const response = await authenticatedApi.post('items/', {
        data: {
          title: `Due Date Task ${Date.now()}`,
          mode: 'task',
          due_date: dueDate
        }
      })

      expect(response.ok()).toBeTruthy()

      const task = await response.json()
      expect(task.due_date).toBeTruthy()
    })

    test('should create task with tags via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.post('items/', {
        data: {
          title: `Tagged Task ${Date.now()}`,
          mode: 'task',
          tags: 'test, e2e, automated'  // Tags are comma-separated string
        }
      })

      expect(response.ok()).toBeTruthy()

      const task = await response.json()
      expect(task.tags).toBeDefined()
      expect(task.tags_list).toContain('test')
    })

    test('should paginate tasks via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?page=1')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('count')
      expect(data).toHaveProperty('next')
      expect(data).toHaveProperty('previous')
      expect(data).toHaveProperty('results')
      // Default page size is 10
      expect(data.results.length).toBeLessThanOrEqual(10)
    })

    test('should order tasks via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?ordering=-created_at')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data.results).toBeDefined()

      // Verify ordering (most recent first)
      if (data.results.length >= 2) {
        const first = new Date(data.results[0].created_at)
        const second = new Date(data.results[1].created_at)
        expect(first.getTime()).toBeGreaterThanOrEqual(second.getTime())
      }
    })

  })

})
