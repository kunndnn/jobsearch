import { useEffect, useState } from 'react'
import { CircleStop, History, Play, Search, TriangleAlert } from 'lucide-react'
import ChipEditor from '../components/ChipEditor.jsx'
import ProgressBanner from '../components/ProgressBanner.jsx'
import RunHistory from '../components/RunHistory.jsx'
import ScheduleCard from '../components/ScheduleCard.jsx'
import { NumberField, SaveBar, TextField, Toggle } from '../components/FormControls.jsx'
import { useDraft } from '../hooks/useDraft.js'
import { detailLines, saveSettings } from '../api.js'
import { MAX_INTERVAL_MINUTES, siteLabel } from '../utils/labels.js'
import { formatMinutes } from '../utils/time.js'

const toDraft = (settings) => ({ scrape: settings.scrape, schedule: settings.schedule })

// [field, label, min, max, whole numbers only]
const RANGES = [
  ['results_per_query', 'Results per query', 1, 50, true],
  ['hours_old', 'Max job age (hours)', 1, 720, true],
  ['retention_days', 'Keep jobs for (days)', 1, 365, true],
  ['delay_min_seconds', 'Min delay between queries (s)', 0, 120, true],
  ['delay_max_seconds', 'Max delay between queries (s)', 0, 120, true],
]

function validate(draft, minInterval) {
  const { scrape, schedule } = draft
  const errors = []
  if (!scrape.search_terms?.length) errors.push('Add at least one search term.')
  if (!scrape.locations?.length) errors.push('Add at least one location.')
  if (!scrape.sites?.length) errors.push('Select at least one job board.')
  for (const [key, label, min, max, whole] of RANGES) {
    const value = scrape[key]
    const valid = typeof value === 'number' && (whole ? Number.isInteger(value) : Number.isFinite(value))
    if (!valid || value < min || value > max) {
      errors.push(`${label} must be ${whole ? 'a whole number' : 'a number'} from ${min} to ${max}.`)
    }
  }
  if (Number(scrape.delay_min_seconds) > Number(scrape.delay_max_seconds)) {
    errors.push('Min delay cannot be greater than max delay.')
  }
  if (!String(scrape.country_indeed ?? '').trim()) errors.push('Indeed country is required.')
  const interval = schedule.interval_minutes
  if (interval === '' || !Number.isInteger(interval) || interval < minInterval || interval > MAX_INTERVAL_MINUTES) {
    errors.push(`Interval must be a whole number of minutes from ${minInterval} to ${MAX_INTERVAL_MINUTES}.`)
  }
  return errors
}

const plural = (n, word) => `${n} ${n === 1 ? word : `${word}s`}`

function QueryEstimate({ scrape, warnQueries }) {
  const terms = scrape.search_terms?.length || 0
  const locations = scrape.locations?.length || 0
  const queries = terms * locations
  const sites = scrape.sites || []
  const calls = queries * ((sites.some((s) => s !== 'indeed') ? 1 : 0) + (sites.includes('indeed') ? 1 : 0))
  const avgDelay = ((Number(scrape.delay_min_seconds) || 0) + (Number(scrape.delay_max_seconds) || 0)) / 2
  const delayMinutes = Math.round((Math.max(0, queries - 1) * avgDelay) / 60)
  const tooMany = queries > warnQueries

  return (
    <div className={`query-estimate ${tooMany ? 'warn' : ''}`}>
      {tooMany && <TriangleAlert size={16} />}
      <div>
        <strong>
          {queries} {queries === 1 ? 'query' : 'queries'} per run
        </strong>{' '}
        ({plural(terms, 'term')} × {plural(locations, 'location')}
        {calls !== queries ? `, ${calls} board calls` : ''}
        {delayMinutes > 0 ? `, ~${delayMinutes} min of delays` : ''})
        {tooMany && (
          <div className="field-hint">
            More than {warnQueries} queries makes runs slow and raises the chance of being rate-limited. Consider fewer
            terms or locations.
          </div>
        )}
      </div>
    </div>
  )
}

