import { NowContext, useClock } from '../hooks/useNow.js'

/** Ticks the shared clock; only components that read useNow() re-render on each tick. */
export default function NowProvider({ intervalMs, children }) {
  const now = useClock(intervalMs)
  return <NowContext.Provider value={now}>{children}</NowContext.Provider>
}
