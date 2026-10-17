import assert from 'node:assert/strict'
import { workflowPendingMessage } from '../../src/utils/workflowOperation.js'
assert.match(workflowPendingMessage({ status: 202, data: { pending: true } }), /remains in its current status/)
assert.equal(workflowPendingMessage({ status: 200, data: { status: 'done' } }), null)
assert.equal(workflowPendingMessage({ status: 202, data: { pending: false } }), null)
console.log('workflow operation receipt: 3 passed')
