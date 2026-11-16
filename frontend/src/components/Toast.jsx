import { useEffect } from 'react'
import { CircleCheck, CircleX, Info, TriangleAlert, X } from 'lucide-react'

const ICONS = { success: CircleCheck, error: CircleX, warning: TriangleAlert, info: Info }
const DURATION_MS = { success: 6000, info: 5000, warning: 9000, error: 10000 }

function ToastItem({ toast, onDismiss }) {
  const Icon = ICONS[toast.type] || Info

  useEffect(() => {
    const timer = setTimeout(() => onDismiss(toast.id), DURATION_MS[toast.type] || 6000)
    return () => clearTimeout(timer)
  }, [toast.id, toast.type, onDismiss])

  return (
    <div className={`toast toast-${toast.type}`} role={toast.type === 'error' ? 'alert' : 'status'}>
      <Icon size={18} className="toast-icon" />
      <div className="toast-body">
        {toast.title && <div className="toast-title">{toast.title}</div>}
        {toast.message && <div className="toast-message">{toast.message}</div>}
      </div>
      <button className="btn-close" onClick={() => onDismiss(toast.id)} aria-label="Dismiss notification">
        <X size={16} />
      </button>
    </div>
  )
}

export default function Toast({ toasts, onDismiss }) {
  return (
    <div className="toast-stack" aria-live="polite">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  )
}
