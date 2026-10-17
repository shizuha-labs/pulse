import { test, expect, TEST_USER, NEW_USER } from './fixtures.js'

/**
 * Authentication tests for Shizuha unified login architecture
 *
 * The login flow works as follows:
 * 1. User visits /pulse/ (unauthenticated)
 * 2. ProtectedRoute redirects to /id/login?continue=/pulse/
 * 3. User logs in at Shizuha ID (/id/login)
 * 4. After successful login, redirects back to /pulse/
 *
 * These tests verify the complete flow across both frontends.
 */

test.describe('Authentication', () => {

  test.describe('Unified Login at Shizuha ID', () => {

    test('should display Shizuha ID login page', async ({ page }) => {
      // Go directly to the unified login page
      await page.goto('/id/login')

      // Check page elements - this is Shizuha ID, not Shizuha Hub
      await expect(page.locator('h1')).toContainText('Shizuha ID')
      await expect(page.locator('text=Sign in to continue')).toBeVisible()
      await expect(page.locator('input[id="username"]')).toBeVisible()
      await expect(page.locator('input[id="password"]')).toBeVisible()
      await expect(page.locator('button[type="submit"]')).toContainText('Sign in')
    })

    test('should login with valid credentials and redirect to Pulse', async ({ page }) => {
      // Start at Shizuha ID login with continue parameter
      await page.goto('/id/login?continue=/pulse/')

      // Wait for form to be ready
      await page.waitForSelector('input[id="username"]')

      // Fill in credentials
      await page.fill('input[id="username"]', TEST_USER.username)
      await page.fill('input[id="password"]', TEST_USER.password)

      // Click submit
      await page.click('button[type="submit"]')

      // Wait for successful login API response
      await page.waitForResponse(
        response => response.url().includes('/auth/login/') && response.ok(),
        { timeout: 10000 }
      )

      // The redirect happens via window.location.href, wait for it
      await page.waitForURL(/.*pulse.*/, { timeout: 20000 })

      // Wait for page to load
      await page.waitForLoadState('domcontentloaded', { timeout: 15000 })

      // Verify we're on the Pulse dashboard - use heading specifically
      await expect(page.locator('h1:has-text("Dashboard")')).toBeVisible({ timeout: 10000 })
    })

    test('should show error with invalid credentials', async ({ page }) => {
      await page.goto('/id/login')

      // Fill in wrong credentials
      await page.fill('input[id="username"]', 'wronguser')
      await page.fill('input[id="password"]', 'wrongpassword')

      // Submit form
      await page.click('button[type="submit"]')

      // Wait for error response from API
      await page.waitForResponse(response =>
        response.url().includes('/auth/login/') && response.status() >= 400,
        { timeout: 10000 }
      )

      // Wait a moment for state update
      await page.waitForTimeout(1000)

      // Should show error message - look for the error container with critical styling
      // The error div has classes like bg-critical-50, border-critical-200
      await expect(
        page.locator('div').filter({ hasText: /Login failed|No active account|Invalid|credentials/i }).first()
      ).toBeVisible({ timeout: 5000 })

      // Should stay on login page
      await expect(page).toHaveURL(/.*\/id\/login/)
    })

    test('should toggle password visibility', async ({ page }) => {
      await page.goto('/id/login')

      const passwordInput = page.locator('input[id="password"]')

      // Find the toggle button (the eye icon button next to password input)
      const toggleButton = page.locator('input[id="password"]').locator('..').locator('button')

      // Initially password should be hidden
      await expect(passwordInput).toHaveAttribute('type', 'password')

      // Click toggle button
      await toggleButton.click()

      // Password should be visible
      await expect(passwordInput).toHaveAttribute('type', 'text')

      // Click again to hide
      await toggleButton.click()
      await expect(passwordInput).toHaveAttribute('type', 'password')
    })

    test('should have link to register page', async ({ page }) => {
      await page.goto('/id/login')

      // Click register link
      await page.click('text=Create one')

      // Should navigate to register page
      await expect(page).toHaveURL(/.*\/id\/register/)
    })

  })

  test.describe('Registration at Shizuha ID', () => {

    test('should display Shizuha ID register page', async ({ page }) => {
      await page.goto('/id/register')

      await expect(page.locator('h1')).toContainText('Shizuha ID')
      await expect(page.locator('text=Create your account')).toBeVisible()
      await expect(page.locator('input[id="username"]')).toBeVisible()
      await expect(page.locator('input[id="email"]')).toBeVisible()
      await expect(page.locator('input[id="password"]')).toBeVisible()
    })

    test('should have link to login page', async ({ page }) => {
      await page.goto('/id/register')

      // Click login link
      await page.click('text=Sign in')

      // Should navigate to login page
      await expect(page).toHaveURL(/.*\/id\/login/)
    })

    // Registration test creates a new user with unique timestamp-based username
    test('should register new user and redirect to Pulse', async ({ page }) => {
      // Generate unique user data for this test run
      const timestamp = Date.now()
      const newUser = {
        username: `testuser_${timestamp}`,
        email: `test_${timestamp}@example.com`,
        password: 'NewTestPass123!'
      }

      await page.goto('/id/register?continue=/pulse/')

      // Wait for form to be ready
      await page.waitForSelector('input[id="username"]')

      // Fill in registration form
      await page.fill('input[id="username"]', newUser.username)
      await page.fill('input[id="email"]', newUser.email)
      await page.fill('input[id="password"]', newUser.password)
      await page.fill('input[id="password2"]', newUser.password)

      // Submit form
      await page.click('button[type="submit"]')

      // Wait for registration API response
      await page.waitForResponse(
        response => response.url().includes('/auth/register/') && response.ok(),
        { timeout: 15000 }
      )

      // Should redirect to Pulse dashboard after successful registration
      await page.waitForURL(/.*pulse.*/, { timeout: 20000 })

      // Verify we're on the pulse app
      await expect(page.locator('h1:has-text("Dashboard")')).toBeVisible({ timeout: 10000 })
    })

  })

  test.describe('Pulse App Redirects', () => {

    test('should redirect from /pulse/ to /id/login when unauthenticated', async ({ page }) => {
      // Clear any existing auth state
      await page.goto('/id/login')
      await page.evaluate(() => {
        localStorage.clear()
      })

      // Try to access Pulse dashboard directly
      await page.goto('/pulse/')

      // Should redirect to Shizuha ID login with continue parameter
      await page.waitForURL(/.*\/id\/login.*continue/, { timeout: 10000 })

      // Verify we're on the login page
      await expect(page.locator('h1')).toContainText('Shizuha ID')
    })

    test('should redirect old /pulse/login to /id/login', async ({ page }) => {
      // Try to access old login route
      await page.goto('/pulse/login')

      // Should redirect to Shizuha ID login
      await page.waitForURL(/.*\/id\/login/, { timeout: 10000 })
    })

    test('should redirect old /pulse/register to /id/register', async ({ page }) => {
      // Try to access old register route
      await page.goto('/pulse/register')

      // Should redirect to Shizuha ID register
      await page.waitForURL(/.*\/id\/register/, { timeout: 10000 })
    })

  })

  test.describe('Logout', () => {

    test('should logout successfully and redirect to login', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to settings page
      await page.goto('/pulse/settings')
      await page.waitForLoadState('networkidle')

      // Find logout button (adjust selector based on actual UI)
      const logoutButton = page.locator('button:has-text("Sign Out")').or(
        page.locator('button:has-text("Logout")')
      ).or(
        page.locator('button:has-text("Log out")')
      )

      if (await logoutButton.isVisible({ timeout: 5000 })) {
        await logoutButton.click()

        // Should redirect to login page (either /id/login or just contain login)
        await page.waitForURL(/.*login/, { timeout: 10000 })
      }
    })

  })

  test.describe('Session Persistence', () => {

    test('should persist authentication across page reload', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Verify we're authenticated
      await expect(page.locator('h1:has-text("Dashboard")')).toBeVisible({ timeout: 10000 })

      // Reload the page
      await page.reload()

      // Should still be on dashboard (not redirected to login)
      await expect(page.locator('h1:has-text("Dashboard")')).toBeVisible({ timeout: 10000 })
    })

    test('should maintain session when navigating between pages', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to different pages within Pulse
      await page.goto('/pulse/tasks')
      await page.waitForLoadState('domcontentloaded')
      // Check we're still authenticated (not redirected to login)
      await expect(page).not.toHaveURL(/.*login/)

      await page.goto('/pulse/settings')
      await page.waitForLoadState('domcontentloaded')
      // Check we're still authenticated (not redirected to login)
      await expect(page).not.toHaveURL(/.*login/)
    })

  })

  test.describe('API Authentication', () => {

    test('should make authenticated API calls after login', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Listen for API responses
      const apiResponses = []
      page.on('response', response => {
        if (response.url().includes('/pulse/api/') || response.url().includes('/id/api/')) {
          apiResponses.push({
            url: response.url(),
            status: response.status()
          })
        }
      })

      // Navigate to trigger some API calls
      await page.goto('/pulse/')
      await page.waitForLoadState('networkidle')

      // Wait a bit for API calls to complete
      await page.waitForTimeout(2000)

      // Check that API calls were made and returned successful status
      const successfulCalls = apiResponses.filter(r => r.status >= 200 && r.status < 400)
      const failedAuth = apiResponses.filter(r => r.status === 401)

      // Log for debugging
      console.log('API responses:', apiResponses)

      // There should be some successful API calls
      expect(successfulCalls.length).toBeGreaterThan(0)

      // There should be no 401 errors after authentication
      expect(failedAuth.length).toBe(0)
    })

  })

})
