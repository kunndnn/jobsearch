import { useCallback, useEffect, useRef, useState } from 'react'
import { getJobs } from '../api.js'

const DEBOUNCE_MS = 300
export const PAGE_SIZE = 50

export const DEFAULT_FILTERS = {
  search: '',
  roleTrack: 'all',
  location: 'all',
  sites: 'all',
  sortBy: 'match_score',
  minScore: 0,
  remoteOnly: false,
  excludeSenior: true,
  status: 'all',
  newOnly: false,
}

/** Filters -> GET /api/jobs query string (without page). `sites` is 'all' or an array; [] means no boards. */
function buildQuery(filters) {
  const params = new URLSearchParams()
  const search = filters.search.trim()
  if (search) params.set('search', search)
  params.set('role_track', filters.roleTrack)
  params.set('location', filters.location)
  params.set('sites', filters.sites === 'all' ? 'all' : filters.sites.join(','))
  params.set('sort_by', filters.sortBy)
  params.set('min_score', String(filters.minScore))
  params.set('remote_only', String(filters.remoteOnly))
  params.set('exclude_senior', String(filters.excludeSenior))
  params.set('status', filters.status)
  params.set('new_only', String(filters.newOnly))
  params.set('page_size', String(PAGE_SIZE))
  return params.toString()
}

const EMPTY = { jobs: [], total: 0, totalUnfiltered: 0, page: 0, loading: true, loadingMore: false, error: null }

/** Appends a page, skipping ids already listed (rows can shift between pages while scraping). */
function mergePage(existing, incoming) {
  const seen = new Set(existing.map((j) => j.id))
  const merged = [...existing]
  for (const job of incoming) {
    if (!seen.has(job.id)) {
      seen.add(job.id)
      merged.push(job)
    }
  }
  return merged
}

/**
 * Debounced, abortable job list. Only the newest request may write state, so a slow
 * response for old filters can never overwrite a newer one. `refreshKey` changes force a reload.
 */
export function useJobs(filters, refreshKey = 0) {
  const query = buildQuery(filters)
  const [state, setState] = useState(EMPTY)
  const controllerRef = useRef(null)
  const seqRef = useRef(0)

  const fetchPage = useCallback(async (q, page) => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const seq = ++seqRef.current
    const append = page > 1
    setState((s) => ({ ...s, loading: !append, loadingMore: append, error: null }))
    try {
      const data = await getJobs(`${q}&page=${page}`, controller.signal)
      if (seq !== seqRef.current) return
      setState((s) => ({
        jobs: mergePage(append ? s.jobs : [], data.jobs || []),
        total: data.total ?? 0,
        totalUnfiltered: data.total_unfiltered ?? 0,
        page: data.page ?? page,
        loading: false,
        loadingMore: false,
        error: null,
      }))
    } catch (err) {
      if (err.name === 'AbortError' || seq !== seqRef.current) return
      setState((s) => ({ ...s, loading: false, loadingMore: false, error: err }))
    }
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => fetchPage(query, 1), DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query, refreshKey, fetchPage])

  useEffect(() => () => controllerRef.current?.abort(), [])

  const loadMore = useCallback(() => fetchPage(query, state.page + 1), [fetchPage, query, state.page])

  const reload = useCallback(() => fetchPage(query, 1), [fetchPage, query])

  const patchJob = useCallback((id, patch) => {
    setState((s) => ({ ...s, jobs: s.jobs.map((j) => (j.id === id ? { ...j, ...patch } : j)) }))
  }, [])

  return {
    ...state,
    hasMore: state.jobs.length < state.total,
    loadMore,
    reload,
    patchJob,
  }
}
