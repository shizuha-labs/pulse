import { format, formatDistanceToNow, isToday, isTomorrow, isYesterday, parseISO } from 'date-fns'

/**
 * Format a date string to a human-readable format
 */
export function formatDate(dateString, formatStr = 'MMM d, yyyy') {
  if (!dateString) return ''
  const date = typeof dateString === 'string' ? parseISO(dateString) : dateString
  return format(date, formatStr)
}

/**
 * Format a date to relative time (e.g., "2 hours ago")
 */
export function formatRelativeTime(dateString) {
  if (!dateString) return ''
  const date = typeof dateString === 'string' ? parseISO(dateString) : dateString
  return formatDistanceToNow(date, { addSuffix: true })
}

/**
 * Format a date for display, using "Today", "Tomorrow", "Yesterday" when appropriate
 */
export function formatSmartDate(dateString, includeTime = true) {
  if (!dateString) return ''
  const date = typeof dateString === 'string' ? parseISO(dateString) : dateString

  let dateStr
  if (isToday(date)) {
    dateStr = 'Today'
  } else if (isTomorrow(date)) {
    dateStr = 'Tomorrow'
  } else if (isYesterday(date)) {
    dateStr = 'Yesterday'
  } else {
    dateStr = format(date, 'MMM d')
  }

  if (includeTime) {
    return `${dateStr} at ${format(date, 'h:mm a')}`
  }
  return dateStr
}

/**
 * Get severity color classes
 */
export function getSeverityClasses(severity) {
  const classes = {
    critical: {
      bg: 'bg-red-100',
      text: 'text-red-800',
      border: 'border-red-300',
      dot: 'bg-red-500',
    },
    error: {
      bg: 'bg-orange-100',
      text: 'text-orange-800',
      border: 'border-orange-300',
      dot: 'bg-orange-500',
    },
    warning: {
      bg: 'bg-yellow-100',
      text: 'text-yellow-800',
      border: 'border-yellow-300',
      dot: 'bg-yellow-500',
    },
    info: {
      bg: 'bg-blue-100',
      text: 'text-blue-800',
      border: 'border-blue-300',
      dot: 'bg-blue-500',
    },
  }
  return classes[severity] || classes.info
}

/**
 * Get status color classes
 */
export function getStatusClasses(status) {
  const classes = {
    // Task statuses
    pending: { bg: 'bg-gray-100', text: 'text-gray-800' },
    in_progress: { bg: 'bg-blue-100', text: 'text-blue-800' },
    completed: { bg: 'bg-green-100', text: 'text-green-800' },
    cancelled: { bg: 'bg-gray-100', text: 'text-gray-500' },
    blocked: { bg: 'bg-red-100', text: 'text-red-800' },

    // Action statuses
    approved: { bg: 'bg-green-100', text: 'text-green-800' },
    executing: { bg: 'bg-blue-100', text: 'text-blue-800' },
    failed: { bg: 'bg-red-100', text: 'text-red-800' },
    rejected: { bg: 'bg-red-100', text: 'text-red-800' },
    expired: { bg: 'bg-gray-100', text: 'text-gray-500' },

    // Alert statuses
    firing: { bg: 'bg-red-100', text: 'text-red-800' },
    acknowledged: { bg: 'bg-yellow-100', text: 'text-yellow-800' },
    resolved: { bg: 'bg-green-100', text: 'text-green-800' },
    silenced: { bg: 'bg-gray-100', text: 'text-gray-500' },
  }
  return classes[status] || { bg: 'bg-gray-100', text: 'text-gray-800' }
}

/**
 * Get priority color classes
 */
export function getPriorityClasses(priority) {
  const classes = {
    urgent: { bg: 'bg-red-100', text: 'text-red-800' },
    high: { bg: 'bg-orange-100', text: 'text-orange-800' },
    normal: { bg: 'bg-gray-100', text: 'text-gray-800' },
    low: { bg: 'bg-gray-50', text: 'text-gray-600' },
  }
  return classes[priority] || classes.normal
}

/**
 * Format status for display
 */
