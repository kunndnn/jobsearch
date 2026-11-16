import RelativeTime from './RelativeTime.jsx'
import { siteLabel } from '../utils/labels.js'
import { formatLocal } from '../utils/time.js'

function StatCard({ label, value, sub, tone }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ? `tone-${tone}` : ''}`}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

const count = (value) => (typeof value === 'number' ? value.toLocaleString() : '—')

export default function StatsBar({ stats }) {
  const s = stats || {}
  const platforms = Object.entries(s.platform_distribution || {})
    .sort((a, b) => b[1] - a[1])
    .map(([site, n]) => `${siteLabel(site)} ${n}`)
    .join(' · ')
  const topSkills = s.top_skills || []

  return (
    <>
      <section className="stats-grid">
        <StatCard
          label="Matching jobs"
          value={count(s.total_jobs)}
          sub={`${count(s.total_stored)} stored${platforms ? ` · ${platforms}` : ''}`}
        />
        <StatCard label="High matches (≥80%)" value={count(s.high_match_count)} tone="emerald" sub="Strong fit for your profile" />
        <StatCard label="New" value={count(s.new_count)} tone="purple" sub="Found by the latest scrape" />
        <StatCard
          label="Applied"
          value={count(s.applied_count)}
          tone="blue"
          sub={`${count(s.interviewing_count)} interviewing · ${count(s.rejected_count)} rejected`}
        />
        <StatCard label="Saved" value={count(s.saved_count)} tone="amber" sub="Bookmarked for later" />
        <StatCard
          label="Last scraped"
          value={<RelativeTime value={s.last_scrape_at} fallback="Never" />}
          sub={s.last_scrape_at ? formatLocal(s.last_scrape_at) : 'Run a scrape to fetch jobs'}
        />
      </section>
      {topSkills.length > 0 && (
        <div className="insight-row">
          <span className="pill-label">In demand</span>
          {topSkills.slice(0, 10).map((skill) => (
            <span key={skill.name} className="skill-chip">
              {skill.name} <span className="muted">{skill.count}</span>
            </span>
          ))}
        </div>
      )}
    </>
  )
}
