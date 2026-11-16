import { Award, Home, RotateCcw, Search, ShieldCheck, Sparkles } from 'lucide-react'
import { APP_STATUSES, SORT_OPTIONS, siteLabel } from '../utils/labels.js'

function Pill({ active, onClick, children, title }) {
  return (
    <button type="button" className={`pill-btn ${active ? 'active' : ''}`} onClick={onClick} title={title} aria-pressed={active}>
      {children}
    </button>
  )
}

function PillGroup({ label, children }) {
  return (
    <div className="pills-group" role="group" aria-label={label}>
      <span className="pill-label">{label}</span>
      {children}
    </div>
  )
}

export default function JobFilters({ filters, onChange, onReset, profile, availableSites }) {
  const set = (patch) => onChange({ ...filters, ...patch })
  const tracks = profile?.target_tracks || []
  const locations = profile?.preferred_locations || []
  const sites = availableSites || []
  const siteSelected = (site) => filters.sites === 'all' || filters.sites.includes(site)

  const toggleSite = (site) => {
    if (filters.sites === 'all') return set({ sites: [site] })
    const next = filters.sites.includes(site) ? filters.sites.filter((s) => s !== site) : [...filters.sites, site]
    set({ sites: next })
  }

  return (
    <section className="controls-card">
      <div className="search-row">
        <div className="search-input-wrapper">
          <Search className="search-icon" size={18} />
          <input
            type="search"
            placeholder="Search title, company, description or skill…"
            className="search-input"
            value={filters.search}
            onChange={(e) => set({ search: e.target.value })}
            aria-label="Search jobs"
          />
        </div>

        <label className="score-slider-wrapper">
          <Award size={16} className="tone-emerald" />
          <span className="score-slider-label">Min match: {filters.minScore}%</span>
          <input
            type="range"
            min="0"
            max="90"
            step="5"
            value={filters.minScore}
            onChange={(e) => set({ minScore: Number(e.target.value) })}
          />
        </label>

        <div className="toggle-row">
          <Pill
            active={filters.excludeSenior}
            onClick={() => set({ excludeSenior: !filters.excludeSenior })}
            title="Hide senior roles (above your max experience) and internships"
          >
            <ShieldCheck size={14} /> Hide senior & intern
          </Pill>
          <Pill active={filters.remoteOnly} onClick={() => set({ remoteOnly: !filters.remoteOnly })}>
            <Home size={14} /> Remote only
          </Pill>
          <Pill active={filters.newOnly} onClick={() => set({ newOnly: !filters.newOnly })} title="Only jobs found by the latest scrape">
            <Sparkles size={14} /> New only
          </Pill>
        </div>
      </div>

      <PillGroup label="Role">
        <Pill active={filters.roleTrack === 'all'} onClick={() => set({ roleTrack: 'all' })}>
          All roles
        </Pill>
        {tracks.map((track) => (
          <Pill key={track.id} active={filters.roleTrack === track.id} onClick={() => set({ roleTrack: track.id })}>
            {track.title || track.id}
          </Pill>
        ))}
        <Pill active={filters.roleTrack === 'other'} onClick={() => set({ roleTrack: 'other' })}>
          Other
        </Pill>
      </PillGroup>

      <PillGroup label="Location">
        <Pill active={filters.location === 'all'} onClick={() => set({ location: 'all' })}>
          Anywhere
        </Pill>
        {locations.map((loc) => (
          <Pill key={loc} active={filters.location === loc} onClick={() => set({ location: loc })}>
            {loc}
          </Pill>
        ))}
      </PillGroup>

      <PillGroup label="Boards">
        <Pill active={filters.sites === 'all'} onClick={() => set({ sites: 'all' })}>
          All boards
        </Pill>
        {sites.map((site) => (
          <Pill key={site} active={filters.sites !== 'all' && siteSelected(site)} onClick={() => toggleSite(site)}>
            {siteLabel(site)}
          </Pill>
        ))}
      </PillGroup>

      <PillGroup label="Status">
        <Pill active={filters.status === 'all'} onClick={() => set({ status: 'all' })}>
          Any
        </Pill>
        {APP_STATUSES.map((s) => (
          <Pill key={s.id} active={filters.status === s.id} onClick={() => set({ status: s.id })}>
            {s.label}
          </Pill>
        ))}
      </PillGroup>

      <div className="pills-group sort-group">
        <span className="pill-label">Sort</span>
        {SORT_OPTIONS.map((opt) => (
          <Pill key={opt.id} active={filters.sortBy === opt.id} onClick={() => set({ sortBy: opt.id })}>
            {opt.label}
          </Pill>
        ))}
        <button type="button" className="link-btn reset-btn" onClick={onReset}>
          <RotateCcw size={13} /> Reset filters
        </button>
      </div>
    </section>
  )
}
