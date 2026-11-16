import { useCallback, useEffect, useRef, useState } from 'react'
import { Briefcase, CalendarClock, Play, RefreshCw, Settings, Sparkles, User, WifiOff } from 'lucide-react'
import ProgressBanner from './components/ProgressBanner.jsx'
import NowProvider from './components/NowProvider.jsx'
import RelativeTime from './components/RelativeTime.jsx'
import Toast from './components/Toast.jsx'
import JobsView from './views/JobsView.jsx'
import AutomationView from './views/AutomationView.jsx'
import ProfileView from './views/ProfileView.jsx'
import { usePersistentState } from './hooks/usePersistentState.js'
import { useScrapeStatus } from './hooks/useScrapeStatus.js'
import { useToasts } from './hooks/useToasts.js'
import { getProfile, getSettings, getStats, startScrape, stopScrape } from './api.js'
import { TABS, runFinishedToast } from './utils/labels.js'
import { formatMinutes } from './utils/time.js'
import './App.css'

const TAB_ICONS = { jobs: Briefcase, automation: Settings, profile: User }

function UnreachableBanner({ error, onRetry }) {
  return (
    <div className="alert-banner" role="alert">
      <WifiOff size={18} />
      <div>
        <strong>{error.unreachable ? 'Backend unreachable.' : 'Status check failed.'}</strong>{' '}
        {error.unreachable
          ? 'The dashboard server is not responding — start it with `python start.py`. Retrying automatically…'
          : error.message}
      </div>
      <button className="pill-btn" onClick={onRetry}>
        <RefreshCw size={14} /> Retry
      </button>
    </div>
  )
}

function ScheduleSummary({ schedule }) {
  if (!schedule) return null
  if (!schedule.enabled) return <span className="header-meta">Auto-scrape off</span>
  return (
    <span className="header-meta">
      <CalendarClock size={14} /> Every {formatMinutes(schedule.interval_minutes)}
      {schedule.next_run_at && (
        <>
          {' · next '}
          <RelativeTime value={schedule.next_run_at} />
        </>
      )}
    </span>
  )
}

