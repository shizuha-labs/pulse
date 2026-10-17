import { test as base } from '@playwright/test'

/**
 * Test fixtures for Shizuha Pulse E2E tests
 * Provides authenticated user context and test data
 *
 * With the unified login architecture:
 * - Login happens at /id/login (Shizuha ID)
 * - After login, user is redirected to /pulse/ (Shizuha Pulse)
 * - Tokens are stored in localStorage with shared keys
 */

// Test user credentials
export const TEST_USER = {
  username: 'testuser',
  password: 'TestPass123',
  email: 'testuser@example.com'
}

// New user for registration tests
export const NEW_USER = {
  username: `testuser_${Date.now()}`,
  email: `test_${Date.now()}@example.com`,
  password: 'NewTestPass123!'
}

// Extend base test with authentication fixture
export const test = base.extend({
  // Authenticated page fixture - logs in via Shizuha ID
  authenticatedPage: async ({ page }, use) => {
    // Go to Shizuha ID login page with continue parameter
    await page.goto('/id/login?continue=/pulse/')

    // Wait for form to be ready
    await page.waitForSelector('input[id="username"]', { timeout: 10000 })

    // Fill in credentials
    await page.fill('input[id="username"]', TEST_USER.username)
    await page.fill('input[id="password"]', TEST_USER.password)

    // Click submit
    await page.click('button[type="submit"]')

    // Wait for successful login API response
    await page.waitForResponse(
      response => response.url().includes('/auth/login/') && response.ok(),
      { timeout: 15000 }
    )

    // The redirect happens via window.location.href, wait for it
    await page.waitForURL(/.*pulse.*/, { timeout: 30000 })

    // Wait for page to fully load
    await page.waitForLoadState('networkidle', { timeout: 20000 })

    // Wait a bit for React to hydrate
    await page.waitForTimeout(1000)

    // Verify we're on the pulse app - multiple possible indicators
    const pulseIndicators = [
      'h1:has-text("Dashboard")',
      '[data-testid="dashboard"]',
      'text=Tasks',
      'text=Alerts',
      'text=Actions',
      'nav',
      '.sidebar',
      '[class*="dashboard"]'
    ]

    // Wait for any of these indicators
    let found = false
    for (const selector of pulseIndicators) {
      try {
        await page.waitForSelector(selector, { timeout: 2000 })
        found = true
        break
      } catch {
        // Try next selector
      }
    }

    if (!found) {
      // As fallback, just ensure we're not on login page anymore
      const url = page.url()
      if (url.includes('/id/login')) {
        throw new Error('Still on login page after authentication')
      }
    }

    // Use the authenticated page
    await use(page)
  },

  // API context for direct API testing (unauthenticated)
  apiContext: async ({ playwright }, use) => {
    const baseUrl = process.env.BASE_URL || 'http://shizuha-nginx'
    const apiContext = await playwright.request.newContext({
      baseURL: process.env.API_URL || `${baseUrl}/pulse/api`,
      extraHTTPHeaders: {
        'Content-Type': 'application/json',
      },
    })

    await use(apiContext)

    await apiContext.dispose()
  },

  // Authenticated API context - uses Shizuha ID for login
  authenticatedApi: async ({ playwright }, use) => {
    // Note: baseURL must end with slash for relative paths to work correctly
    const baseUrl = process.env.BASE_URL || 'http://shizuha-nginx'
    const pulseApiUrl = process.env.API_URL || `${baseUrl}/pulse/api/`
    const idApiUrl = process.env.ID_API_URL || `${baseUrl}/id/api/`

    const apiContext = await playwright.request.newContext({
      baseURL: idApiUrl,
      extraHTTPHeaders: {
        'Content-Type': 'application/json',
      },
    })

    // Login via Shizuha ID to get token
    // Note: Don't use leading slash - it replaces the base path
    const loginResponse = await apiContext.post('auth/login/', {
      data: {
        username: TEST_USER.username,
        password: TEST_USER.password
      }
    })

    // Check if login was successful
    if (!loginResponse.ok()) {
      const text = await loginResponse.text()
      throw new Error(`Login failed: ${loginResponse.status()} - ${text.substring(0, 200)}`)
    }

    const { tokens } = await loginResponse.json()

    // Create new context with auth header for Pulse API
    const authContext = await playwright.request.newContext({
      baseURL: pulseApiUrl,
      extraHTTPHeaders: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${tokens.access}`
      },
    })

    await use(authContext)

    await apiContext.dispose()
    await authContext.dispose()
  }
})

export { expect } from '@playwright/test'
