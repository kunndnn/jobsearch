import { createContext, useContext, useEffect, useState } from 'react'

/** Shared clock so every relative timestamp re-renders together. */
export const NowContext = createContext(Date.now())

export const useNow = () => useContext(NowContext)

export function useClock(intervalMs) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(timer)
  }, [intervalMs])
  return now
}
