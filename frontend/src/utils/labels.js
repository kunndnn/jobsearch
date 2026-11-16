export const TABS = [
  { id: 'jobs', label: 'Jobs' },
  { id: 'automation', label: 'Automation' },
  { id: 'profile', label: 'Profile' },
]

export const APP_STATUSES = [
  { id: 'none', label: 'Not tracked' },
  { id: 'saved', label: 'Saved' },
  { id: 'applied', label: 'Applied' },
  { id: 'interviewing', label: 'Interviewing' },
  { id: 'rejected', label: 'Rejected' },
]

export const UNTRACK_NOTES_WARNING = 'Setting this job to “Not tracked” deletes its notes. Continue?'

export const SORT_OPTIONS = [
  { id: 'match_score', label: 'Best match' },
  { id: 'exp_asc', label: 'Experience: low → high' },
  { id: 'exp_desc', label: 'Experience: high → low' },
  { id: 'date', label: 'Newest posted' },
  { id: 'salary', label: 'Highest salary' },
  { id: 'found', label: 'Recently found' },
]

export const INTERVAL_PRESETS = [
  { label: '30m', minutes: 30 },
  { label: '1h', minutes: 60 },
  { label: '3h', minutes: 180 },
  { label: '6h', minutes: 360 },
  { label: '12h', minutes: 720 },
  { label: '24h', minutes: 1440 },
]

export const MAX_INTERVAL_MINUTES = 10080

export const WEIGHTED_SKILL_CATEGORIES = ['core_backend', 'ai_llm', 'frontend', 'devops_cloud']

export const TRACK_PRIORITIES = ['highest', 'high', 'medium', 'low']

const TRIGGER_LABELS = {
  manual: 'Manual',
  schedule: 'Scheduled',
  startup: 'Startup',
  cli: 'CLI',
}

const RUN_STATUS_LABELS = {
  running: 'Running',
  completed: 'Completed',
  partial: 'Partial',
  failed: 'Failed',
  interrupted: 'Interrupted',
  cancelled: 'Cancelled',
}

const SITE_LABELS = {
  linkedin: 'LinkedIn',
  naukri: 'Naukri',
  indeed: 'Indeed',
  glassdoor: 'Glassdoor',
  google: 'Google',
  zip_recruiter: 'ZipRecruiter',
}

export const triggerLabel = (trigger) => TRIGGER_LABELS[trigger] || trigger || '—'

export const runStatusLabel = (status) => RUN_STATUS_LABELS[status] || status || '—'

export const siteLabel = (site) => SITE_LABELS[String(site || '').toLowerCase()] || site || 'Board'

export const scoreClass = (score) => (score >= 80 ? 'match-high' : score >= 65 ? 'match-mid' : 'match-low')

export const boardClass = (site) => `board-badge board-${String(site || '').toLowerCase()}`

export const appStatusLabel = (status) => APP_STATUSES.find((s) => s.id === status)?.label || status

/** Toast payload for a run that just finished. */
export function runFinishedToast(run) {
  const who = run.trigger === 'manual' ? 'Scrape' : `${triggerLabel(run.trigger)} scrape`
  const counts = `${run.new_jobs ?? 0} new · ${run.updated_jobs ?? 0} updated · ${run.jobs_found ?? 0} found`
  const boards = Object.keys(run.errors || {})
  switch (run.status) {
    case 'completed':
      return { type: 'success', title: `${who} completed`, message: counts }
    case 'partial':
      return {
        type: 'warning',
        title: `${who} finished with errors`,
        message: `${counts}. Failed boards: ${boards.join(', ') || 'see run history'}`,
      }
    case 'failed':
      return { type: 'error', title: `${who} failed`, message: run.message || 'Every board returned an error.' }
    case 'cancelled':
      return { type: 'info', title: `${who} cancelled`, message: counts }
    case 'interrupted':
      return { type: 'warning', title: `${who} interrupted`, message: run.message || counts }
    default:
      return { type: 'info', title: `${who} ${runStatusLabel(run.status).toLowerCase()}`, message: counts }
  }
}

/**
 * Progress of a running scrape. The backend's `current_query` is the 1-based query in
 * progress (0 before the first one), so `current - 1` queries are complete.
 * `fraction` is null (indeterminate bar) until the first query has finished.
 */
export function runProgress(run) {
  const total = Number(run?.total_queries) || 0
  const current = Number.isInteger(run?.current_query) && run.current_query > 0 ? run.current_query : null
  const completed = current === null ? 0 : Math.min(current - 1, total)
  const fraction = total > 0 && completed > 0 ? completed / total : null
  return { total, current, fraction }
}
