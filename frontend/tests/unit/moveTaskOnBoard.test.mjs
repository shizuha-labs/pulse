import assert from 'node:assert/strict'
import {
  BOARD_MOVE_NEEDS_TASK_PAGE,
  moveTaskToColumn,
  pickBoardTransition,
  taskAlreadyInColumn,
} from '../../src/utils/moveTaskOnBoard.js'
import {
  formatApiError,
  organizationIdForNewTask,
  restoredOrganization,
} from '../../src/utils/createTaskRequest.js'

const start = {
  id: '53dd5a5a-d416-40a1-8aa5-068140f05225',
  name: 'Start Progress',
  to_status: 'in_progress',
  screen_fields: [],
}
const cancel = {
  id: '31510f8f-7766-40ea-a413-19118dd6e94c',
  name: 'Cancel',
  to_status: 'cancelled',
  screen_fields: [{ field: 'reason_category', required: true }],
}
const transitions = { current_status: 'todo', transitions: [start, cancel] }

assert.equal(pickBoardTransition(transitions, 'in_progress').id, start.id)
assert.equal(pickBoardTransition(transitions, 'done'), null)
assert.equal(pickBoardTransition({ transitions: [cancel] }, 'cancelled'), null)

const todoItem = { id: 1, status: 'todo', status_category: 'todo', workflow_name: 'Simple Workflow' }
assert.equal(taskAlreadyInColumn(todoItem, 'todo'), true)
assert.equal(taskAlreadyInColumn(todoItem, 'pending'), true)
assert.equal(taskAlreadyInColumn(todoItem, 'in_progress'), false)

const calls = []
const api = {
  async getAvailableTransitions(id) {
    calls.push(['transitions', id])
    return { data: transitions }
  },
  async executeTransition(id, body) {
    calls.push(['execute', id, body])
    return { data: { status: body.to_status } }
  },
  async update(id, body) {
    calls.push(['update', id, body])
    return { data: body }
  },
}

const moved = await moveTaskToColumn(todoItem, 'in_progress', api)
assert.equal(moved.via, 'transition')
assert.equal(moved.status, 'in_progress')
assert.deepEqual(calls, [
  ['transitions', 1],
  ['execute', 1, { to_status: 'in_progress', transition_id: start.id }],
])

const plain = await moveTaskToColumn(
  { id: 2, status: 'pending' },
  'todo',
  api,
)
assert.equal(plain.reason, 'same-column')

const patched = await moveTaskToColumn(
  { id: 3, status: 'pending' },
  'in_progress',
  api,
)
assert.equal(patched.via, 'patch')
assert.deepEqual(calls.at(-1), ['update', 3, { status: 'in_progress' }])

await assert.rejects(
  () => moveTaskToColumn(todoItem, 'done', api),
  (error) => error.message === BOARD_MOVE_NEEDS_TASK_PAGE,
)

const sole = [{ id: 909, slug: 'personal-1001', name: "Raunak's Workspace" }]
assert.equal(organizationIdForNewTask(null, sole), 909)
assert.equal(organizationIdForNewTask({ id: 1 }, sole), 1)
assert.equal(organizationIdForNewTask(null, [{ id: 1 }, { id: 2 }]), null)
const personalAndCompany = [
  { id: 914, slug: 'legbizz-consultants-llp', is_personal: false, name: 'LEGBIZZ CONSULTANTS LLP' },
  { id: 909, slug: 'personal-1001', is_personal: true, name: "Raunak's Workspace" },
]
assert.equal(organizationIdForNewTask(null, personalAndCompany), 909)
assert.equal(organizationIdForNewTask({ id: 914 }, personalAndCompany), 914)
assert.equal(organizationIdForNewTask(null, [
  { id: 914, slug: 'legbizz-consultants-llp' },
  { id: 909, slug: 'personal-1001' },
]), 909)
assert.equal(organizationIdForNewTask(null, [
  { id: 1, slug: 'alpha', is_personal: false },
  { id: 2, slug: 'beta', is_personal: false },
]), null)
assert.deepEqual(restoredOrganization(sole, null), { org: sole[0], persistSlug: 'personal-1001' })
assert.deepEqual(restoredOrganization(sole, '__all__'), { org: null, persistSlug: null })
assert.equal(
  formatApiError({ response: { data: { project: ['A task must belong to an organization project.'] } } }, 'fallback'),
  'A task must belong to an organization project.',
)

console.log('move task on board: passed')
