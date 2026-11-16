import { useEffect, useState } from 'react'

function readStored(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key)
    return raw === null ? fallback : JSON.parse(raw)
  } catch {
    return fallback
  }
}

/** useState mirrored to localStorage; storage failures (private mode, blocked) are ignored. */
export function usePersistentState(key, fallback) {
  const [value, setValue] = useState(() => readStored(key, fallback))
  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value))
    } catch {
      // storage unavailable: keep in-memory value only
    }
  }, [key, value])
  return [value, setValue]
}
