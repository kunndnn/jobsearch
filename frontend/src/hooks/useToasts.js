import { useCallback, useRef, useState } from 'react'

const MAX_TOASTS = 4

export function useToasts() {
  const [toasts, setToasts] = useState([])
  const nextId = useRef(1)

  const notify = useCallback((toast) => {
    const id = nextId.current++
    setToasts((list) => [...list, { type: 'info', ...toast, id }].slice(-MAX_TOASTS))
    return id
  }, [])

  const dismiss = useCallback((id) => {
    setToasts((list) => list.filter((t) => t.id !== id))
  }, [])

  return { toasts, notify, dismiss }
}
