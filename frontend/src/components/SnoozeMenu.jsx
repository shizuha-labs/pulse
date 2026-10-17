/**
 * SnoozeMenu — dropdown with preset + custom snooze times for recurring tasks.
 *
 * Parks the task in the Scheduled gap-state until the user-chosen datetime
 * by posting to /items/{id}/snooze/. Presets mirror Linear / Gmail
 * snooze patterns: relative ("1 hour", "4 hours") and absolute ("tomorrow 9 AM",
 * "next Monday 9 AM"). Custom opens a datetime input.
 *
 * Visible only for recurring tasks (snooze is scoped to the cycle semantics).
 */
import { useState, useRef, useEffect } from 'react'
import { Clock, ChevronDown } from 'lucide-react'
import { cn } from '../utils/cn'

function nextMonday9am() {
  const d = new Date()
  d.setHours(9, 0, 0, 0)
  const daysUntilMonday = (8 - d.getDay()) % 7 || 7  // 1..7 (never same day)
  d.setDate(d.getDate() + daysUntilMonday)
  return d
}

function tomorrow9am() {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  d.setHours(9, 0, 0, 0)
  return d
}

function inHours(h) {
  return new Date(Date.now() + h * 3600 * 1000)
}

function fmt(d) {
  return d.toLocaleString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

export default function SnoozeMenu({ task, onSnooze, isSnoozing, className }) {
  const [open, setOpen] = useState(false)
  const [customOpen, setCustomOpen] = useState(false)
  const [customValue, setCustomValue] = useState('')
  const containerRef = useRef(null)

  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false)
        setCustomOpen(false)
      }
    }
    if (open) document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  if (!task?.is_recurring) return null

  const presets = [
    { label: '1 hour',          at: inHours(1) },
    { label: '4 hours',         at: inHours(4) },
    { label: 'Tomorrow 9 AM',   at: tomorrow9am() },
    { label: 'Next Monday 9 AM', at: nextMonday9am() },
  ]

  const isScheduled = task.status_category === 'scheduled'
  const label = isScheduled ? 'Reschedule' : 'Snooze'

  const fire = (date) => {
    onSnooze({ next_fire_at: date.toISOString() })
    setOpen(false)
    setCustomOpen(false)
  }

  const submitCustom = (e) => {
    e.preventDefault()
    if (!customValue) return
    const d = new Date(customValue)
    if (isNaN(d.getTime()) || d <= new Date()) return
    fire(d)
  }

  return (
    <div ref={containerRef} className={cn('relative inline-block', className)}>
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        disabled={isSnoozing}
        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
        title={isScheduled ? 'Change next fire time' : 'Park this task in Scheduled until…'}
      >
        <Clock size={14} />
        {isSnoozing ? 'Saving…' : label}
        <ChevronDown size={12} />
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-1 w-60 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg py-1">
          {!customOpen && (
            <>
              <div className="px-3 py-1.5 text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">
                Snooze until
              </div>
              {presets.map((p) => (
                <button
                  key={p.label}
                  onClick={() => fire(p.at)}
                  className="w-full flex items-center justify-between gap-2 px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-700/60 text-left"
                >
                  <span className="text-gray-900 dark:text-gray-100">{p.label}</span>
                  <span className="text-xs text-gray-500 dark:text-gray-400">{fmt(p.at)}</span>
                </button>
              ))}
              <div className="border-t border-gray-200 dark:border-gray-700 my-1" />
              <button
                onClick={() => setCustomOpen(true)}
                className="w-full px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-700/60 text-left text-gray-900 dark:text-gray-100"
              >
                Custom date &amp; time…
              </button>
            </>
          )}
          {customOpen && (
            <form onSubmit={submitCustom} className="p-3 space-y-2">
              <label className="block text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">
                Snooze until
              </label>
              <input
                autoFocus
                type="datetime-local"
                value={customValue}
                onChange={(e) => setCustomValue(e.target.value)}
                className="input text-sm w-full"
              />
              <div className="flex justify-end gap-1.5">
                <button
                  type="button"
                  onClick={() => { setCustomOpen(false); setCustomValue('') }}
                  className="text-xs px-2 py-1 rounded text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700"
                >
                  Back
                </button>
                <button
                  type="submit"
                  disabled={!customValue || new Date(customValue) <= new Date()}
                  className="text-xs px-2.5 py-1 rounded bg-violet-600 hover:bg-violet-700 text-white disabled:opacity-50"
                >
                  Snooze
                </button>
              </div>
            </form>
          )}
        </div>
      )}
    </div>
  )
}
