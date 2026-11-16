const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

const UNITS = [
  ['y', 365 * 86400],
  ['mo', 30 * 86400],
  ['w', 7 * 86400],
  ['d', 86400],
  ['h', 3600],
]

/** Parses ISO timestamps; bare `YYYY-MM-DD` dates are read as local calendar days. */
export function parseDate(value) {
  if (!value) return null
  if (typeof value === 'string' && DATE_ONLY.test(value)) {
    const [y, m, d] = value.split('-').map(Number)
    return new Date(y, m - 1, d)
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function isDateOnly(value) {
  return typeof value === 'string' && DATE_ONLY.test(value)
}

/** "2h ago", "in 3h", "just now". */
export function formatRelative(value, now = Date.now()) {
  const date = parseDate(value)
  if (!date) return '—'
  if (isDateOnly(value)) return formatRelativeDay(date, now)

  const diff = Math.round((date.getTime() - now) / 1000)
  const abs = Math.abs(diff)
  // Up to 10 s in the future still reads as "just now" (clock tick lag / small server clock skew).
  if (abs < 45) return diff < 10 ? 'just now' : 'in <1m'

  let label = `${Math.max(1, Math.round(abs / 60))}m`
  for (const [unit, seconds] of UNITS) {
    if (abs >= seconds) {
      label = `${Math.floor(abs / seconds)}${unit}`
      break
    }
  }
  return diff < 0 ? `${label} ago` : `in ${label}`
}

function startOfDay(ms) {
  const d = new Date(ms)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

function formatRelativeDay(date, now) {
  const days = Math.round((startOfDay(now) - startOfDay(date.getTime())) / 86400000)
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days === -1) return 'tomorrow'
  const abs = Math.abs(days)
  const label = abs >= 365 ? `${Math.floor(abs / 365)}y` : abs >= 30 ? `${Math.floor(abs / 30)}mo` : `${abs}d`
  return days > 0 ? `${label} ago` : `in ${label}`
}

/** Full local date/time for tooltips and detail rows. */
export function formatLocal(value) {
  const date = parseDate(value)
  if (!date) return '—'
  if (isDateOnly(value)) {
    return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
  }
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/** "45s", "3m 12s", "1h 05m". */
export function formatDuration(start, end, now = Date.now()) {
  const from = parseDate(start)
  if (!from) return '—'
  const to = parseDate(end)?.getTime() ?? now
  const total = Math.max(0, Math.round((to - from.getTime()) / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

/** 90 -> "1h 30m", 360 -> "6h", 45 -> "45 min". */
export function formatMinutes(minutes) {
  const value = Number(minutes)
  if (!Number.isFinite(value) || value <= 0) return '—'
  if (value < 60) return `${value} min`
  const h = Math.floor(value / 60)
  const m = value % 60
  return m ? `${h}h ${m}m` : `${h}h`
}
