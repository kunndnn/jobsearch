import { useCallback, useState } from 'react'
import { Briefcase, Play } from 'lucide-react'
import JobCard from '../components/JobCard.jsx'
import JobDetailModal from '../components/JobDetailModal.jsx'
import JobFilters from '../components/JobFilters.jsx'
import StatsBar from '../components/StatsBar.jsx'
import { DEFAULT_FILTERS, useJobs } from '../hooks/useJobs.js'
import { setJobStatus } from '../api.js'
import { SORT_OPTIONS, UNTRACK_NOTES_WARNING, appStatusLabel } from '../utils/labels.js'

function EmptyState({ hasAnyJobs, noBoards, onReset, onRunNow, isScraping }) {
  if (!hasAnyJobs) {
    return (
      <div className="empty-state">
        <Briefcase size={40} />
        <h3>No jobs stored yet</h3>
        <p>Run a scrape to pull fresh openings that match your profile.</p>
        <button className="btn-scrape" onClick={onRunNow} disabled={isScraping}>
          <Play size={16} /> {isScraping ? 'Scrape running…' : 'Run scrape now'}
        </button>
      </div>
    )
  }
  return (
    <div className="empty-state">
      <Briefcase size={40} />
      <h3>{noBoards ? 'No boards selected' : 'No jobs match your filters'}</h3>
      <p>{noBoards ? 'Pick at least one board above.' : 'Try lowering the minimum match or clearing a filter.'}</p>
      <button className="pill-btn" onClick={onReset}>
        Reset filters
      </button>
    </div>
  )
}

/** Drops a role/location filter whose pill disappeared after a profile edit (else the list is silently empty). */
function pruneFilters(filters, profile) {
  if (!profile) return filters
  const tracks = ['all', 'other', ...(profile.target_tracks || []).map((t) => t.id)]
  const locations = ['all', ...(profile.preferred_locations || [])]
  const patch = {}
  if (!tracks.includes(filters.roleTrack)) patch.roleTrack = 'all'
  if (!locations.includes(filters.location)) patch.location = 'all'
  return Object.keys(patch).length ? { ...filters, ...patch } : filters
}

export default function JobsView({ profile, settings, stats, refreshKey, onJobsChanged, onRunNow, isScraping, notify }) {
  const [filters, setFilters] = useState(DEFAULT_FILTERS)
  const [filtersProfile, setFiltersProfile] = useState(profile)
  if (profile !== filtersProfile) {
    setFiltersProfile(profile)
    const pruned = pruneFilters(filters, profile)
    if (pruned !== filters) setFilters(pruned)
  }
  const [selected, setSelected] = useState(null)
  const { jobs, total, totalUnfiltered, loading, loadingMore, error, hasMore, loadMore, reload, patchJob } = useJobs(
    filters,
    refreshKey,
  )

  const trackTitle = useCallback(
    (id) => (profile?.target_tracks || []).find((t) => t.id === id)?.title || (id === 'other' ? 'Other' : id || '—'),
    [profile],
  )

  const applyStatus = useCallback(
    (id, status, notes) => {
      const patch = { app_status: status, app_notes: status === 'none' ? '' : notes }
      patchJob(id, patch)
      setSelected((s) => (s && s.id === id ? { ...s, ...patch } : s))
      if (filters.status !== 'all') reload()
      onJobsChanged()
    },
    [patchJob, reload, filters.status, onJobsChanged],
  )

  const quickStatus = useCallback(
    async (job, status) => {
      if (status === 'none' && job.app_notes && !window.confirm(UNTRACK_NOTES_WARNING)) return
      try {
        const res = await setJobStatus(job.id, status, job.app_notes || '')
        applyStatus(job.id, res.status ?? status, job.app_notes || '')
        notify({ type: 'success', title: appStatusLabel(res.status ?? status), message: job.title })
      } catch (err) {
        notify({ type: 'error', title: 'Could not update status', message: err.message })
      }
    },
    [applyStatus, notify],
  )

  const closeModal = useCallback(() => setSelected(null), [])
  const selectedJob = selected ? jobs.find((j) => j.id === selected.id) || selected : null
  const sortLabel = SORT_OPTIONS.find((s) => s.id === filters.sortBy)?.label.toLowerCase()
  const noBoards = Array.isArray(filters.sites) && filters.sites.length === 0

  return (
    <>
      <StatsBar stats={stats} />

      <JobFilters
        filters={filters}
        onChange={setFilters}
        onReset={() => setFilters(DEFAULT_FILTERS)}
        profile={profile}
        availableSites={settings?.available_sites}
      />

      <section>
        <div className="feed-header">
          <h2 className="section-title">
            {loading && !jobs.length ? 'Loading jobs…' : `${total.toLocaleString()} jobs`}
            {!loading && totalUnfiltered > 0 && <span className="muted"> of {totalUnfiltered.toLocaleString()} stored</span>}
            {loading && jobs.length > 0 && <span className="spinner inline" aria-label="Updating" />}
          </h2>
          <span className="muted small">Sorted by {sortLabel} · scores reflect your profile</span>
        </div>

        {error && (
          <div className="form-error">
            Could not load jobs: {error.message}{' '}
            <button className="link-btn" onClick={reload}>
              Retry
            </button>
          </div>
        )}

        {loading && !jobs.length ? (
          <div className="loading-block">
            <div className="spinner large" />
            <p>Loading matching jobs…</p>
          </div>
        ) : !jobs.length && !error ? (
          <EmptyState
            hasAnyJobs={totalUnfiltered > 0}
            noBoards={noBoards}
            onReset={() => setFilters(DEFAULT_FILTERS)}
            onRunNow={onRunNow}
            isScraping={isScraping}
          />
        ) : (
          <div className={`jobs-feed ${loading ? 'is-updating' : ''}`}>
            {jobs.map((job) => (
              <JobCard key={job.id} job={job} onOpen={setSelected} onQuickStatus={quickStatus} />
            ))}
          </div>
        )}

        {hasMore && !loading && (
          <div className="load-more">
            <button className="pill-btn" onClick={loadMore} disabled={loadingMore}>
              {loadingMore ? 'Loading…' : `Load more (${(total - jobs.length).toLocaleString()} left)`}
            </button>
          </div>
        )}
      </section>

      {selectedJob && (
        <JobDetailModal
          key={selectedJob.id}
          job={selectedJob}
          trackTitle={trackTitle}
          onClose={closeModal}
          onStatusSaved={applyStatus}
          notify={notify}
        />
      )}
    </>
  )
}
