import { test, expect } from './fixtures.js'

test.describe('App Switcher', () => {

  test('should open app switcher popover on click', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Find the app switcher button (waffle icon) - use first() since it appears in both header and sidebar
    const appSwitcherButton = page.locator('aside button[aria-label="Switch apps"]')
    await expect(appSwitcherButton).toBeVisible()

    // Click to open
    await appSwitcherButton.click()

    // Popover should be visible with "Shizuha Apps" header
    await expect(page.locator('text=Shizuha Apps')).toBeVisible()

    // All apps in the popover should be visible - check inside the popover by looking for the button with app name and description
    await expect(page.getByRole('button', { name: /Pulse.*Task Management/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /Notes.*Personal Notes/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /Wiki.*Team Documentation/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /Inventory.*Asset Management/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /Mail.*Email/i })).toBeVisible()
    await expect(page.getByRole('button', { name: /Account.*Profile/i })).toBeVisible()
  })

  test('should close app switcher on Escape key', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Open app switcher - use first() since it appears in both header and sidebar
    const appSwitcherButton = page.locator('aside button[aria-label="Switch apps"]')
    await appSwitcherButton.click()
    await expect(page.locator('text=Shizuha Apps')).toBeVisible()

    // Press Escape
    await page.keyboard.press('Escape')

    // Popover should be hidden
    await expect(page.locator('text=Shizuha Apps')).not.toBeVisible()
  })

  test('should close app switcher on click outside', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Open app switcher - use first() since it appears in both header and sidebar
    const appSwitcherButton = page.locator('aside button[aria-label="Switch apps"]')
    await appSwitcherButton.click()
    await expect(page.locator('text=Shizuha Apps')).toBeVisible()

    // Click outside (on the main content area)
    await page.locator('main').click()

    // Popover should be hidden
    await expect(page.locator('text=Shizuha Apps')).not.toBeVisible()
  })

  test('should highlight current app (Pulse)', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Open app switcher - use first() since it appears in both header and sidebar
    const appSwitcherButton = page.locator('aside button[aria-label="Switch apps"]')
    await appSwitcherButton.click()

    // The Pulse button should have a ring (highlighted as current)
    const pulseButton = page.locator('button').filter({ hasText: 'Pulse' }).filter({ hasText: 'Task Management' })
    await expect(pulseButton).toBeVisible()

    // Check for ring class indicating current app
    const hasRing = await pulseButton.evaluate(el => el.classList.contains('ring-2'))
    expect(hasRing).toBeTruthy()
  })

  test('should navigate to Notes app', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Open app switcher - use first() since it appears in both header and sidebar
    const appSwitcherButton = page.locator('aside button[aria-label="Switch apps"]')
    await appSwitcherButton.click()

    // Click on Notes
    const notesButton = page.locator('button').filter({ hasText: 'Notes' }).filter({ hasText: 'Personal Notes' })
    await notesButton.click()

    // Should navigate to /notes/
    await page.waitForURL(/.*notes.*/, { timeout: 10000 })
    expect(page.url()).toContain('/notes/')
  })

  test('should navigate to Account app', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    // Open app switcher - use first() since it appears in both header and sidebar
    const appSwitcherButton = page.locator('aside button[aria-label="Switch apps"]')
    await appSwitcherButton.click()

    // Wait for popover to be fully visible
    await expect(page.locator('text=Shizuha Apps')).toBeVisible()

    // Find and click Account button
    const accountButton = page.locator('button').filter({ hasText: 'Account' }).filter({ hasText: 'Profile & Settings' })
    await expect(accountButton).toBeVisible()

    // Log button state for debugging
    const buttonText = await accountButton.innerText()
    console.log('Account button text:', buttonText)

    // Click on Account
    await accountButton.click()

    // Should navigate to /id/
    await page.waitForURL(/.*\/id\/.*/, { timeout: 10000 })
    expect(page.url()).toContain('/id/')
  })

  test('should stay on current app when clicking current app button', async ({ authenticatedPage }) => {
    const page = authenticatedPage

    const initialUrl = page.url()

    // Open app switcher - use first() since it appears in both header and sidebar
    const appSwitcherButton = page.locator('aside button[aria-label="Switch apps"]')
    await appSwitcherButton.click()

    // Click on Pulse (current app)
    const pulseButton = page.locator('button').filter({ hasText: 'Pulse' }).filter({ hasText: 'Task Management' })
    await pulseButton.click()

    // Should stay on same URL (popover just closes)
    await page.waitForTimeout(500)
    expect(page.url()).toBe(initialUrl)

    // Popover should be closed
    await expect(page.locator('text=Shizuha Apps')).not.toBeVisible()
  })

})