function ScrapeSettingsPanel({ scrape, setScrape, availableSites }) {
  const toggleSite = (site, on) =>
    setScrape({ sites: on ? [...scrape.sites, site] : scrape.sites.filter((s) => s !== site) })

  return (
    <div className="panel">
      <div className="panel-header">
        <h3 className="panel-title">
          <Search size={18} /> What to scrape
        </h3>
      </div>
      <ChipEditor
        label="Search terms"
        values={scrape.search_terms}
        onChange={(search_terms) => setScrape({ search_terms })}
        placeholder="e.g. Backend Developer"
      />
      <ChipEditor
        label="Locations"
        values={scrape.locations}
        onChange={(locations) => setScrape({ locations })}
        placeholder="e.g. Bengaluru or Remote"
        commaSeparates={false}
        hint="Press Enter to add. Commas are allowed inside a location (e.g. “Pune, Maharashtra”)."
      />

      <div className="field">
        <span className="field-label">Job boards</span>
        <div className="checkbox-row">
          {availableSites.map((site) => (
            <label key={site} className="checkbox">
              <input
                type="checkbox"
                checked={scrape.sites.includes(site)}
                onChange={(e) => toggleSite(site, e.target.checked)}
              />
              <span>{siteLabel(site)}</span>
            </label>
          ))}
        </div>
      </div>

      <ChipEditor
        label="Exclude terms"
        values={scrape.exclude_terms}
        onChange={(exclude_terms) => setScrape({ exclude_terms })}
        placeholder="e.g. senior"
        variant="negative"
        hint="Negative keywords, applied to Indeed searches only."
      />

      <div className="field-grid">
        {RANGES.map(([key, label, min, max, whole]) => (
          <NumberField
            key={key}
            label={label}
            value={scrape[key]}
            min={min}
            max={max}
            step={whole ? 1 : 0.5}
            onChange={(value) => setScrape({ [key]: value })}
          />
        ))}
        <TextField
          label="Indeed country"
          value={scrape.country_indeed}
          onChange={(country_indeed) => setScrape({ country_indeed })}
        />
      </div>

      <Toggle
        checked={scrape.fetch_description}
        onChange={(fetch_description) => setScrape({ fetch_description })}
        label="Fetch full descriptions"
        hint="Better matching and experience parsing, but slower runs"
      />
    </div>
  )
}

export default function AutomationView({
  settings,
  settingsError,
  status,
  onSettingsSaved,
  onDirtyChange,
  onRunNow,
  onStop,
  starting,
  stopping,
  notify,
}) {
  const { draft, setDraft, dirty, reset } = useDraft(settings, toDraft)
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange])
  const [saving, setSaving] = useState(false)
  const [serverErrors, setServerErrors] = useState([])

  if (!draft) {
    return (
      <div className="loading-block">
        {settingsError ? (
          <p className="form-error">Could not load settings: {settingsError.message}</p>
        ) : (
          <>
            <div className="spinner large" />
            <p>Loading automation settings…</p>
          </>
        )}
      </div>
    )
  }

  const limits = settings.limits || {}
  const minInterval = limits.min_interval_minutes ?? 15
  const warnQueries = limits.warn_queries ?? 30
  const errors = validate(draft, minInterval)
  const isScraping = Boolean(status?.is_scraping)
  const currentRun = status?.current_run

  const setScrape = (patch) => setDraft((d) => ({ ...d, scrape: { ...d.scrape, ...patch } }))
  const setSchedule = (patch) => setDraft((d) => ({ ...d, schedule: { ...d.schedule, ...patch } }))

  const save = async () => {
    setSaving(true)
    setServerErrors([])
    try {
      const res = await saveSettings({ scrape: draft.scrape, schedule: draft.schedule })
      reset(res)
      onSettingsSaved(res)
      notify({ type: 'success', title: 'Automation settings saved' })
    } catch (err) {
      setServerErrors(err.status === 422 ? detailLines(err.detail) : [err.message])
      notify({ type: 'error', title: 'Settings not saved', message: err.message })
    } finally {
      setSaving(false)
    }
  }

  const discard = () => {
    reset(settings)
    setServerErrors([])
  }

  return (
    <div className="automation">
      <div className="automation-grid">
        <ScrapeSettingsPanel
          scrape={draft.scrape}
          setScrape={setScrape}
          availableSites={settings.available_sites || []}
        />

        <div className="automation-side">
          <ScheduleCard
            schedule={draft.schedule}
            onChange={setSchedule}
            minInterval={minInterval}
            live={status?.schedule}
            unsaved={JSON.stringify(draft.schedule) !== JSON.stringify(settings.schedule)}
          />

          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">
                <Play size={18} /> Run
              </h3>
              <span className="muted small">Every {formatMinutes(settings.schedule?.interval_minutes)} when enabled</span>
            </div>
            <QueryEstimate scrape={draft.scrape} warnQueries={warnQueries} />
            <div className="run-actions">
              <button className="btn-scrape" onClick={onRunNow} disabled={isScraping || starting}>
                <Play size={16} /> {starting ? 'Starting…' : 'Run now'}
              </button>
              {isScraping && (
                <button className="btn-danger" onClick={onStop} disabled={stopping || currentRun?.cancel_requested}>
                  <CircleStop size={16} /> Stop
                </button>
              )}
            </div>
            {dirty && <div className="field-hint">Run now uses the saved settings. Save first to use your edits.</div>}
            {currentRun && <ProgressBanner run={currentRun} stopping={stopping} />}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <h3 className="panel-title">
            <History size={18} /> Run history
          </h3>
          <span className="muted small">Last {status?.history?.length || 0} runs</span>
        </div>
        <RunHistory runs={status?.history} />
      </div>

      <SaveBar dirty={dirty} saving={saving} errors={dirty ? errors : []} serverErrors={serverErrors} onSave={save} onDiscard={discard} />
    </div>
  )
}
