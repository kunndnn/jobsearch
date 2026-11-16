import {
  Bookmark,
  Briefcase,
  Building,
  CircleCheck,
  Clock,
  ExternalLink,
  MapPin,
  Sparkles,
  TrendingUp,
  Zap,
} from 'lucide-react'
import RelativeTime from './RelativeTime.jsx'
import { appStatusLabel, boardClass, scoreClass, siteLabel } from '../utils/labels.js'

export function SkillChips({ job, limitMissing = 6 }) {
  const backend = new Set(job.backend_skills || [])
  const ai = new Set(job.ai_skills || [])
  const missing = job.missing_skills || []
  return (
    <div className="skills-container">
      {(job.matched_skills || []).map((skill) => (
        <span key={skill} className={`skill-chip ${ai.has(skill) ? 'ai' : backend.has(skill) ? 'backend' : ''}`}>
          {skill}
        </span>
      ))}
      {missing.slice(0, limitMissing).map((skill) => (
        <span key={`missing-${skill}`} className="skill-chip missing" title="In the job, not in your profile">
          +{skill}
        </span>
      ))}
    </div>
  )
}

export function JobBadges({ job }) {
  const status = job.app_status || 'none'
  return (
    <>
      <span className={boardClass(job.site)}>{siteLabel(job.site)}</span>
      {(job.also_on || []).map((dup) => (
        <a
          key={`${dup.site}-${dup.job_url}`}
          className={`${boardClass(dup.site)} also-on`}
          href={dup.job_url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
          title={`Also posted on ${siteLabel(dup.site)}`}
        >
          also on {siteLabel(dup.site)}
        </a>
      ))}
      {job.is_new && (
        <span className="board-badge badge-new">
          <Sparkles size={11} /> New
        </span>
      )}
      {job.is_remote && <span className="board-badge badge-remote">Remote</span>}
      {job.is_senior && (
        <span className="board-badge badge-senior" title={job.senior_reason || 'Above your experience range'}>
          Senior
        </span>
      )}
      {job.is_intern && <span className="board-badge badge-senior">Intern</span>}
      {status !== 'none' && <span className={`board-badge app-${status}`}>{appStatusLabel(status)}</span>}
    </>
  )
}

export default function JobCard({ job, onOpen, onQuickStatus }) {
  const score = job.match_score ?? 0
  const status = job.app_status || 'none'
  const toggle = (target) => onQuickStatus(job, status === target ? 'none' : target)

  return (
    <article className="job-card" onClick={() => onOpen(job)}>
      <div className="job-card-header">
        <div className="job-card-main">
          <div className="job-title-row">
            <button
              type="button"
              className="job-title"
              onClick={(e) => {
                e.stopPropagation()
                onOpen(job)
              }}
            >
              {job.title || 'Untitled role'}
            </button>
            <JobBadges job={job} />
          </div>
          <div className="company-name">
            <Building size={15} />
            <span>{job.company || 'Company not listed'}</span>
          </div>
        </div>
        <div className={`match-badge ${scoreClass(score)}`} title="Match with your profile">
          <Zap size={14} />
          <span>{score}%</span>
        </div>
      </div>

      <div className="job-meta-row">
        <div className="meta-item">
          <MapPin size={14} />
          <span>{job.location || 'Location not listed'}</span>
        </div>
        <div className="meta-item salary">
          <TrendingUp size={14} />
          <span>{job.salary_display || 'Not disclosed'}</span>
        </div>
        {job.experience_required && (
          <div className="meta-item">
            <Briefcase size={14} />
            <span>Exp: {job.experience_required}</span>
          </div>
        )}
        {job.date_posted && (
          <div className="meta-item">
            <Clock size={14} />
            <span>
              Posted <RelativeTime value={job.date_posted} />
            </span>
          </div>
        )}
        {job.first_seen_at && (
          <div className="meta-item">
            <span>
              Found <RelativeTime value={job.first_seen_at} />
            </span>
          </div>
        )}
      </div>

      {job.description_snippet && <p className="job-snippet">{job.description_snippet}</p>}

      <SkillChips job={job} limitMissing={4} />

      <div className="card-actions" onClick={(e) => e.stopPropagation()}>
        <div className="action-btn-group">
          <button
            type="button"
            className={`btn-icon ${status === 'saved' ? 'active' : ''}`}
            title={status === 'saved' ? 'Remove from saved' : 'Save job'}
            aria-pressed={status === 'saved'}
            onClick={() => toggle('saved')}
          >
            <Bookmark size={16} />
          </button>
          <button
            type="button"
            className={`btn-icon ${status === 'applied' ? 'active applied' : ''}`}
            title={status === 'applied' ? 'Unmark applied' : 'Mark as applied'}
            aria-pressed={status === 'applied'}
            onClick={() => toggle('applied')}
          >
            <CircleCheck size={16} />
          </button>
        </div>
        {(job.job_url_direct || job.job_url) && (
          <a href={job.job_url_direct || job.job_url} target="_blank" rel="noopener noreferrer" className="btn-apply">
            <span>Apply</span>
            <ExternalLink size={14} />
          </a>
        )}
      </div>
    </article>
  )
}
