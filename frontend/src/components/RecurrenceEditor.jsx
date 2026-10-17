/**
 * RecurrenceEditor — inline form for editing a task's recurrence rule.
 *
 * UX pattern borrowed from Todoist / Asana / ClickUp: frequency dropdown
 * (Does not repeat / Daily / Weekly / Monthly / Yearly / Custom) and — when
 * Custom is selected — a free-text pattern input ("every 4 hours",
 * "every 30 minutes"). Save emits an update object of the shape the Pulse
 * API expects: { is_recurring, frequency, recurrence_pattern }.
 *
 * Rendered as a controlled popover by the caller (sidebar row OR the
 * Scheduled banner). The caller decides onCancel behaviour.
 */
import { useState } from 'react'
import { formatRecurrence } from '../utils/format'

/**
 * Turn a stored pattern (JSON or human form) into a human-editable
 * "every N unit" string for display in the Custom input. Falls back to
 * the raw stored value if we can't parse it.
 */
function humanizePattern(pattern) {
  if (!pattern) return ''
  const raw = typeof pattern === 'string' ? pattern.trim() : ''
  if (!raw) return ''
  // formatRecurrence returns strings like "Every 4 hours" for custom
  // frequencies — lowercase it to get back a valid "every N unit" form.
  const humanized = formatRecurrence('custom', raw)
  if (humanized && humanized !== 'Custom') return humanized.toLowerCase()
  return raw
}

const FREQUENCY_PRESETS = [
  { value: 'one_time', label: 'Does not repeat' },
  { value: 'daily',    label: 'Daily' },
  { value: 'weekly',   label: 'Weekly' },
  { value: 'monthly',  label: 'Monthly' },
  { value: 'yearly',   label: 'Yearly' },
  { value: 'custom',   label: 'Custom…' },
]

export default function RecurrenceEditor({
  task,
  onSave,
  onCancel,
  isSaving = false,
  className = '',
}) {
  const [frequency, setFrequency] = useState(
    task.is_recurring ? (task.frequency || 'daily') : 'one_time'
  )
  // Display JSON patterns ({"interval_hours": 4}) as their human form
  // ("every 4 hours") — both parse identically on the backend, and the
  // human form is what a user would type.
  const [pattern, setPattern] = useState(humanizePattern(task.recurrence_pattern))

  const submit = (e) => {
    e.preventDefault()
    const patch = {}
    if (frequency === 'one_time') {
      patch.is_recurring = false
      patch.frequency = 'one_time'
      patch.recurrence_pattern = ''
    } else {
      patch.is_recurring = true
      patch.frequency = frequency
      patch.recurrence_pattern = frequency === 'custom' ? pattern.trim() : ''
    }
    onSave(patch)
  }

  return (
    <form onSubmit={submit} className={`space-y-2 ${className}`}>
      <div className="flex items-center gap-2">
        <label className="text-xs text-gray-600 dark:text-gray-400 w-16 flex-shrink-0">Repeats</label>
        <select
          value={frequency}
          onChange={(e) => setFrequency(e.target.value)}
          className="input text-xs flex-1"
        >
          {FREQUENCY_PRESETS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>
      {frequency === 'custom' && (
        <div className="flex items-center gap-2">
          <label className="text-xs text-gray-600 dark:text-gray-400 w-16 flex-shrink-0">Every</label>
          <input
            type="text"
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
            placeholder="every 4 hours"
            className="input text-xs flex-1"
          />
        </div>
      )}
      <div className="flex justify-end gap-1.5 pt-1">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="text-xs px-2 py-1 rounded text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
        )}
        <button
          type="submit"
          disabled={isSaving || (frequency === 'custom' && !pattern.trim())}
          className="text-xs px-2.5 py-1 rounded bg-brand-600 hover:bg-brand-700 text-white disabled:opacity-50"
        >
          {isSaving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </form>
  )
}
