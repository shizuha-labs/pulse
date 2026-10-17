// GHSA-253c-mchw-3w2r: exercise Pulse's real Markdown renderer, not a mocked parser.
import assert from 'node:assert/strict'
import { Worker } from 'node:worker_threads'
import { renderMarkdown } from '../../src/utils/markdown.js'
assert.match(renderMarkdown('Contact reader@example.com'), /href="mailto:reader@example.com"/)
assert.match(renderMarkdown('**Readable** text'), /<strong>Readable<\/strong>/)
const renderer = new URL('../../src/utils/markdown.js', import.meta.url).href
for (const payload of ['reader@example.com\n'.repeat(20000), 'unregistered://'.repeat(40000)]) {
  const worker = new Worker(`
    const { parentPort, workerData } = require('node:worker_threads');
    import(workerData.renderer).then(({ renderMarkdown }) => {
      const html = renderMarkdown(workerData.payload);
      if (!html.length) throw new Error('renderer dropped nonempty input');
      parentPort.postMessage('passed');
    });
  `, { eval: true, execArgv: [], workerData: { renderer, payload } })
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Pulse Markdown rendering exceeded 5 second budget')), 5000)
      worker.once('message', value => { clearTimeout(timer); assert.equal(value, 'passed'); resolve() })
      worker.once('error', error => { clearTimeout(timer); reject(error) })
      worker.once('exit', code => { if (code) { clearTimeout(timer); reject(new Error(`renderer worker exited ${code}`)) } })
    })
  } finally { await worker.terminate() }
}
console.log('Pulse actual Markdown renderer bounded linkify regressions passed')
