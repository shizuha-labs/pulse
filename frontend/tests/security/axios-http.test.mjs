// Real HTTP requests through the installed consumer dependency, without mocks.
import assert from 'node:assert/strict'
import http from 'node:http'
import { once } from 'node:events'
import axios from 'axios'

const observations = []
const server = http.createServer((request, response) => {
  observations.push({ method: request.method, canary: request.headers['x-inherited-canary'], own: request.headers['x-own-header'] })
  request.resume()
  response.writeHead(200, { 'Content-Type': 'application/json' })
  response.end('{"ok":true}')
})
server.listen(0, '127.0.0.1')
await once(server, 'listening')
const url = `http://127.0.0.1:${server.address().port}/consumer`
try {
  for (const adapter of ['http', 'fetch']) {
    // Node's native fetch itself reads inherited RequestInit values; test the
    // Axios pollution boundary through HTTP, then its clean fetch path.
    if (adapter === 'http') Object.defineProperty(Object.prototype, 'method', { value: 'DELETE', writable: true, configurable: true })
    if (adapter === 'http') Object.defineProperty(Object.prototype, 'headers', { value: { 'X-Inherited-Canary': 'blocked' }, writable: true, configurable: true })
    try {
      await axios.request({ url, adapter, proxy: false, timeout: 2000 })
      await axios({ url, adapter, method: 'POST', data: 'explicit body', headers: { 'X-Own-Header': 'preserved' }, proxy: false, timeout: 2000 })
    } finally {
      delete Object.prototype.method
      delete Object.prototype.headers
    }
  }
  assert.deepEqual(observations.map(value => value.method), ['GET', 'POST', 'GET', 'POST'])
  assert.ok(observations.every(value => value.canary === undefined), 'inherited headers reached the server')
  assert.equal(observations[1].own, 'preserved')
  assert.equal(observations[3].own, 'preserved')
  console.log('Axios HTTP pollution regression and clean native-fetch requests passed')
} finally {
  delete Object.prototype.method
  delete Object.prototype.headers
  server.closeAllConnections()
  await new Promise(resolve => server.close(resolve))
}
