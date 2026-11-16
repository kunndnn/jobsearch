import { useNow } from '../hooks/useNow.js'
import { formatLocal, formatRelative } from '../utils/time.js'

/** Relative time ("2h ago", "in 3h") with the full local date/time as tooltip. */
export default function RelativeTime({ value, fallback = '—', prefix = '' }) {
  const now = useNow()
  if (!value) return <span className="muted">{fallback}</span>
  return (
    <time dateTime={value} title={formatLocal(value)}>
      {prefix}
      {formatRelative(value, now)}
    </time>
  )
}
