import { CircleStop } from 'lucide-react'
import { useClock } from '../hooks/useNow.js'
import { runProgress, triggerLabel } from '../utils/labels.js'
import { formatDuration } from '../utils/time.js'

/** Live view of the running scrape with a progress bar and Stop button. */
export default function ProgressBanner({ run, onStop, stopping }) {
  const now = useClock(1000)
  if (!run) return null

  const { total, current, fraction } = runProgress(run)
  const cancelling = stopping || run.cancel_requested
  const step = current !== null && total ? `Query ${Math.min(current, total)} of ${total}` : total ? `${total} queries` : ''

  return (
    <div className="progress-banner" role="status">
      <div className="progress-head">
        <div className="spinner" />
        <div className="progress-text">
          <strong>{cancelling ? 'Stopping scrape…' : `${triggerLabel(run.trigger)} scrape running`}</strong>
          <span className="progress-message">{run.message || 'Starting…'}</span>
        </div>
        <div className="progress-counts">
          <span>
            <b>{run.new_jobs ?? 0}</b> new
          </span>
          <span>
            <b>{run.updated_jobs ?? 0}</b> updated
          </span>
          <span>
            <b>{run.jobs_found ?? 0}</b> found
          </span>
          <span className="muted">{formatDuration(run.started_at, null, now)}</span>
        </div>
        {onStop && (
          <button className="btn-danger" onClick={onStop} disabled={cancelling}>
            <CircleStop size={15} /> {cancelling ? 'Stopping' : 'Stop'}
          </button>
        )}
      </div>
      <div className={`progress-track ${fraction === null ? 'indeterminate' : ''}`}>
        <div className="progress-fill" style={fraction === null ? undefined : { width: `${Math.round(fraction * 100)}%` }} />
      </div>
      {step && (
        <div className="progress-foot">
          <span>{step}</span>
        </div>
      )}
    </div>
  )
}
