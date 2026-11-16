import { useState } from 'react'
import { CalendarClock } from 'lucide-react'
import RelativeTime from './RelativeTime.jsx'
import { Toggle } from './FormControls.jsx'
import { useNow } from '../hooks/useNow.js'
import { INTERVAL_PRESETS, MAX_INTERVAL_MINUTES } from '../utils/labels.js'
import { formatLocal, formatMinutes, parseDate } from '../utils/time.js'

function NextRun({ live, now }) {
  if (!live) return <span className="muted">—</span>
  if (!live.enabled) return <span className="muted">Automation is off</span>
  const next = parseDate(live.next_run_at)
  if (!next) return <span className="muted">Not scheduled</span>
  if (next.getTime() - now < 10000) return <span title={formatLocal(live.next_run_at)}>Due now</span>
  return <RelativeTime value={live.next_run_at} />
}

/** schedule: draft ScheduleSettings; live: status.schedule (last/next run as the server sees it). */
export default function ScheduleCard({ schedule, onChange, minInterval, live, unsaved }) {
  const now = useNow()
  const interval = schedule.interval_minutes
  const isPreset = INTERVAL_PRESETS.some((p) => p.minutes === interval)
  const [customOpen, setCustomOpen] = useState(!isPreset)
  const showCustom = customOpen || !isPreset

  return (
    <div className="panel">
      <div className="panel-header">
        <h3 className="panel-title">
          <CalendarClock size={18} /> Schedule
        </h3>
        <span className={`run-status ${schedule.enabled ? 'status-completed' : 'status-cancelled'}`}>
          {schedule.enabled ? `Every ${formatMinutes(interval)}` : 'Off'}
        </span>
      </div>

      <Toggle
        checked={schedule.enabled}
        onChange={(enabled) => onChange({ enabled })}
        label="Scrape automatically"
        hint="Runs inside the dashboard server while it is running"
      />

      <div className="field">
        <span className="field-label">Interval</span>
        <div className="pills-group tight">
          {INTERVAL_PRESETS.map((preset) => (
            <button
              key={preset.minutes}
              type="button"
              className={`pill-btn ${!showCustom && interval === preset.minutes ? 'active' : ''}`}
              disabled={preset.minutes < minInterval}
              title={preset.minutes < minInterval ? `Below the ${minInterval} min minimum` : undefined}
              onClick={() => {
                setCustomOpen(false)
                onChange({ interval_minutes: preset.minutes })
              }}
            >
              {preset.label}
            </button>
          ))}
          <button type="button" className={`pill-btn ${showCustom ? 'active' : ''}`} onClick={() => setCustomOpen(true)}>
            Custom
          </button>
        </div>
        {showCustom && (
          <div className="input-with-suffix custom-interval">
            <input
              className="input"
              type="number"
              min={minInterval}
              max={MAX_INTERVAL_MINUTES}
              step={5}
              value={interval ?? ''}
              aria-label="Custom interval in minutes"
              onChange={(e) => onChange({ interval_minutes: e.target.value === '' ? '' : Number(e.target.value) })}
            />
            <span className="input-suffix">minutes</span>
          </div>
        )}
        <div className="field-hint">
          Minimum {formatMinutes(minInterval)} · maximum {formatMinutes(MAX_INTERVAL_MINUTES)} (7 days)
        </div>
      </div>

      <Toggle
        checked={schedule.run_on_startup}
        onChange={(run_on_startup) => onChange({ run_on_startup })}
        label="Run when the server starts"
        hint="Otherwise a startup run only happens if the schedule is overdue"
      />

      <div className="schedule-times">
        <div>
          <span className="detail-label">Last run</span>
          <RelativeTime value={live?.last_run_at} fallback="Never" />
        </div>
        <div>
          <span className="detail-label">Next run</span>
          <NextRun live={live} now={now} />
        </div>
      </div>
      {unsaved && <div className="field-hint">Save to apply schedule changes; next run is recalculated immediately.</div>}
    </div>
  )
}
