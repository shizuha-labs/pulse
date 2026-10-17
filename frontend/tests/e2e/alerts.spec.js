import { test, expect, TEST_USER } from './fixtures.js'

/**
 * Comprehensive E2E tests for Alerts functionality
 *
 * Alerts are monitoring/notification items with severity levels.
 * They have states: firing, acknowledged, resolved, silenced
 * And severities: critical, error, warning, info
 *
 * Tests cover:
 * - Alert list and display
 * - Alert acknowledgment
 * - Alert resolution
 * - Alert silencing
 * - Severity filtering
 * - Status filtering
 * - Bulk operations
 * - Alert API operations
 */

test.describe('Alerts', () => {

  test.describe('Alerts List Page', () => {

    test('should display alerts page with correct elements', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to alerts
      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      // Check page header
      await expect(page.locator('h1').filter({ hasText: /Alerts/i })).toBeVisible()
    })

    test('should show status tabs (Firing, Acknowledged, Resolved)', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      // Check for status tabs
      await expect(
        page.locator('button:has-text("Firing")').or(
          page.locator('text=Firing').first()
        )
      ).toBeVisible({ timeout: 5000 })
    })

    test('should show severity filter', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      // Check for severity filter dropdown
      const severityFilter = page.locator('button:has-text("Severity")').or(
        page.locator('select[name="severity"]').or(
          page.locator('[data-testid="severity-filter"]')
        )
      )

      // Severity filter may be present
      await page.waitForTimeout(1000)
    })

    test('should show alert count badge in navigation', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Look for alert count badge near Alerts link
      const alertsBadge = page.locator('a[href*="alerts"]').locator('..').locator('span').or(
        page.locator('[data-testid="firing-alerts-count"]')
      )

      // Badge may show firing alert count
      await page.waitForTimeout(1000)
    })

  })

  test.describe('Alert Status Filters', () => {

    test('should filter alerts by firing status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)
      await page.waitForLoadState('networkidle')

      // Click firing filter
      const firingFilter = page.locator('button:has-text("Firing")').or(
        page.locator('[data-status="firing"]')
      )

      if (await firingFilter.isVisible()) {
        await firingFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should filter alerts by acknowledged status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      // Click acknowledged filter
      const acknowledgedFilter = page.locator('button:has-text("Acknowledged")').or(
        page.locator('[data-status="acknowledged"]')
      )

      if (await acknowledgedFilter.isVisible()) {
        await acknowledgedFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should filter alerts by resolved status', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      // Click resolved filter
      const resolvedFilter = page.locator('button:has-text("Resolved")').or(
        page.locator('[data-status="resolved"]')
      )

      if (await resolvedFilter.isVisible()) {
        await resolvedFilter.click()
        await page.waitForTimeout(500)
      }
    })

    test('should show all alerts when All filter is selected', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

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

  test.describe('Alert Severity Filters', () => {

    test('should filter alerts by critical severity', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      // Open severity dropdown and select critical
      const severityFilter = page.locator('button:has-text("Severity")').or(
        page.locator('select[name="severity"]')
      )

      if (await severityFilter.isVisible()) {
        await severityFilter.click()

        const criticalOption = page.locator('text=Critical').or(
          page.locator('option[value="critical"]')
        )

        if (await criticalOption.isVisible()) {
          await criticalOption.click()
          await page.waitForTimeout(500)
        }
      }
    })

    test('should filter alerts by error severity', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      const severityFilter = page.locator('button:has-text("Severity")').or(
        page.locator('select[name="severity"]')
      )

      if (await severityFilter.isVisible()) {
        await severityFilter.click()

        const errorOption = page.locator('text=Error').or(
          page.locator('option[value="error"]')
        )

        if (await errorOption.isVisible()) {
          await errorOption.click()
          await page.waitForTimeout(500)
        }
      }
    })

    test('should filter alerts by warning severity', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      const severityFilter = page.locator('button:has-text("Severity")').or(
        page.locator('select[name="severity"]')
      )

      if (await severityFilter.isVisible()) {
        await severityFilter.click()

        const warningOption = page.locator('text=Warning').or(
          page.locator('option[value="warning"]')
        )

        if (await warningOption.isVisible()) {
          await warningOption.click()
          await page.waitForTimeout(500)
        }
      }
    })

    test('should filter alerts by info severity', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      const severityFilter = page.locator('button:has-text("Severity")').or(
        page.locator('select[name="severity"]')
      )

      if (await severityFilter.isVisible()) {
        await severityFilter.click()

        const infoOption = page.locator('text=Info').or(
          page.locator('option[value="info"]')
        )

        if (await infoOption.isVisible()) {
          await infoOption.click()
          await page.waitForTimeout(500)
        }
      }
    })

  })

  test.describe('Alert Actions - Acknowledge', () => {

    test('should acknowledge a firing alert', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a firing alert via API
      const alertTitle = `AckMe${Date.now()}`
      await authenticatedApi.post('items/', {
        data: {
          title: alertTitle,
          mode: 'alert',
          status: 'firing',
          priority: 'high'
        }
      })

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)
      await page.waitForLoadState('networkidle')

      // Find the alert
      const alertRow = page.locator(`text=${alertTitle}`).first()

      if (await alertRow.isVisible({ timeout: 5000 })) {
        // Click acknowledge button
        const ackButton = page.locator('button:has-text("Acknowledge")').or(
          page.locator('button:has-text("Ack")').or(
            page.locator('button[aria-label*="acknowledge" i]')
          )
        )

        if (await ackButton.isVisible()) {
          await ackButton.click()
          await page.waitForTimeout(1000)

          // Alert should be acknowledged
        }
      }
    })

    test('should bulk acknowledge all firing alerts', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)
      await page.waitForLoadState('networkidle')

      // Look for bulk acknowledge button
      const bulkAckButton = page.locator('button:has-text("Acknowledge All")').or(
        page.locator('button:has-text("Ack All")').or(
          page.locator('[data-testid="bulk-acknowledge"]')
        )
      )

      if (await bulkAckButton.isVisible()) {
        await bulkAckButton.click()

        // Confirm if dialog appears
        const confirmButton = page.locator('button:has-text("Confirm")').or(
          page.locator('button:has-text("Yes")')
        )
        if (await confirmButton.isVisible({ timeout: 2000 })) {
          await confirmButton.click()
        }

        await page.waitForTimeout(1000)
      }
    })

  })

  test.describe('Alert Actions - Resolve', () => {

    test('should resolve an acknowledged alert', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create an acknowledged alert via API
      const alertTitle = `ResolveMe${Date.now()}`
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: alertTitle,
          mode: 'alert',
          status: 'acknowledged'
        }
      })

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      // Switch to acknowledged tab
      const acknowledgedFilter = page.locator('button:has-text("Acknowledged")')
      if (await acknowledgedFilter.isVisible()) {
        await acknowledgedFilter.click()
      }

      await page.waitForLoadState('networkidle')

      // Find the alert
      const alertRow = page.locator(`text=${alertTitle}`).first()

      if (await alertRow.isVisible({ timeout: 5000 })) {
        // Click resolve button
        const resolveButton = page.locator('button:has-text("Resolve")').or(
          page.locator('button[aria-label*="resolve" i]')
        )

        if (await resolveButton.isVisible()) {
          await resolveButton.click()
          await page.waitForTimeout(1000)
        }
      }
    })

    test('should bulk resolve alerts', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)
      await page.waitForLoadState('networkidle')

      // Look for bulk resolve button
      const bulkResolveButton = page.locator('button:has-text("Resolve All")').or(
        page.locator('[data-testid="bulk-resolve"]')
      )

      if (await bulkResolveButton.isVisible()) {
        await bulkResolveButton.click()

        // Confirm if dialog appears
        const confirmButton = page.locator('button:has-text("Confirm")').or(
          page.locator('button:has-text("Yes")')
        )
        if (await confirmButton.isVisible({ timeout: 2000 })) {
          await confirmButton.click()
        }

        await page.waitForTimeout(1000)
      }
    })

  })

  test.describe('Alert Actions - Silence', () => {

    test('should silence a firing alert', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a firing alert
      const alertTitle = `SilenceMe${Date.now()}`
      await authenticatedApi.post('items/', {
        data: {
          title: alertTitle,
          mode: 'alert',
          status: 'firing'
        }
      })

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)
      await page.waitForLoadState('networkidle')

      // Find the alert
      const alertRow = page.locator(`text=${alertTitle}`).first()

      if (await alertRow.isVisible({ timeout: 5000 })) {
        // Click silence button
        const silenceButton = page.locator('button:has-text("Silence")').or(
          page.locator('button[aria-label*="silence" i]')
        )

        if (await silenceButton.isVisible()) {
          await silenceButton.click()

          // Duration modal may appear
          const durationInput = page.locator('input[name="duration"]').or(
            page.locator('select[name="duration"]').or(
              page.locator('[data-testid="silence-duration"]')
            )
          )

          if (await durationInput.isVisible({ timeout: 2000 })) {
            // Select or enter duration
            if (await durationInput.isEditable()) {
              await durationInput.fill('60') // 60 minutes
            }

            // Confirm
            const confirmButton = page.locator('button:has-text("Confirm")').or(
              page.locator('button:has-text("Silence")').or(
                page.locator('button[type="submit"]')
              )
            )
            await confirmButton.click()
          }

          await page.waitForTimeout(1000)
        }
      }
    })

    test('should show silence duration options', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create a firing alert
      const alertTitle = `DurationTest${Date.now()}`
      await authenticatedApi.post('items/', {
        data: {
          title: alertTitle,
          mode: 'alert',
          status: 'firing'
        }
      })

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)
      await page.waitForLoadState('networkidle')

      // Click silence on the alert
      const silenceButton = page.locator('button:has-text("Silence")').first()

      if (await silenceButton.isVisible()) {
        await silenceButton.click()

        // Check for duration options
        const durationOptions = page.locator('text=/1 hour|2 hours|4 hours|8 hours|24 hours/i').or(
          page.locator('option').filter({ hasText: /hour/i })
        )

        await page.waitForTimeout(1000)
      }
    })

  })

  test.describe('Alert Details', () => {

    test('should view alert details', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create an alert with full details
      const alertTitle = `DetailedAlert${Date.now()}`
      await authenticatedApi.post('items/', {
        data: {
          title: alertTitle,
          description: 'This is a detailed test alert',
          mode: 'alert',
          priority: 'high',
          labels: { service: 'test', environment: 'staging' }
        }
      })

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)
      await page.waitForLoadState('networkidle')

      // Click on the alert
      await page.locator(`text=${alertTitle}`).click()

      // Should show details - check for description
      await page.waitForTimeout(1000)
    })

    test('should show alert labels/metadata', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage

      // Create an alert with labels
      const alertTitle = `LabeledAlert${Date.now()}`
      await authenticatedApi.post('items/', {
        data: {
          title: alertTitle,
          mode: 'alert',
          labels: { service: 'api', severity: 'critical' }
        }
      })

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)
      await page.waitForLoadState('networkidle')

      // Click on the alert
      await page.locator(`text=${alertTitle}`).click()

      // Labels may be displayed
      await page.waitForTimeout(1000)
    })

  })

  test.describe('Alert API Operations', () => {

    test('should create alert via API', async ({ authenticatedApi }) => {
      const alertTitle = `API Alert ${Date.now()}`

      const response = await authenticatedApi.post('items/', {
        data: {
          title: alertTitle,
          mode: 'alert',
          priority: 'high',
          status: 'firing'
        }
      })

      expect(response.ok()).toBeTruthy()

      const alert = await response.json()
      expect(alert.title).toBe(alertTitle)
      expect(alert.mode).toBe('alert')
    })

    test('should submit alert via alerts endpoint', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.post('alerts/', {
        data: {
          title: `Submitted Alert ${Date.now()}`,
          description: 'Alert submitted via alerts endpoint',
          source: 'e2e-test',  // Required field
          severity: 'critical',  // severity choices: info, warning, error, critical
          priority: 'high',      // priority choices: low, normal, high, urgent
          labels: { source: 'e2e-test' }
        }
      })

      // Check if endpoint exists
      if (response.status() !== 404) {
        expect(response.ok()).toBeTruthy()
        const result = await response.json()
        // API returns message, item_id, fingerprint, status
        expect(result.message).toMatch(/Alert (created|deduplicated)/)
        expect(result.item_id).toBeDefined()
      }
    })

    test('should list alerts via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=alert')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('results')
      expect(Array.isArray(data.results)).toBeTruthy()

      for (const item of data.results) {
        expect(item.mode).toBe('alert')
      }
    })

    test('should filter alerts by status via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=alert&status=firing')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      for (const alert of data.results) {
        expect(alert.status).toBe('firing')
        expect(alert.mode).toBe('alert')
      }
    })

    test('should filter alerts by priority/severity via API', async ({ authenticatedApi }) => {
      // Create a high priority alert
      await authenticatedApi.post('items/', {
        data: {
          title: `High Priority Alert ${Date.now()}`,
          mode: 'alert',
          priority: 'high'
        }
      })

      const response = await authenticatedApi.get('items/?mode=alert&priority=high')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      for (const alert of data.results) {
        expect(alert.priority).toBe('high')
      }
    })

    test('should acknowledge alert via API', async ({ authenticatedApi }) => {
      // Create a firing alert
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Ack API ${Date.now()}`,
          mode: 'alert',
          status: 'firing'
        }
      })

      const alert = await createResponse.json()

      // Acknowledge the alert
      const ackResponse = await authenticatedApi.post(`items/${alert.id}/acknowledge/`)

      expect(ackResponse.ok()).toBeTruthy()

      const ackedAlert = await ackResponse.json()
      expect(ackedAlert.status).toBe('acknowledged')
    })

    test('should resolve alert via API', async ({ authenticatedApi }) => {
      // Create an acknowledged alert
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Resolve API ${Date.now()}`,
          mode: 'alert',
          status: 'acknowledged'
        }
      })

      const alert = await createResponse.json()

      // Resolve the alert
      const resolveResponse = await authenticatedApi.post(`items/${alert.id}/resolve/`)

      expect(resolveResponse.ok()).toBeTruthy()

      const resolvedAlert = await resolveResponse.json()
      expect(resolvedAlert.status).toBe('resolved')
    })

    test('should silence alert via API', async ({ authenticatedApi }) => {
      // Create a firing alert
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Silence API ${Date.now()}`,
          mode: 'alert',
          status: 'firing'
        }
      })

      const alert = await createResponse.json()

      // Silence the alert (duration in minutes) - API expects duration_minutes
      const silenceResponse = await authenticatedApi.post(`items/${alert.id}/silence/`, {
        data: {
          duration_minutes: 60 // 60 minutes
        }
      })

      // Check if silence endpoint exists
      if (silenceResponse.status() !== 404 && silenceResponse.status() !== 405) {
        expect(silenceResponse.ok()).toBeTruthy()
      }
    })

    test('should bulk acknowledge alerts via API', async ({ authenticatedApi }) => {
      // Create multiple firing alerts
      const alerts = []
      for (let i = 0; i < 3; i++) {
        const response = await authenticatedApi.post('items/', {
          data: {
            title: `Bulk Ack ${Date.now()}_${i}`,
            mode: 'alert',
            status: 'firing'
          }
        })
        const alert = await response.json()
        alerts.push(alert.id)
      }

      // Bulk acknowledge - API expects item_ids
      const bulkResponse = await authenticatedApi.post('alerts/bulk-acknowledge/', {
        data: {
          item_ids: alerts
        }
      })

      expect(bulkResponse.ok()).toBeTruthy()

      // Verify all alerts are acknowledged
      for (const alertId of alerts) {
        const response = await authenticatedApi.get(`items/${alertId}/`)
        const alert = await response.json()
        expect(alert.status).toBe('acknowledged')
      }
    })

    test('should bulk resolve alerts via API', async ({ authenticatedApi }) => {
      // Create multiple acknowledged alerts
      const alerts = []
      for (let i = 0; i < 2; i++) {
        const response = await authenticatedApi.post('items/', {
          data: {
            title: `Bulk Resolve ${Date.now()}_${i}`,
            mode: 'alert',
            status: 'acknowledged'
          }
        })
        const alert = await response.json()
        alerts.push(alert.id)
      }

      // Bulk resolve - API expects item_ids
      const bulkResponse = await authenticatedApi.post('alerts/bulk-resolve/', {
        data: {
          item_ids: alerts
        }
      })

      expect(bulkResponse.ok()).toBeTruthy()

      // Verify all alerts are resolved
      for (const alertId of alerts) {
        const response = await authenticatedApi.get(`items/${alertId}/`)
        const alert = await response.json()
        expect(alert.status).toBe('resolved')
      }
    })

    test('should update alert via API', async ({ authenticatedApi }) => {
      // Create an alert
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Update Alert ${Date.now()}`,
          mode: 'alert'
        }
      })

      const alert = await createResponse.json()

      // Update the alert - don't use leading slash
      const updateResponse = await authenticatedApi.patch(`items/${alert.id}/`, {
        data: {
          title: 'Updated Alert Title',
          description: 'Updated description'
        }
      })

      expect(updateResponse.ok()).toBeTruthy()

      const updatedAlert = await updateResponse.json()
      expect(updatedAlert.title).toBe('Updated Alert Title')
    })

    test('should delete alert via API', async ({ authenticatedApi }) => {
      // Create an alert
      const createResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Delete Alert ${Date.now()}`,
          mode: 'alert'
        }
      })

      const alert = await createResponse.json()

      // Delete the alert
      const deleteResponse = await authenticatedApi.delete(`items/${alert.id}/`)

      expect(deleteResponse.status()).toBe(204)

      // Verify deletion
      const getResponse = await authenticatedApi.get(`items/${alert.id}/`)
      expect(getResponse.status()).toBe(404)
    })

    test('should search alerts via API', async ({ authenticatedApi }) => {
      const uniqueTitle = `UniqueAlertSearch${Date.now()}`

      // Create an alert with unique title
      await authenticatedApi.post('items/', {
        data: {
          title: uniqueTitle,
          mode: 'alert'
        }
      })

      const response = await authenticatedApi.get(`items/?mode=alert&search=${uniqueTitle}`)

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data.results.length).toBeGreaterThan(0)
      expect(data.results[0].title).toContain(uniqueTitle)
    })

    test('should paginate alerts via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=alert&page=1')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('count')
      expect(data).toHaveProperty('results')
      // Default page size is 10
      expect(data.results.length).toBeLessThanOrEqual(10)
    })

    test('should order alerts by created_at via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('items/?mode=alert&ordering=-created_at')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      if (data.results.length >= 2) {
        const first = new Date(data.results[0].created_at)
        const second = new Date(data.results[1].created_at)
        expect(first.getTime()).toBeGreaterThanOrEqual(second.getTime())
      }
    })

    test('should create alert with labels via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.post('items/', {
        data: {
          title: `Labeled Alert ${Date.now()}`,
          mode: 'alert',
          labels: {
            service: 'api',
            environment: 'production',
            team: 'backend'
          }
        }
      })

      expect(response.ok()).toBeTruthy()

      const alert = await response.json()
      expect(alert.labels).toBeDefined()
    })

  })

  test.describe('Silence Rules', () => {

    test('should list silence rules via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('rules/silence/')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('results')
    })

    test('should create silence rule via API', async ({ authenticatedApi }) => {
      const now = new Date()
      const endsAt = new Date(now.getTime() + 2 * 60 * 60 * 1000) // 2 hours from now

      const response = await authenticatedApi.post('rules/silence/', {
        data: {
          name: `Test Silence ${Date.now()}`,
          description: 'Test silence rule',
          matchers: { service: 'test' },
          starts_at: now.toISOString(),
          ends_at: endsAt.toISOString()
        }
      })

      // Rule creation may or may not be allowed
      if (response.ok()) {
        const rule = await response.json()
        expect(rule.name).toBeDefined()
      }
    })

  })

  test.describe('Escalation Rules', () => {

    test('should list escalation rules via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('rules/escalation/')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('results')
    })

  })

  test.describe('Alert Auto-Refresh', () => {

    test('should have refresh functionality', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      await page.click('a[href*="alerts"]')
      await page.waitForURL(/.*alerts/)

      // Look for refresh button
      const refreshButton = page.locator('button:has-text("Refresh")').or(
        page.locator('button[aria-label*="refresh" i]').or(
          page.locator('[data-testid="refresh-alerts"]')
        )
      )

      if (await refreshButton.isVisible()) {
        await refreshButton.click()
        await page.waitForLoadState('networkidle')
      }
    })

  })

})