export default function App() {
  const { toasts, notify, dismiss } = useToasts()
  const [storedTab, setTab] = usePersistentState('jobspy.activeTab', 'jobs')
  const tab = TABS.some((t) => t.id === storedTab) ? storedTab : 'jobs'

  const [profile, setProfile] = useState(null)
  const [profileError, setProfileError] = useState(null)
  const [settings, setSettings] = useState(null)
  const [settingsError, setSettingsError] = useState(null)
  const [stats, setStats] = useState(null)
  const [jobsRefreshKey, setJobsRefreshKey] = useState(0)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const automationDirty = useRef(false)
  const setAutomationDirty = useCallback((dirty) => {
    automationDirty.current = dirty
  }, [])

  // Overlapping stats requests (e.g. two quick status changes): only the newest may write.
  const statsSeq = useRef(0)
  const loadStats = useCallback(async () => {
    const seq = ++statsSeq.current
    try {
      const next = await getStats()
      if (seq === statsSeq.current) setStats(next)
    } catch {
      // stats are decorative; the status banner reports connectivity problems
    }
  }, [])

  const refreshData = useCallback(() => {
    setJobsRefreshKey((k) => k + 1)
    loadStats()
  }, [loadStats])

  const handleRunFinished = useCallback(
    (run) => {
      notify(runFinishedToast(run))
      setStopping(false)
      refreshData()
    },
    [notify, refreshData],
  )

  const { status, error: statusError, refresh: refreshStatus, isScraping, currentRun } = useScrapeStatus({
    onRunFinished: handleRunFinished,
  })
  const unreachable = Boolean(statusError?.unreachable)
  const isStopping = stopping && isScraping

  // Load profile/settings/stats on mount and again whenever the backend comes back.
  const wasUnreachable = useRef(false)
  useEffect(() => {
    if (unreachable) {
      wasUnreachable.current = true
      return
    }
    const controller = new AbortController()
    const { signal } = controller
    const recovering = wasUnreachable.current
    wasUnreachable.current = false

    getProfile(signal)
      .then((p) => {
        setProfile(p)
        setProfileError(null)
      })
      .catch((err) => err.name !== 'AbortError' && setProfileError(err))
    getSettings(signal)
      .then((s) => {
        setSettings(s)
        setSettingsError(null)
      })
      .catch((err) => err.name !== 'AbortError' && setSettingsError(err))
    const seq = ++statsSeq.current
    getStats(signal)
      .then((s) => {
        if (seq === statsSeq.current) setStats(s)
        if (recovering) setJobsRefreshKey((k) => k + 1)
      })
      .catch(() => {})
    return () => controller.abort()
  }, [unreachable])

  const runNow = useCallback(async () => {
    if (
      automationDirty.current &&
      !window.confirm('The Automation tab has unsaved changes. Run now with the saved settings instead?')
    ) {
      return
    }
    setStarting(true)
    try {
      const res = await startScrape({})
      notify({ type: 'info', title: 'Scrape started', message: `Run #${res.run_id} uses your saved automation settings.` })
    } catch (err) {
      notify({
        type: err.status === 409 ? 'warning' : 'error',
        title: err.status === 409 ? 'A scrape is already running' : 'Could not start scrape',
        message: err.message,
      })
    } finally {
      setStarting(false)
      refreshStatus()
    }
  }, [notify, refreshStatus])

  const stopRun = useCallback(async () => {
    setStopping(true)
    try {
      await stopScrape()
      notify({ type: 'info', title: 'Stopping scrape', message: 'The run stops after the current query.' })
    } catch (err) {
      setStopping(false)
      notify({ type: err.status === 404 ? 'info' : 'error', title: 'Could not stop scrape', message: err.message })
    } finally {
      refreshStatus()
    }
  }, [notify, refreshStatus])

  const handleSettingsSaved = useCallback(
    (next) => {
      setSettings(next)
      refreshStatus()
    },
    [refreshStatus],
  )

  const handleProfileSaved = useCallback(
    (res) => {
      setProfile(res.profile)
      refreshData()
      notify({
        type: 'success',
        title: 'Profile saved',
        message: `${res.rescored ?? 0} jobs re-scored against your profile.`,
      })
    },
    [notify, refreshData],
  )

  return (
    <NowProvider intervalMs={10000}>
      <div className="app-container">
        <header className="header">
          <div className="brand-section">
            <div className="logo-badge">JS</div>
            <div>
              <h1 className="brand-title">JobSpy India • Career Intelligence</h1>
              <div className="candidate-pill">
                <Sparkles size={13} />
                <span>
                  {profile
                    ? [profile.name, profile.headline].filter(Boolean).join(' • ')
                    : profileError
                      ? 'Profile unavailable'
                      : 'Loading profile…'}
                </span>
              </div>
            </div>
          </div>

          <div className="header-actions">
            <ScheduleSummary schedule={status?.schedule} />
            <button className="btn-scrape" onClick={runNow} disabled={isScraping || starting || unreachable}>
              {isScraping ? (
                <>
                  <div className="spinner" />
                  <span>Scraping…</span>
                </>
              ) : (
                <>
                  <Play size={16} />
                  <span>{starting ? 'Starting…' : 'Run scrape now'}</span>
                </>
              )}
            </button>
          </div>
        </header>

        {statusError && <UnreachableBanner error={statusError} onRetry={refreshStatus} />}

        <nav className="tabs" role="tablist">
          {TABS.map(({ id, label }) => {
            const Icon = TAB_ICONS[id]
            return (
              <button
                key={id}
                role="tab"
                aria-selected={tab === id}
                className={`tab-btn ${tab === id ? 'active' : ''}`}
                onClick={() => setTab(id)}
              >
                <Icon size={16} /> {label}
                {id === 'automation' && isScraping && <span className="tab-dot" aria-label="Scrape running" />}
              </button>
            )
          })}
        </nav>

        {tab !== 'automation' && currentRun && <ProgressBanner run={currentRun} onStop={stopRun} stopping={isStopping} />}

        <main>
          <div hidden={tab !== 'jobs'}>
            <JobsView
              profile={profile}
              settings={settings}
              stats={stats}
              refreshKey={jobsRefreshKey}
              onJobsChanged={loadStats}
              onRunNow={runNow}
              isScraping={isScraping}
              notify={notify}
            />
          </div>
          <div hidden={tab !== 'automation'}>
            <AutomationView
              settings={settings}
              settingsError={settingsError}
              status={status}
              onSettingsSaved={handleSettingsSaved}
              onDirtyChange={setAutomationDirty}
              onRunNow={runNow}
              onStop={stopRun}
              starting={starting}
              stopping={isStopping}
              notify={notify}
            />
          </div>
          <div hidden={tab !== 'profile'}>
            <ProfileView
              profile={profile}
              profileError={profileError}
              onProfileSaved={handleProfileSaved}
              notify={notify}
            />
          </div>
        </main>

        <Toast toasts={toasts} onDismiss={dismiss} />
      </div>
    </NowProvider>
  )
}
