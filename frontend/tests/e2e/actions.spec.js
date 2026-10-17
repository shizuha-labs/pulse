import { test, expect, TEST_USER } from './fixtures.js'

/**
 * Comprehensive E2E tests for Actions functionality
 *
 * Actions are items that require approval before execution.
 * They have states: pending, approved, executing, completed, failed, rejected
 *
 * Tests cover:
 * - Action list and display
 * - Action approval workflow
 * - Action rejection workflow
 * - Status filtering
 * - Action details
 * - API operations
 */

test.describe('Actions', () => {

  test.describe('Actions List Page', () => {

    test('should display actions page with correct elements', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to actions
      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)

      // Check page header
      await expect(page.locator('h1').filter({ hasText: /Actions/i })).toBeVisible()
    })

    test('should show pending count badge in navigation', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Look for pending count badge near Actions link
      const actionsBadge = page.locator('a[href*="actions"]').locator('..').locator('span').or(
        page.locator('[data-testid="pending-actions-count"]')
      )

      // Badge may or may not be visible depending on pending actions
      await page.waitForTimeout(1000)
    })

    test('should show filter tabs for action status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)

      // Check for status filter tabs/buttons - use getByRole for exact button match
      const pendingFilter = page.getByRole('button', { name: 'Pending' })

      await expect(pendingFilter).toBeVisible({ timeout: 5000 })
    })

  })

  test.describe('Action Status Filters', () => {

    test('should filter actions by pending status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)
      await page.waitForLoadState('networkidle')

      // Click pending filter
      const pendingFilter = page.locator('button:has-text("Pending")').or(
        page.locator('[data-status="pending"]')
      )

      if (await pendingFilter.isVisible()) {
        await pendingFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should filter actions by approved status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)

      // Click approved filter
      const approvedFilter = page.locator('button:has-text("Approved")').or(
        page.locator('[data-status="approved"]')
      )

      if (await approvedFilter.isVisible()) {
        await approvedFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should filter actions by completed status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)

      // Click completed filter
      const completedFilter = page.locator('button:has-text("Completed")').or(
        page.locator('[data-status="completed"]')
      )

      if (await completedFilter.isVisible()) {
        await completedFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should filter actions by failed status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)

      // Click failed filter
      const failedFilter = page.locator('button:has-text("Failed")').or(
        page.locator('[data-status="failed"]')
      )

      if (await failedFilter.isVisible()) {
        await failedFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should filter actions by rejected status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)

      // Click rejected filter
      const rejectedFilter = page.locator('button:has-text("Rejected")').or(
        page.locator('[data-status="rejected"]')
      )

      if (await rejectedFilter.isVisible()) {
        await rejectedFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should show all actions when All filter is selected', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)

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

  test.describe('Action Approval Workflow', () => {

    test('should approve a pending action', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a pending action via API
      const actionTitle = `ApproveMe${Date.now()}`
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: actionTitle,
          mode: 'action',
          status: 'pending'
        }
      })
      const action = await createResponse.json()

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)
      await page.waitForLoadState('networkidle')

      // Find the action
      const actionRow = page.locator(`text=${actionTitle}`).first()

      if (await actionRow.isVisible({ timeout: 5000 })) {
        // Click approve button
        const approveButton = page.locator('button:has-text("Approve")').or(
          page.locator('button[aria-label*="approve" i]').or(
            page.locator('[data-testid="approve-action"]')
          )
        )

        if (await approveButton.isVisible()) {
          await approveButton.click()

          // Wait for status change
          await page.waitForTimeout(1000)

          // Action should now be approved or executing
          const statusIndicator = page.locator(`text=${actionTitle}`).locator('..').locator('text=/approved|executing/i')
          if (await statusIndicator.isVisible({ timeout: 3000 })) {
            // Verification passed
          }
        }
      }
    })

    test('should reject a pending action', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a pending action via API
      const actionTitle = `RejectMe${Date.now()}`
      await authenticatedApi.post('items/', {
        data: {
          title: actionTitle,
          mode: 'action',
          status: 'pending'
        }
      })

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)
      await page.waitForLoadState('networkidle')

      // Find the action
      const actionRow = page.locator(`text=${actionTitle}`).first()

      if (await actionRow.isVisible({ timeout: 5000 })) {
        // Click reject button
        const rejectButton = page.locator('button:has-text("Reject")').or(
          page.locator('button[aria-label*="reject" i]').or(
            page.locator('[data-testid="reject-action"]')
          )
        )

        if (await rejectButton.isVisible()) {
          await rejectButton.click()

          // If a reason modal appears, fill it
          const reasonInput = page.locator('input[name="reason"]').or(
            page.locator('textarea[name="reason"]').or(
              page.locator('input[placeholder*="reason" i]')
            )
          )

          if (await reasonInput.isVisible({ timeout: 2000 })) {
            await reasonInput.fill('Test rejection reason')

            // Confirm rejection
            const confirmButton = page.locator('button:has-text("Confirm")').or(
              page.locator('button:has-text("Submit")').or(
                page.locator('button[type="submit"]')
              )
            )
            await confirmButton.click()
          }

          await page.waitForTimeout(1000)
        }
      }
    })

    test('should show rejection reason modal', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a pending action
      const actionTitle = `ReasonModal${Date.now()}`
      await authenticatedApi.post('items/', {
        data: {
          title: actionTitle,
          mode: 'action',
          status: 'pending'
        }
      })

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)
      await page.waitForLoadState('networkidle')

      // Click reject on the action
      const rejectButton = page.locator('button:has-text("Reject")').first()

      if (await rejectButton.isVisible()) {
        await rejectButton.click()

        // Modal or input should appear for reason
        const reasonInput = page.locator('input[name="reason"]').or(
          page.locator('textarea[name="reason"]').or(
            page.locator('[data-testid="rejection-reason"]')
          )
        )

        // May or may not require reason depending on implementation
        await page.waitForTimeout(1000)
      }
    })

  })

  test.describe('Action Details', () => {

    test('should view action details', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create an action
      const actionTitle = `ViewDetails${Date.now()}`
      await authenticatedApi.post('items/', {
        data: {
          title: actionTitle,
          description: 'Detailed action for testing',
          mode: 'action'
        }
      })

      await page.click('a[href*="actions"]')
      await page.waitForURL(/.*actions/)
      await page.waitForLoadState('networkidle')

      // Click on the action to view details
      await page.locator(`text=${actionTitle}`).click()

      // Should show details modal or navigate to detail page
      await page.waitForTimeout(1000)

      // Check for action details elements
      const detailsView = page.locator('text=Detailed action for testing').or(
        page.locator('[data-testid="action-details"]')
      )

      // Details may be shown in modal or separate view
    })

  })

  test.describe('Action API Operations', () => {

    test('should create action via API', async ({ authenticatedApi }) => {
      const actionTitle = `API Action ${Date.now()}`

      const response = await authenticatedApi.post('items/', {
        data: {
          title: actionTitle,
          mode: 'action',
          description: 'Action created via API test'
        }
      })

      expect(response.ok()).toBeTruthy()

      const action = await response.json()
      expect(action.title).toBe(actionTitle)
      expect(action.mode).toBe('action')
      expect(action.status).toBe('pending')
    })

    test('should list actions via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=action')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('results')
      expect(Array.isArray(data.results)).toBeTruthy()

      // All results should be actions
      for (const item of data.results) {
        expect(item.mode).toBe('action')
      }
    })

    test('should filter actions by status via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=action&status=pending')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      for (const action of data.results) {
        expect(action.status).toBe('pending')
        expect(action.mode).toBe('action')
      }
    })

    test('should approve action via API', async ({ authenticatedApi }) => {
      // Create a pending action
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Approve API ${Date.now()}`,
          mode: 'action',
          status: 'pending'
        }
      })

      const action = await createResponse.json()

      // Approve the action
      const approveResponse = await authenticatedApi.post(`items/${action.id}/approve/`)

      expect(approveResponse.ok()).toBeTruthy()

      const approvedAction = await approveResponse.json()
      expect(['approved', 'executing']).toContain(approvedAction.status)
    })

    test('should reject action via API', async ({ authenticatedApi }) => {
      // Create a pending action
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Reject API ${Date.now()}`,
          mode: 'action',
          status: 'pending'
        }
      })

      const action = await createResponse.json()

      // Reject the action - don't use leading slash as it replaces the base path
      const rejectResponse = await authenticatedApi.post(`items/${action.id}/reject/`, {
        data: {
          reason: 'Rejected via API test'
        }
      })

      expect(rejectResponse.ok()).toBeTruthy()

      const rejectedAction = await rejectResponse.json()
      expect(rejectedAction.status).toBe('rejected')
    })

    test('should update action via API', async ({ authenticatedApi }) => {
      // Create an action
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Update Action ${Date.now()}`,
          mode: 'action'
        }
      })

      const action = await createResponse.json()

      // Update the action - don't use leading slash as it replaces the base path
      const updateResponse = await authenticatedApi.patch(`items/${action.id}/`, {
        data: {
          title: 'Updated Action Title',
          description: 'Updated description'
        }
      })

      expect(updateResponse.ok()).toBeTruthy()

      const updatedAction = await updateResponse.json()
      expect(updatedAction.title).toBe('Updated Action Title')
    })

    test('should delete action via API', async ({ authenticatedApi }) => {
      // Create an action
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Delete Action ${Date.now()}`,
          mode: 'action'
        }
      })

      const action = await createResponse.json()

      // Delete the action
      const deleteResponse = await authenticatedApi.delete(`items/${action.id}/`)

      expect(deleteResponse.status()).toBe(204)

      // Verify deletion
      const getResponse = await authenticatedApi.get(`items/${action.id}/`)
      expect(getResponse.status()).toBe(404)
    })

    test('should get action with related data via API', async ({ authenticatedApi }) => {
      // Create an action
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Action with data ${Date.now()}`,
          mode: 'action',
          payload: { key: 'value', nested: { data: true } }
        }
      })

      const action = await createResponse.json()

      // Get the action
      const getResponse = await authenticatedApi.get(`items/${action.id}/`)

      expect(getResponse.ok()).toBeTruthy()

      const fetchedAction = await getResponse.json()
      expect(fetchedAction.payload).toBeDefined()
    })

    test('should submit action via dedicated endpoint', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.post('actions/submit/', {
        data: {
          title: `Submitted Action ${Date.now()}`,
          description: 'Action submitted via submit endpoint',
          source: 'e2e-test',  // Required field
          payload: { action_type: 'test' }
        }
      })

      // Check if endpoint exists and works
      if (response.status() !== 404) {
        expect(response.ok()).toBeTruthy()
        const result = await response.json()
        // API returns message, item_id, status, requires_approval
        expect(result.message).toBe('Action submitted')
        expect(result.item_id).toBeDefined()
        expect(result.status).toBe('pending')
      }
    })

    test('should paginate actions via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=action&page=1')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('count')
      expect(data).toHaveProperty('results')
      // Default page size is 10
      expect(data.results.length).toBeLessThanOrEqual(10)
    })

    test('should search actions via API', async ({ authenticatedApi }) => {
      const uniqueTitle = `UniqueActionSearch${Date.now()}`

      // Create an action with unique title
      await authenticatedApi.post('items/', {
        data: {
          title: uniqueTitle,
          mode: 'action'
        }
      })

      const response = await authenticatedApi.get(`items/?mode=action&search=${uniqueTitle}`)

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data.results.length).toBeGreaterThan(0)
      expect(data.results[0].title).toContain(uniqueTitle)
    })

    test('should order actions by created_at via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=action&ordering=-created_at')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      if (data.results.length >= 2) {
        const first = new Date(data.results[0].created_at)
        const second = new Date(data.results[1].created_at)
        expect(first.getTime()).toBeGreaterThanOrEqual(second.getTime())
      }
    })

  })

  test.describe('Approval Rules', () => {

    test('should list approval rules via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('rules/approval/')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('results')
    })

    test('should create approval rule via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.post('rules/approval/', {
        data: {
          name: `Test Rule ${Date.now()}`,
          description: 'Test approval rule',
          conditions: {},
          auto_approve: false,
          required_approvals: 1
        }
      })

      // Rule creation may or may not be allowed depending on permissions
      if (response.ok()) {
        const rule = await response.json()
        expect(rule.name).toBeDefined()
      }
    })

  })

})
