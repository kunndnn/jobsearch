import RelativeTime from './RelativeTime.jsx'
import { useNow } from '../hooks/useNow.js'
import { runStatusLabel, siteLabel, triggerLabel } from '../utils/labels.js'
import { formatDuration } from '../utils/time.js'

function RunStatusPill({ status }) {
  return <span className={`run-status status-${status}`}>{runStatusLabel(status)}</span>
}

function BoardErrors({ errors }) {
  const entries = Object.entries(errors || {})
  if (!entries.length) return <span className="muted">—</span>
  return (
    <div className="run-errors">
      {entries.map(([site, message]) => (
        <span key={site} className="error-chip" title={message}>
          <b>{siteLabel(site)}:</b> {message}
        </span>
      ))}
    </div>
  )
}

export default function RunHistory({ runs }) {
  const now = useNow()
  if (!runs?.length) {
    return <p className="muted empty-note">No runs yet. Use “Run now” or enable the schedule.</p>
  }
  return (
    <div className="table-wrap">
      <table className="run-table">
        <thead>
          <tr>
            <th>Started</th>
            <th>Trigger</th>
            <th>Status</th>
            <th className="num">New</th>
            <th className="num">Updated</th>
            <th className="num">Found</th>
            <th>Duration</th>
            <th>Board errors</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td data-label="Started">
                <RelativeTime value={run.started_at} />
              </td>
              <td data-label="Trigger">{triggerLabel(run.trigger)}</td>
              <td data-label="Status">
                <RunStatusPill status={run.status} />
                {run.message && (
                  <div className="run-message" title={run.message}>
                    {run.message}
                  </div>
                )}
              </td>
              <td data-label="New" className="num">
                {run.new_jobs ?? 0}
              </td>
              <td data-label="Updated" className="num">
                {run.updated_jobs ?? 0}
              </td>
              <td data-label="Found" className="num">
                {run.jobs_found ?? 0}
              </td>
              <td data-label="Duration">
                {run.status === 'running' || run.finished_at ? formatDuration(run.started_at, run.finished_at, now) : '—'}
              </td>
              <td data-label="Errors">
                <BoardErrors errors={run.errors} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
