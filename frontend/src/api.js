// Every HTTP call to the backend goes through this module (spec section 8).

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null, unreachable = false } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.unreachable = unreachable
  }
}

function formatIssue(issue) {
  if (typeof issue === 'string') return issue
  if (!issue || typeof issue !== 'object') return String(issue)
  const loc = Array.isArray(issue.loc) ? issue.loc.filter((part) => part !== 'body').join('.') : ''
  const msg = issue.msg || JSON.stringify(issue)
  return loc ? `${loc}: ${msg}` : msg
}

/** Human-readable lines for a FastAPI `detail` (string, object or 422 issue list). */
export function detailLines(detail) {
  if (detail == null || detail === '') return []
  if (Array.isArray(detail)) return detail.map(formatIssue)
  return [formatIssue(detail)]
}

async function readJson(res) {
  const type = res.headers.get('content-type') || ''
  if (!type.includes('json')) return undefined
  try {
    return await res.json()
  } catch (err) {
    if (err.name === 'AbortError') throw err
    return undefined
  }
}

async function request(path, { method = 'GET', body, signal } = {}) {
  let res
  try {
    res = await fetch(path, {
      method,
      signal,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    throw new ApiError('Cannot reach the JobSpy server', { unreachable: true })
  }

  const data = await readJson(res)
  if (!res.ok) {
    const detail = data?.detail ?? null
    const unreachable = (res.status >= 502 && res.status <= 504) || (res.status >= 500 && data === undefined)
    const message = detailLines(detail).join('; ') || `Request failed (${res.status})`
    throw new ApiError(message, { status: res.status, detail, unreachable })
  }
  if (data === undefined) {
    throw new ApiError(`Unexpected non-JSON response from ${path}`, { status: res.status })
  }
  return data
}

const jobPath = (id) => `/api/jobs/${encodeURIComponent(id)}`

export const getProfile = (signal) => request('/api/profile', { signal })

export const saveProfile = (profile) => request('/api/profile', { method: 'PUT', body: profile })

export const getSettings = (signal) => request('/api/settings', { signal })

/** patch: `{ scrape?: {...}, schedule?: {...} }` */
export const saveSettings = (patch) => request('/api/settings', { method: 'PUT', body: patch })

export const startScrape = (overrides = {}) => request('/api/scrape', { method: 'POST', body: overrides })

export const stopScrape = () => request('/api/scrape/stop', { method: 'POST' })

export const getScrapeStatus = (signal) => request('/api/scrape/status', { signal })

/** query: a URLSearchParams string built by useJobs. */
export const getJobs = (query, signal) => request(`/api/jobs?${query}`, { signal })

export const getJob = (id, signal) => request(jobPath(id), { signal })

export const setJobStatus = (id, status, notes) =>
  request(`${jobPath(id)}/status`, {
    method: 'POST',
    body: notes === undefined ? { status } : { status, notes },
  })

export const getStats = (signal) => request('/api/stats', { signal })