export function formatStatus(status) {
  return status?.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) || ''
}

/**
 * Format mode for display
 */
export function formatMode(mode) {
  const modes = {
    task: 'Task',
    action: 'Action',
    alert: 'Alert',
  }
  return modes[mode] || mode
}

/**
 * Truncate text with ellipsis
 */
export function truncate(text, maxLength = 50) {
  if (!text || text.length <= maxLength) return text
  return text.slice(0, maxLength) + '...'
}

/**
 * Humanize a recurrence (frequency + optional custom pattern) into something
 * users recognise — "Every 4 hours" / "Daily" / "Weekly" — so the UI never
 * displays the raw "custom" token next to tasks.
 *
 * Accepts the raw DB shape: frequency ∈ {daily, weekly, monthly, yearly,
 * custom, hourly, one_time} and an optional recurrence_pattern that's either
 * a human-readable "every N unit" string or a JSON `{interval_seconds: N}`.
 *
 * Matches tasks/services/recurring.py: parse_interval_seconds on the backend.
 */
export function formatRecurrence(frequency, recurrencePattern) {
  if (!frequency || frequency === 'one_time') return ''
  const named = {
    hourly: 'Every hour',
    daily: 'Daily',
    weekly: 'Weekly',
    monthly: 'Monthly',
    yearly: 'Yearly',
  }
  if (named[frequency]) return named[frequency]
  if (frequency !== 'custom') return capitalize(frequency)
  if (!recurrencePattern) return 'Custom'
  const pattern = typeof recurrencePattern === 'string' ? recurrencePattern.trim() : ''
  if (!pattern) return 'Custom'
  // JSON form — accept any interval_* unit key: interval_seconds,
  // interval_minutes, interval_hours, interval_days, interval_weeks.
  // Agents sometimes write {"interval_hours": 4} directly.
  if (pattern.startsWith('{')) {
    try {
      const obj = JSON.parse(pattern)
      const secs = intervalDictToSeconds(obj)
      if (secs > 0) return humanizeInterval(secs)
    } catch { /* fall through */ }
  }
  // Human-readable "every N unit"
  const m = pattern.match(/^\s*every\s+(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks)\s*$/i)
  if (m) {
    const n = parseInt(m[1], 10)
    const unit = normalizeUnit(m[2].toLowerCase(), n)
    return `Every ${n} ${unit}`
  }
  return pattern  // best effort: show whatever was stored
}

function intervalDictToSeconds(obj) {
  if (!obj || typeof obj !== 'object') return 0
  const keys = [
    ['interval_weeks', 604800],
    ['interval_days', 86400],
    ['interval_hours', 3600],
    ['interval_minutes', 60],
    ['interval_seconds', 1],
  ]
  for (const [k, mult] of keys) {
    const v = obj[k]
    if (typeof v === 'number' && v > 0) return v * mult
  }
  return 0
}

function humanizeInterval(seconds) {
  const units = [
    { s: 604800, one: 'week', many: 'weeks' },
    { s: 86400,  one: 'day',  many: 'days' },
    { s: 3600,   one: 'hour', many: 'hours' },
    { s: 60,     one: 'minute', many: 'minutes' },
    { s: 1,      one: 'second', many: 'seconds' },
  ]
  for (const u of units) {
    if (seconds % u.s === 0 && seconds >= u.s) {
      const n = seconds / u.s
      return `Every ${n} ${n === 1 ? u.one : u.many}`
    }
  }
  return `Every ${seconds} seconds`
}

function normalizeUnit(raw, count) {
  const map = {
    s: 'second', sec: 'second', secs: 'second', second: 'second', seconds: 'second',
    m: 'minute', min: 'minute', mins: 'minute', minute: 'minute', minutes: 'minute',
    h: 'hour', hr: 'hour', hrs: 'hour', hour: 'hour', hours: 'hour',
    d: 'day', day: 'day', days: 'day',
    w: 'week', wk: 'week', wks: 'week', week: 'week', weeks: 'week',
  }
  const base = map[raw] || raw
  return count === 1 ? base : base + 's'
}

function capitalize(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : s
}
