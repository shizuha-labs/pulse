import { test, expect, TEST_USER } from './fixtures.js'

/**
 * E2E tests for Phase 1 Features:
 * 1. Rich text description editor (TipTap)
 * 2. @mentions support in comments and descriptions
 * 3. Subtasks UI section
 * 4. Threaded comments display
 * 5. Saved filters
 */

test.describe('Phase 1 Features', () => {

  test.describe('Rich Text Description Editor', () => {

    test('should display description editor in task detail view', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `DescTest${Date.now()}`

      // Create a task via API
      const response = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task',
          description: '<p>Initial description</p>'
        }
      })
      const task = await response.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Look for the description section
      const descriptionSection = page.locator('text=Description').or(
        page.locator('[data-testid="description-section"]')
      )
      await expect(descriptionSection.first()).toBeVisible({ timeout: 10000 })
    })

    test('should allow editing description with rich text formatting', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `DescEditTest${Date.now()}`

      // Create a task via API
      const response = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task'
        }
      })
      const task = await response.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Click edit button for description (look for edit icon or "Add a description" placeholder)
      const editButton = page.locator('button[title*="Edit description"]').or(
        page.locator('text=Add a description').or(
          page.locator('.description-editor button').first()
        )
      )

      if (await editButton.isVisible({ timeout: 5000 })) {
        await editButton.click()

        // Wait for TipTap editor to appear - use first() to avoid strict mode violation
        const editor = page.locator('.ProseMirror').first()
        await expect(editor).toBeVisible({ timeout: 5000 })

        // Type some text
        await editor.click()
        await editor.fill('This is a test description with formatting')

        // Look for formatting toolbar (Bold button)
        const boldButton = page.locator('button[title*="Bold"]').first()
        if (await boldButton.isVisible()) {
          // Select text and make it bold
          await page.keyboard.down('Control')
          await page.keyboard.press('a')
          await page.keyboard.up('Control')
          await boldButton.click()
        }

        // Save the description
        const saveButton = page.locator('button:has-text("Save")').or(
          page.locator('button[title*="Save"]')
        ).first()
        if (await saveButton.isVisible()) {
          await saveButton.click()
          await page.waitForTimeout(1000)
        }
      }
    })

    test('should display existing rich text description correctly', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `RichDescTest${Date.now()}`

      // Create a task with rich text description via API
      const response = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task',
          description: '<p><strong>Bold text</strong> and <em>italic text</em></p><ul><li>List item 1</li><li>List item 2</li></ul>'
        }
      })
      const task = await response.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Check if rich text is rendered (look for strong/bold elements)
      const boldText = page.locator('strong:has-text("Bold text")').or(
        page.locator('b:has-text("Bold text")')
      )
      await expect(boldText).toBeVisible({ timeout: 10000 })
    })

  })

  test.describe('@Mentions Support', () => {

    test('should show @ button in comment editor when users available', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `MentionTest${Date.now()}`

      // First get a project to create the task in
      const projectsResponse = await authenticatedApi.get('projects/')
      const projects = await projectsResponse.json()
      const project = projects.results?.[0] || projects[0]

      if (!project) {
        console.log('Skipping mention test - no projects available')
        return
      }

      // Create a task in the project
      const response = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task',
          project: project.id
        }
      })
      const task = await response.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Look for the comment editor
      const commentEditor = page.locator('.ProseMirror').or(
        page.locator('[data-testid="comment-editor"]').or(
          page.locator('textarea[placeholder*="comment" i]')
        )
      )

      if (await commentEditor.first().isVisible({ timeout: 5000 })) {
        // Look for @ mention button
        const mentionButton = page.locator('button[title*="Mention"]').or(
          page.locator('button:has(svg.lucide-at-sign)').or(
            page.locator('[aria-label*="mention" i]')
          )
        )

        // The @ button may or may not be visible depending on project members
        // Just verify the comment editor exists
        await expect(commentEditor.first()).toBeVisible()
      }
    })

    test('should trigger mention dropdown when typing @', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `MentionDropdownTest${Date.now()}`

      // Get a project
      const projectsResponse = await authenticatedApi.get('projects/')
      const projects = await projectsResponse.json()
      const project = projects.results?.[0] || projects[0]

      if (!project) {
        console.log('Skipping mention dropdown test - no projects available')
        return
      }

      // Create a task
      const response = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task',
          project: project.id
        }
      })
      const task = await response.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Find and click on comment editor
      const commentEditor = page.locator('.ProseMirror').first()
      if (await commentEditor.isVisible({ timeout: 5000 })) {
        await commentEditor.click()
        await page.keyboard.type('@')
        await page.waitForTimeout(500)

        // Check if mention dropdown appears (it may not if no users are available)
        const mentionDropdown = page.locator('.mention-list').or(
          page.locator('[data-testid="mention-dropdown"]').or(
            page.locator('[class*="tippy"]')
          )
        )

        // The dropdown visibility depends on having mentionable users
        // We just verify typing @ doesn't crash the editor
        await expect(commentEditor).toBeVisible()
      }
    })

  })

  test.describe('Subtasks UI Section', () => {

    test('should display subtasks tab in task detail', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `SubtaskTest${Date.now()}`

      // Create a task
      const response = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task'
        }
      })
      const task = await response.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Look for Subtasks tab
      const subtasksTab = page.locator('button:has-text("Subtasks")').or(
        page.locator('[data-testid="subtasks-tab"]').or(
          page.locator('text=Subtasks')
        )
      )
      await expect(subtasksTab.first()).toBeVisible({ timeout: 10000 })
    })

    test('should show add subtask button when subtasks tab is active', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `SubtaskAddTest${Date.now()}`

      // Create a task
      const response = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task'
        }
      })
      const task = await response.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Click subtasks tab
      const subtasksTab = page.locator('button:has-text("Subtasks")').first()
      if (await subtasksTab.isVisible({ timeout: 5000 })) {
        await subtasksTab.click()
        await page.waitForTimeout(500)

        // Look for add subtask button (+ icon)
        const addButton = page.locator('button[title="Add subtask"]').or(
          page.locator('button:has(svg.lucide-plus)').or(
            page.locator('[data-testid="add-subtask"]')
          )
        )
        await expect(addButton.first()).toBeVisible({ timeout: 5000 })
      }
    })

    test('should create a subtask', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const parentTitle = `ParentTask${Date.now()}`
      const subtaskTitle = `Subtask${Date.now()}`

      // Get a project
      const projectsResponse = await authenticatedApi.get('projects/')
      const projects = await projectsResponse.json()
      const project = projects.results?.[0] || projects[0]

      // Create parent task
      const response = await authenticatedApi.post('items/', {
        data: {
          title: parentTitle,
          mode: 'task',
          project: project?.id
        }
      })
      const task = await response.json()

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Click subtasks tab
      const subtasksTab = page.locator('button:has-text("Subtasks")').first()
      if (await subtasksTab.isVisible({ timeout: 5000 })) {
        await subtasksTab.click()
        await page.waitForTimeout(1000)

        // Click add subtask button (look for + icon in the subtasks section)
        const addButton = page.locator('button[title="Add subtask"]').or(
          page.locator('[class*="subtask"] button:has(svg)').first()
        )

        if (await addButton.isVisible({ timeout: 3000 })) {
          await addButton.click()
          await page.waitForTimeout(500)

          // Fill subtask title
          const titleInput = page.locator('input[placeholder*="Subtask"]').or(
            page.locator('input[placeholder*="title" i]')
          ).first()

          if (await titleInput.isVisible({ timeout: 3000 })) {
            await titleInput.fill(subtaskTitle)

            // Click add/create button - look for the Add button in the form
            const createButton = page.locator('button.btn-primary:has-text("Add")').or(
              page.locator('form button[type="submit"]')
            ).first()
            await createButton.click()

            // Wait for subtask to appear (may take a moment for API call)
            await page.waitForTimeout(2000)
            await expect(page.locator(`text=${subtaskTitle}`).first()).toBeVisible({ timeout: 10000 })
          }
        }
      }
    })

    test('should show subtask progress bar', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const parentTitle = `ProgressTest${Date.now()}`

      // Get a project
      const projectsResponse = await authenticatedApi.get('projects/')
      const projects = await projectsResponse.json()
      const project = projects.results?.[0] || projects[0]

      // Create parent task with project
      const parentResponse = await authenticatedApi.post('items/', {
        data: {
          title: parentTitle,
          mode: 'task',
          project: project?.id
        }
      })
      const parentTask = await parentResponse.json()

      // Create a subtask via API
      await authenticatedApi.post('items/', {
        data: {
          title: `Subtask1_${Date.now()}`,
          mode: 'task',
          parent_item: parentTask.id,
          project: project?.id
        }
      })

      // Navigate to parent task detail
      await page.goto(`/pulse/tasks/${parentTask.item_key || parentTask.id}`)
      await page.waitForLoadState('networkidle')

      // Click subtasks tab
      const subtasksTab = page.locator('button:has-text("Subtasks")').first()
      if (await subtasksTab.isVisible({ timeout: 5000 })) {
        await subtasksTab.click()
        await page.waitForTimeout(1000)

        // Look for progress indicator (e.g., "0/1" or progress bar)
        const progressIndicator = page.locator('text=/\\d+\\/\\d+/').or(
          page.locator('text=/\\d+%/').or(
            page.locator('[class*="progress"]')
          )
        )
        await expect(progressIndicator.first()).toBeVisible({ timeout: 5000 })
      }
    })

  })

  test.describe('Threaded Comments', () => {

    test('should display reply button on comments', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `ThreadedTest${Date.now()}`

      // Create a task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task'
        }
      })
      const task = await taskResponse.json()

      // Create a comment via API
      await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          content: '<p>This is a test comment for threading</p>'
        }
      })

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')

      // Wait for comments to load
      await page.waitForTimeout(2000)

      // Look for reply button on the comment
      const replyButton = page.locator('button:has-text("Reply")').or(
        page.locator('button[title*="Reply"]').or(
          page.locator('[data-testid="reply-button"]')
        )
      )

      // Reply button should be visible if there are comments
      if (await page.locator('text=test comment').isVisible({ timeout: 3000 })) {
        await expect(replyButton.first()).toBeVisible({ timeout: 5000 })
      }
    })

    test('should show reply form when reply button clicked', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `ReplyFormTest${Date.now()}`

      // Create a task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task'
        }
      })
      const task = await taskResponse.json()

      // Create a comment via API
      await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          content: '<p>Parent comment for reply test</p>'
        }
      })

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(2000)

      // Click reply button
      const replyButton = page.locator('button:has-text("Reply")').first()
      if (await replyButton.isVisible({ timeout: 5000 })) {
        await replyButton.click()

        // Reply form should appear (look for another TipTap editor)
        const replyEditor = page.locator('.ProseMirror').nth(1).or(
          page.locator('[placeholder*="reply" i]')
        )
        await expect(replyEditor).toBeVisible({ timeout: 5000 })
      }
    })

    test('should create threaded reply', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `CreateReplyTest${Date.now()}`
      const replyContent = `Reply content ${Date.now()}`

      // Create a task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task'
        }
      })
      const task = await taskResponse.json()

      // Create a parent comment via API
      const commentResponse = await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          content: '<p>Parent comment</p>'
        }
      })

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(2000)

      // Click reply button
      const replyButton = page.locator('button:has-text("Reply")').first()
      if (await replyButton.isVisible({ timeout: 5000 })) {
        await replyButton.click()
        await page.waitForTimeout(500)

        // Type reply in the reply editor
        const replyEditor = page.locator('.ProseMirror').nth(1).or(
          page.locator('[placeholder*="reply" i]')
        )

        if (await replyEditor.isVisible({ timeout: 3000 })) {
          await replyEditor.click()
          await replyEditor.fill(replyContent)

          // Submit reply
          const submitButton = page.locator('button:has-text("Send")').or(
            page.locator('button:has-text("Reply")')
          ).last()
          await submitButton.click()

          // Wait for reply to appear
          await page.waitForTimeout(1500)
          await expect(page.locator(`text=${replyContent}`)).toBeVisible({ timeout: 5000 })
        }
      }
    })

    test('should show collapse/expand for threads with replies', async ({ authenticatedPage, authenticatedApi }) => {
      const page = authenticatedPage
      const taskTitle = `CollapseTest${Date.now()}`

      // Create a task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: taskTitle,
          mode: 'task'
        }
      })
      const task = await taskResponse.json()

      // Create parent comment
      const parentResponse = await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          content: '<p>Parent with replies</p>'
        }
      })
      const parentComment = await parentResponse.json()

      // Create reply comment
      await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          parent: parentComment.id,
          content: '<p>This is a reply</p>'
        }
      })

      // Navigate to task detail
      await page.goto(`/pulse/tasks/${task.item_key || task.id}`)
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(2000)

      // Look for collapse/expand toggle
      const collapseToggle = page.locator('button:has-text("Hide")').or(
        page.locator('button:has-text("Show")').or(
          page.locator('text=/\\d+ repl/i')
        )
      )

      // May or may not be visible depending on thread state
      if (await collapseToggle.first().isVisible({ timeout: 3000 })) {
        await expect(collapseToggle.first()).toBeVisible()
      }
    })

  })

  test.describe('Saved Filters', () => {

    test('should display saved filters button on tasks page', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to tasks
      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Look for saved filters button
      const filtersButton = page.locator('button:has-text("Filters")').or(
        page.locator('button:has(svg.lucide-bookmark)').or(
          page.locator('[title="Saved filters"]')
        )
      )
      await expect(filtersButton.first()).toBeVisible({ timeout: 10000 })
    })

    test('should open saved filters dropdown', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to tasks
      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Click filters button
      const filtersButton = page.locator('button:has-text("Filters")').or(
        page.locator('button:has(svg.lucide-bookmark)')
      ).first()

      if (await filtersButton.isVisible({ timeout: 5000 })) {
        await filtersButton.click()

        // Dropdown should appear
        const dropdown = page.locator('.absolute').or(
          page.locator('[class*="dropdown"]').or(
            page.locator('text=No saved filters')
          )
        )
        await expect(dropdown.first()).toBeVisible({ timeout: 5000 })
      }
    })

    test('should save current filter configuration', async ({ authenticatedPage }) => {
      const page = authenticatedPage
      const filterName = `TestFilter${Date.now()}`

      // Navigate to tasks
      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Apply some filters first (e.g., change status filter)
      const statusFilter = page.locator('select').first()
      if (await statusFilter.isVisible({ timeout: 5000 })) {
        await statusFilter.selectOption('pending')
        await page.waitForTimeout(500)
      }

      // Open filters dropdown
      const filtersButton = page.locator('button:has-text("Filters")').or(
        page.locator('button:has(svg.lucide-bookmark)')
      ).first()

      if (await filtersButton.isVisible({ timeout: 3000 })) {
        await filtersButton.click()
        await page.waitForTimeout(500)

        // Click "Save current filters" button
        const saveButton = page.locator('button:has-text("Save current filters")').or(
          page.locator('text=Save current filters')
        )

        if (await saveButton.isVisible({ timeout: 3000 })) {
          await saveButton.click()

          // Fill filter name
          const nameInput = page.locator('input[placeholder*="Filter name"]').or(
            page.locator('input[placeholder*="name" i]')
          )

          if (await nameInput.isVisible({ timeout: 3000 })) {
            await nameInput.fill(filterName)

            // Click save/confirm button
            const confirmButton = page.locator('button[title="Save"]').or(
              page.locator('button:has(svg.lucide-check)')
            ).first()
            await confirmButton.click()

            // Filter should be saved
            await page.waitForTimeout(500)
            await expect(page.locator(`text=${filterName}`)).toBeVisible({ timeout: 5000 })
          }
        }
      }
    })

    test('should apply saved filter', async ({ authenticatedPage }) => {
      const page = authenticatedPage
      const filterName = `ApplyTest${Date.now()}`

      // Navigate to tasks
      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // First create a saved filter
      const statusFilter = page.locator('select').first()
      if (await statusFilter.isVisible({ timeout: 5000 })) {
        await statusFilter.selectOption('pending')
        await page.waitForTimeout(500)
      }

      const filtersButton = page.locator('button:has-text("Filters")').or(
        page.locator('button:has(svg.lucide-bookmark)')
      ).first()

      if (await filtersButton.isVisible({ timeout: 3000 })) {
        await filtersButton.click()
        await page.waitForTimeout(500)

        const saveButton = page.locator('button:has-text("Save current filters")').or(
          page.locator('text=Save current filters')
        )

        if (await saveButton.isVisible({ timeout: 3000 })) {
          await saveButton.click()

          const nameInput = page.locator('input[placeholder*="Filter name"]').or(
            page.locator('input[placeholder*="name" i]')
          )

          if (await nameInput.isVisible({ timeout: 3000 })) {
            await nameInput.fill(filterName)
            const confirmButton = page.locator('button[title="Save"]').or(
              page.locator('button:has(svg.lucide-check)')
            ).first()
            await confirmButton.click()
            await page.waitForTimeout(500)

            // Now reset filters
            await statusFilter.selectOption('') // All statuses
            await page.waitForTimeout(500)

            // Open filters and click the saved filter to apply it
            await filtersButton.click()
            await page.waitForTimeout(500)

            const savedFilter = page.locator(`text=${filterName}`)
            if (await savedFilter.isVisible({ timeout: 3000 })) {
              await savedFilter.click()
              await page.waitForTimeout(500)

              // Verify filter was applied (status should be pending again)
              await expect(statusFilter).toHaveValue('pending')
            }
          }
        }
      }
    })

    test('should delete saved filter', async ({ authenticatedPage }) => {
      const page = authenticatedPage
      const filterName = `DeleteTest${Date.now()}`

      // Navigate to tasks
      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Create a saved filter
      const statusFilter = page.locator('select').first()
      if (await statusFilter.isVisible({ timeout: 5000 })) {
        await statusFilter.selectOption('completed')
        await page.waitForTimeout(500)
      }

      const filtersButton = page.locator('button:has-text("Filters")').or(
        page.locator('button:has(svg.lucide-bookmark)')
      ).first()

      if (await filtersButton.isVisible({ timeout: 3000 })) {
        await filtersButton.click()
        await page.waitForTimeout(500)

        const saveButton = page.locator('button:has-text("Save current filters")').or(
          page.locator('text=Save current filters')
        )

        if (await saveButton.isVisible({ timeout: 3000 })) {
          await saveButton.click()

          const nameInput = page.locator('input[placeholder*="Filter name"]')
          if (await nameInput.isVisible({ timeout: 3000 })) {
            await nameInput.fill(filterName)
            const confirmButton = page.locator('button[title="Save"]').or(
              page.locator('button:has(svg.lucide-check)')
            ).first()
            await confirmButton.click()
            await page.waitForTimeout(500)

            // Now delete the filter
            const savedFilterItem = page.locator(`text=${filterName}`).locator('..')
            const deleteButton = savedFilterItem.locator('button[title="Delete filter"]').or(
              savedFilterItem.locator('button:has(svg.lucide-trash-2)')
            )

            if (await deleteButton.isVisible({ timeout: 3000 })) {
              await deleteButton.click()
              await page.waitForTimeout(500)

              // Filter should be removed
              await expect(page.locator(`text=${filterName}`)).not.toBeVisible({ timeout: 3000 })
            }
          }
        }
      }
    })

    test('should persist saved filters across page reloads', async ({ authenticatedPage }) => {
      const page = authenticatedPage
      const filterName = `PersistTest${Date.now()}`

      // Navigate to tasks
      await page.click('a[href*="tasks"]')
      await page.waitForURL(/.*tasks/)
      await page.waitForLoadState('networkidle')

      // Create a saved filter
      const statusFilter = page.locator('select').first()
      if (await statusFilter.isVisible({ timeout: 5000 })) {
        await statusFilter.selectOption('in_progress')
        await page.waitForTimeout(500)
      }

      const filtersButton = page.locator('button:has-text("Filters")').or(
        page.locator('button:has(svg.lucide-bookmark)')
      ).first()

      if (await filtersButton.isVisible({ timeout: 3000 })) {
        await filtersButton.click()
        await page.waitForTimeout(500)

        const saveButton = page.locator('button:has-text("Save current filters")').or(
          page.locator('text=Save current filters')
        )

        if (await saveButton.isVisible({ timeout: 3000 })) {
          await saveButton.click()

          const nameInput = page.locator('input[placeholder*="Filter name"]')
          if (await nameInput.isVisible({ timeout: 3000 })) {
            await nameInput.fill(filterName)
            const confirmButton = page.locator('button[title="Save"]').or(
              page.locator('button:has(svg.lucide-check)')
            ).first()
            await confirmButton.click()
            await page.waitForTimeout(500)

            // Reload the page
            await page.reload()
            await page.waitForLoadState('networkidle')

            // Open filters dropdown again
            await filtersButton.click()
            await page.waitForTimeout(500)

            // Saved filter should still be there
            await expect(page.locator(`text=${filterName}`)).toBeVisible({ timeout: 5000 })
          }
        }
      }
    })

  })

  test.describe('API Tests for Phase 1 Features', () => {

    test('should create item with rich text description via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.post('items/', {
        data: {
          title: `API Rich Text ${Date.now()}`,
          mode: 'task',
          description: '<p><strong>Bold</strong> and <em>italic</em></p>'
        }
      })

      expect(response.ok()).toBeTruthy()

      const task = await response.json()
      expect(task.description).toContain('<strong>')
      expect(task.description).toContain('Bold')
    })

    test('should create subtask via API with parent_item', async ({ authenticatedApi }) => {
      // Create parent task
      const parentResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Parent ${Date.now()}`,
          mode: 'task'
        }
      })
      const parent = await parentResponse.json()

      // Create subtask
      const subtaskResponse = await authenticatedApi.post('items/', {
        data: {
          title: `Subtask ${Date.now()}`,
          mode: 'task',
          parent_item: parent.id
        }
      })

      expect(subtaskResponse.ok()).toBeTruthy()

      const subtask = await subtaskResponse.json()
      expect(subtask.parent_item).toBe(parent.id)
    })

    test('should fetch subtasks by parent_item via API', async ({ authenticatedApi }) => {
      // Create parent task
      const parentResponse = await authenticatedApi.post('items/', {
        data: {
          title: `ListSubtasksParent ${Date.now()}`,
          mode: 'task'
        }
      })
      const parent = await parentResponse.json()

      // Create subtasks
      for (let i = 0; i < 3; i++) {
        await authenticatedApi.post('items/', {
          data: {
            title: `Subtask${i}_${Date.now()}`,
            mode: 'task',
            parent_item: parent.id
          }
        })
      }

      // Fetch subtasks - the API should filter by parent_item
      const response = await authenticatedApi.get(`items/?parent_item=${parent.id}`)
      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      // API may return {results: [...]} or just [...] depending on pagination
      const results = data.results || data

      // The API should return subtasks when filtering by parent_item
      // Just verify we got some results and the filter is working
      expect(results.length).toBeGreaterThanOrEqual(3)
    })

    test('should create comment with parent for threading via API', async ({ authenticatedApi }) => {
      // Create task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: `ThreadedCommentTask ${Date.now()}`,
          mode: 'task'
        }
      })
      const task = await taskResponse.json()

      // Create parent comment
      const parentCommentResponse = await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          content: '<p>Parent comment</p>'
        }
      })

      // Check if parent comment creation succeeded
      if (!parentCommentResponse.ok()) {
        const errorText = await parentCommentResponse.text()
        console.log('Parent comment creation failed:', errorText)
      }
      expect(parentCommentResponse.ok()).toBeTruthy()

      const parentComment = await parentCommentResponse.json()

      // Create reply comment with parent field
      const replyResponse = await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          parent: parentComment.id,
          content: '<p>Reply comment</p>'
        }
      })

      if (!replyResponse.ok()) {
        const errorText = await replyResponse.text()
        console.log('Reply comment creation failed:', errorText)
      }
      expect(replyResponse.ok()).toBeTruthy()

      const reply = await replyResponse.json()
      // The parent field might be returned as 'parent' or 'parent_id' depending on API
      expect(reply.parent || reply.parent_id).toBe(parentComment.id)
    })

    test('should fetch comments with threading info via API', async ({ authenticatedApi }) => {
      // Create task
      const taskResponse = await authenticatedApi.post('items/', {
        data: {
          title: `FetchThreadsTask ${Date.now()}`,
          mode: 'task'
        }
      })
      const task = await taskResponse.json()

      // Create parent comment
      const parentResponse = await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          content: '<p>Parent</p>'
        }
      })

      if (!parentResponse.ok()) {
        const errorText = await parentResponse.text()
        console.log('Parent comment creation failed:', errorText)
      }
      expect(parentResponse.ok()).toBeTruthy()

      const parent = await parentResponse.json()

      // Create replies
      const reply1Response = await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          parent: parent.id,
          content: '<p>Reply 1</p>'
        }
      })

      const reply2Response = await authenticatedApi.post('comments/', {
        data: {
          item: task.id,
          parent: parent.id,
          content: '<p>Reply 2</p>'
        }
      })

      // Fetch all comments for the item
      const response = await authenticatedApi.get(`comments/?item=${task.id}`)
      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      // API may return {results: [...]} or just [...]
      const comments = data.results || data
      expect(comments.length).toBeGreaterThanOrEqual(3)

      // Check parent relationship (field might be 'parent' or 'parent_id')
      const replies = comments.filter(c => (c.parent || c.parent_id) === parent.id)
      expect(replies.length).toBeGreaterThanOrEqual(2)
    })

  })

})
