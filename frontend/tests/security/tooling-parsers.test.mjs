// Installed tooling API regressions for GHSA-q2hr-2g5m-vwhr and GHSA-2883-xcg3-v3hh.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { Worker } from 'node:worker_threads'
import yaml from 'js-yaml'
const require = createRequire(import.meta.url)
const minimatch = require('minimatch')
assert.deepEqual(minimatch.braceExpand('src/{one,two}.js'), ['src/one.js', 'src/two.js'])
assert.deepEqual(yaml.load('name: build\nitems: [one, two]'), { name: 'build', items: ['one', 'two'] })
assert.throws(() => yaml.load('empty: &empty {}\nvalue:\n  <<: [*empty, *empty, *empty, *empty, *empty, *empty]\n', {
  maxTotalMergeKeys: 5,
}), /maxTotalMergeKeys/, 'empty merge sources must consume the configured parsing budget')
const worker = new Worker(`
  const { parentPort, workerData } = require('node:worker_threads');
  const minimatch = require(workerData.minimatch);
  const result = minimatch.braceExpand('{a}' + '}'.repeat(65000) + ',z}');
  if (!result.length) throw Error('glob parser dropped input');
  parentPort.postMessage('passed');
`, { eval: true, workerData: { minimatch: require.resolve('minimatch') } })
try {
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('installed glob parser exceeded 5 second budget')), 5000)
    worker.once('message', value => { clearTimeout(timer); assert.equal(value, 'passed'); resolve() })
    worker.once('error', error => { clearTimeout(timer); reject(error) })
    worker.once('exit', code => { if (code) { clearTimeout(timer); reject(new Error(`glob worker exited ${code}`)) } })
  })
} finally { await worker.terminate() }
console.log('Installed glob and YAML parsing boundary regressions passed')
