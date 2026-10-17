/**
 * Pulse SPA session policy.
 *
 * Only a definite HTTP 401 means the session is dead. Overlay/nginx 5xx,
 * 408, 429, and network errors (no response) must NOT clear tokens or
 * redirect to /id/login — that is the operator re-login loop when a Pulse
 * replica or nginx lands on a flapping node (i9-ws overlay 504s / DB
 * statement timeouts).
 */

export function isDefiniteAuthFailure(error) {
  return error?.response?.status === 401
}

export function restoreStoredUser(raw) {
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    return parsed
  } catch {
    return null
  }
}
