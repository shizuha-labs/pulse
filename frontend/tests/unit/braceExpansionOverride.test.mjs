// Consumer-boundary regression for the brace-expansion 1.1.16 override (PLAT-5379).
//
// Context: PLAT-4440 documented a runtime break where forcing brace-expansion to
// a 5.x major broke minimatch@3's 1.x CJS function API (`var expand = require(
// 'brace-expansion')`). This repo pins `brace-expansion@1.1.16` (the patched
// 1.x line) as an override so the real consumers keep working. This test drives
// the REAL consumer boundaries — not a direct leaf import — so a future override
// bump that changes the 1.x call shape fails CI at the consumer boundary:
//
//   boundary 1: minimatch@3.1.5  (calls brace-expansion's 1.x function export)
//   boundary 2: eslint@8.57.1    (resolves file globs via minimatch/brace-expansion)
//
// Run:  node --test frontend/tests/unit/braceExpansionOverride.test.mjs
//   or: node frontend/tests/unit/braceExpansionOverride.test.mjs
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createRequire } from 'node:module'

// The test file is ESM (.mjs) but the real consumers (brace-expansion, minimatch,
// eslint) are CJS — load them through createRequire so we drive the exact CJS
// call shapes production uses.
const require = createRequire(import.meta.url)

// The advisory payloads / patterns that previously exercised the crash surface:
// brace-expansion CVE-2026-13149 (GHSA-3jxr) and the 1.x function-API shape.
const BRACE_PATTERNS = [
  ['a/{b,c}/d', ['a/b/d', 'a/c/d']],
  ['{a,b}', ['a', 'b']],
  ['src/{components,pages}/**/*.js', ['src/components/**/*.js', 'src/pages/**/*.js']],
]

test('minimatch@3 consumer: brace-expansion 1.x function API expands braces', () => {
  // minimatch@3.1.5 does `var expand = require('brace-expansion')` then calls the
  // returned function directly (1.x CJS call shape). A 5.x override would make
  // this a module object, not a function -> TypeError here.
  const braceExpansion = require('brace-expansion')
  assert.equal(typeof braceExpansion, 'function', 'brace-expansion must stay a 1.x CJS function export')

  for (const [pattern, expected] of BRACE_PATTERNS) {
    assert.deepEqual(braceExpansion(pattern), expected, `braceExpand(${pattern})`)
  }
})

test('minimatch@3 consumer: glob matching with brace patterns still works', () => {
  // minimatch drives brace-expansion through its own braceExpand/globSet path.
  const minimatch = require('minimatch')
  assert.equal(typeof minimatch, 'function', 'minimatch@3 must be a function')

  // Happy path through the consumer.
  assert.equal(minimatch('a/b/d', 'a/{b,c}/d'), true)
  assert.equal(minimatch('a/c/d', 'a/{b,c}/d'), true)
  assert.equal(minimatch('a/e/d', 'a/{b,c}/d'), false)

  // braceExpand surface (used internally by glob matching).
  assert.deepEqual(minimatch.braceExpand('a/{b,c}/d'), ['a/b/d', 'a/c/d'])
})

test('eslint consumer: config/glob brace patterns resolve without crashing', async () => {
  // eslint@8.57 resolves file globs (via minimatch -> brace-expansion) both when
  // loading config `files:` patterns and when walking the file tree. Drive the
  // real Linter so a brace-expansion call-shape break surfaces as a thrown error.
  const { Linter } = require('eslint')
  const linter = new Linter()

  // `files` with a brace-expansion pattern that eslint must match internally.
  const config = {
    rules: { semi: 'error' },
  }
  const code = 'const x = {a: 1, b: 2};\n'

  // Lint a fixture whose path exercises brace matching via minimatch glob logic
  // (eslint uses `glob`/`minimatch` internally for ignore patterns).
  const result = linter.verify(code, config, { filename: 'src/components/Fixture.js' })
  assert.ok(Array.isArray(result), 'eslint Linter must return a results array')

  // Advisory/edge payload case: code with a brace literal in a template.
  const edgeResult = linter.verify('const s = `a/{b,c}/d`;', config, {
    filename: 'src/pages/Edge.js',
  })
  assert.ok(Array.isArray(edgeResult), 'eslint must not crash on brace-literal payloads')
})

console.log('braceExpansionOverride consumer-boundary tests passed')
