/**
 * Pulse SPA must not treat overlay/backend 5xx as logout.
 * Run: node frontend/tests/unit/authSession.test.mjs
 */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const src = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../src/utils/authSession.js',
)
const { isDefiniteAuthFailure, restoreStoredUser } = await import(pathToFileURL(src).href)

assert.equal(isDefiniteAuthFailure({ response: { status: 401 } }), true)
assert.equal(isDefiniteAuthFailure({ response: { status: 500 } }), false)
assert.equal(isDefiniteAuthFailure({ response: { status: 502 } }), false)
assert.equal(isDefiniteAuthFailure({ response: { status: 504 } }), false)
assert.equal(isDefiniteAuthFailure({ response: { status: 429 } }), false)
assert.equal(isDefiniteAuthFailure({ message: 'Network Error' }), false)
assert.equal(isDefiniteAuthFailure({}), false)
assert.equal(isDefiniteAuthFailure(null), false)

const user = { id: 1, email: 'operator@example.test' }
assert.deepEqual(restoreStoredUser(JSON.stringify(user)), user)
assert.equal(restoreStoredUser('not-json'), null)
assert.equal(restoreStoredUser(''), null)
assert.equal(restoreStoredUser(null), null)

console.log('authSession tests passed')
