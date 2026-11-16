import { useEffect, useRef, useState } from 'react'
import { Building, ExternalLink, MapPin, Save, X, Zap } from 'lucide-react'
import DescriptionText from './DescriptionText.jsx'
import RelativeTime from './RelativeTime.jsx'
import { JobBadges, SkillChips } from './JobCard.jsx'
import { getJob, setJobStatus } from '../api.js'
import { APP_STATUSES, UNTRACK_NOTES_WARNING, scoreClass } from '../utils/labels.js'

const isHttp = (url) => typeof url === 'string' && /^https?:\/\//i.test(url)

function DetailRow({ label, children }) {
  return (
    <div className="detail-row">
      <span className="detail-label">{label}</span>
      <span className="detail-value">{children}</span>
    </div>
  )
}

/** Fetches GET /api/jobs/{id}; the list summary is shown until the detail arrives. */
function useJobDetail(id) {
  const [result, setResult] = useState({ id: null, detail: null, error: null })
  useEffect(() => {
    const controller = new AbortController()
    getJob(id, controller.signal)
      .then((detail) => setResult({ id, detail, error: null }))
      .catch((err) => {
        if (err.name !== 'AbortError') setResult({ id, detail: null, error: err })
      })
    return () => controller.abort()
  }, [id])
  return result.id === id ? result : { id, detail: null, error: null }
}

export default function JobDetailModal({ job: summary, trackTitle, onClose, onStatusSaved, notify }) {
  const { detail, error } = useJobDetail(summary.id)
  const job = { ...detail, ...summary }
  const [status, setStatus] = useState(summary.app_status || 'none')
  const [notes, setNotes] = useState(summary.app_notes || '')
  const [saving, setSaving] = useState(false)
  const pressedOverlay = useRef(false)
  // The backend keeps notes only on an application row, and "none" deletes that row.
  const untracked = status === 'none'
  const notesToSave = untracked ? '' : notes
  const dirty = status !== (summary.app_status || 'none') || notesToSave !== (summary.app_notes || '')

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    document.body.classList.add('modal-open')
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.classList.remove('modal-open')
    }
  }, [onClose])

  const save = async () => {
    if (untracked && summary.app_notes && !window.confirm(UNTRACK_NOTES_WARNING)) return
    setSaving(true)
    try {
      const res = await setJobStatus(job.id, status, notesToSave)
      onStatusSaved(job.id, res.status ?? status, notesToSave)
      if (untracked) setNotes('')
      notify({ type: 'success', title: 'Tracking updated', message: `${job.title} · ${res.status ?? status}` })
    } catch (err) {
      notify({ type: 'error', title: 'Could not update status', message: err.message })
    } finally {
      setSaving(false)
    }
  }

  const applyUrl = job.job_url_direct || job.job_url

  // A drag (e.g. selecting text) that starts or ends inside the dialog must not close it,
  // even though the browser then delivers the click to the overlay.
  const onOverlayPointer = (e) => {
    pressedOverlay.current = e.target === e.currentTarget && (e.type === 'mousedown' || pressedOverlay.current)
  }

  return (
    <div
      className="modal-overlay"
      onMouseDown={onOverlayPointer}
      onMouseUp={onOverlayPointer}
      onClick={(e) => pressedOverlay.current && e.target === e.currentTarget && onClose()}
    >
      <div className="modal-content" role="dialog" aria-modal="true" aria-labelledby="job-modal-title">
        <div className="modal-header">
          <div>
            <h3 className="modal-title" id="job-modal-title">
              {job.title}
            </h3>
            <div className="modal-subtitle">
              <Building size={14} /> {job.company || 'Company not listed'}
              <MapPin size={14} /> {job.location || 'Location not listed'}
            </div>
          </div>
          <button className="btn-close" onClick={onClose} aria-label="Close">
            <X size={20} />
          </button>
        </div>

        <div className="modal-badges">
          <span className={`match-badge ${scoreClass(job.match_score ?? 0)}`}>
            <Zap size={14} /> {job.match_score ?? 0}% match with your profile
          </span>
          <JobBadges job={job} />
        </div>

        <div className="detail-grid">
          <DetailRow label="Salary">{job.salary_display || 'Not disclosed'}</DetailRow>
          <DetailRow label="Experience">
            {job.experience_required || job.experience_range || 'Not specified'}
          </DetailRow>
          <DetailRow label="Role track">{trackTitle(job.role_track)}</DetailRow>
          {job.job_type && <DetailRow label="Job type">{job.job_type}</DetailRow>}
          <DetailRow label="Posted">
            <RelativeTime value={job.date_posted} fallback="Unknown" />
          </DetailRow>
          <DetailRow label="First found">
            <RelativeTime value={job.first_seen_at} />
          </DetailRow>
          <DetailRow label="Last seen">
            <RelativeTime value={job.last_seen_at} />
          </DetailRow>
          {job.senior_reason && <DetailRow label="Seniority">{job.senior_reason}</DetailRow>}
          {isHttp(job.company_url) && (
            <DetailRow label="Company">
              <a href={job.company_url} target="_blank" rel="noopener noreferrer">
                Company page <ExternalLink size={12} />
              </a>
            </DetailRow>
          )}
        </div>

        <h4 className="modal-section-title">Skills vs your profile</h4>
        <SkillChips job={job} limitMissing={20} />

        <div className="tracking-box">
          <label className="field">
            <span className="field-label">Application status</span>
            <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
              {APP_STATUSES.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field grow">
            <span className="field-label">Notes</span>
            <textarea
              className="input"
              rows={2}
              value={notes}
              disabled={untracked}
              placeholder={untracked ? 'Pick a status to add notes' : 'Recruiter, referral, follow-up date…'}
              onChange={(e) => setNotes(e.target.value)}
            />
            {untracked && <span className="field-hint">Notes are kept only for tracked jobs.</span>}
          </label>
          <button className="btn-primary" onClick={save} disabled={!dirty || saving}>
            <Save size={15} /> {saving ? 'Saving…' : 'Save'}
          </button>
        </div>

        <h4 className="modal-section-title">Job description</h4>
        {error ? (
          <p className="form-error">Could not load the full job: {error.message}</p>
        ) : detail ? (
          <div className="description-box">
            <DescriptionText text={detail.description} />
          </div>
        ) : (
          <div className="loading-inline">
            <div className="spinner" /> Loading description…
          </div>
        )}

        <div className="modal-footer">
          {applyUrl && (
            <a href={applyUrl} target="_blank" rel="noopener noreferrer" className="btn-scrape">
              <span>Open posting</span>
              <ExternalLink size={16} />
            </a>
          )}
        </div>
      </div>
    </div>
  )
}
