/**
 * Lightweight toast notification system.
 *
 * Usage:
 *   import { ToastContainer, useToast } from '../components/Toast'
 *
 *   // In your root layout:
 *   <ToastContainer />
 *
 *   // In any component:
 *   const toast = useToast()
 *   toast.error('Something went wrong')
 *   toast.success('Saved!')
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, CheckCircle, Info, X, XCircle } from 'lucide-react'
import { cn } from '../utils/cn'

const ToastContext = createContext(null)

const ICONS = {
  success: CheckCircle,
  error: XCircle,
  warning: AlertTriangle,
  info: Info,
}

// Backgrounds must be fully opaque — toasts sit on top of page content so
// any alpha channel lets the content behind bleed through. Dark variants
// previously used /40 (40% opacity) which read as see-through.
const STYLES = {
  success: 'bg-green-50 dark:bg-green-950 text-green-800 dark:text-green-100 border-green-200 dark:border-green-700',
  error:   'bg-red-50   dark:bg-red-950   text-red-800   dark:text-red-100   border-red-200   dark:border-red-700',
  warning: 'bg-amber-50 dark:bg-amber-950 text-amber-800 dark:text-amber-100 border-amber-200 dark:border-amber-700',
  info:    'bg-blue-50  dark:bg-blue-950  text-blue-800  dark:text-blue-100  border-blue-200  dark:border-blue-700',
}

let nextId = 0

function ToastItem({ toast, onDismiss }) {
  const [exiting, setExiting] = useState(false)
  const Icon = ICONS[toast.type] || Info

  const dismiss = useCallback(() => {
    setExiting(true)
    setTimeout(() => onDismiss(toast.id), 200)
  }, [toast.id, onDismiss])

  useEffect(() => {
    const timer = setTimeout(dismiss, toast.duration || 5000)
    return () => clearTimeout(timer)
  }, [dismiss, toast.duration])

  return (
    <div
      className={cn(
        'flex items-start gap-2.5 px-4 py-3 rounded-lg border shadow-lg max-w-md transition-all duration-200',
        STYLES[toast.type] || STYLES.info,
        exiting ? 'opacity-0 translate-x-4' : 'opacity-100 translate-x-0',
      )}
    >
      <Icon size={18} className="shrink-0 mt-0.5" />
      <p className="text-sm flex-1">{toast.message}</p>
      <button
        onClick={dismiss}
        className="shrink-0 p-0.5 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
      >
        <X size={14} />
      </button>
    </div>
  )
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const add = useCallback((type, message, duration) => {
    const id = ++nextId
    setToasts(prev => [...prev, { id, type, message, duration }])
  }, [])

  useEffect(() => {
    const pending = (event) => add('info', event.detail)
    window.addEventListener('pulse:workflow-pending', pending)
    return () => window.removeEventListener('pulse:workflow-pending', pending)
  }, [add])

  const dismiss = useCallback((id) => {
    setToasts(prev => prev.filter(t => t.id !== id))
  }, [])

  const api = useRef({
    success: (msg, dur) => add('success', msg, dur),
    error: (msg, dur) => add('error', msg, dur || 8000),
    warning: (msg, dur) => add('warning', msg, dur),
    info: (msg, dur) => add('info', msg, dur),
  })

  return (
    <ToastContext.Provider value={api.current}>
      {children}
      {createPortal(
        <div className="fixed top-4 right-4 z-[99999] flex flex-col gap-2 pointer-events-auto">
          {toasts.map(t => (
            <ToastItem key={t.id} toast={t} onDismiss={dismiss} />
          ))}
        </div>,
        document.body
      )}
    </ToastContext.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within <ToastProvider>')
  return ctx
}
