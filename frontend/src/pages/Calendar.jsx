import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  ChevronLeft,
  ChevronRight,
  Calendar as CalendarIcon,
  Loader2,
  AlertCircle,
  Filter,
  Clock,
  CheckCircle2,
  Repeat,
} from 'lucide-react'
import {
  format,
  startOfMonth,
  endOfMonth,
  startOfWeek,
  endOfWeek,
  addMonths,
  subMonths,
  addDays,
  addWeeks,
  addMonths as addMonthsFn,
  addYears,
  eachDayOfInterval,
  isSameMonth,
  isSameDay,
  isToday,
  isBefore,
  isAfter,
  parseISO,
  getDay,
  getDate,
  getMonth,
  getYear,
} from 'date-fns'
import { cn } from '../utils/cn'
import { itemsApi, projectsApi, tasksApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import MultiSelectFilter from '../components/MultiSelectFilter'

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

const PRIORITY_COLORS = {
  urgent: 'bg-red-500',
  high: 'bg-orange-500',
  normal: 'bg-blue-500',
  low: 'bg-gray-400',
}

const STATUS_ICONS = {
  completed: CheckCircle2,
  done: CheckCircle2,
  in_progress: Clock,
}

/**
 * Expand a recurring task's pattern into the set of Date occurrences
 * that fall within [rangeStart, rangeEnd]. The calendar only has
 * day-level granularity so sub-day frequencies (e.g. "every 4 hours")
 * collapse to "once per day".
 *
 *   daily    → every day in range
 *   weekly   → every occurrence of the weekday(s) anchored by due_date
 *   monthly  → day-of-month from due_date, within range
 *   yearly   → month+day from due_date, within range
 *   custom   → best-effort: if pattern text contains "hour"/"minute"/"daily",
 *              treat as daily; otherwise fall back to the single due_date.
 *
 * Respects `recurrence_end_date` as an upper bound.
 */
function getRecurringOccurrences(task, rangeStart, rangeEnd) {
  if (!task?.is_recurring || !task.due_date) return []

  const baseDate = parseISO(task.due_date)
  if (isNaN(baseDate.getTime())) return []

  const endCap = task.recurrence_end_date ? parseISO(task.recurrence_end_date) : null
  const effectiveEnd = (endCap && isBefore(endCap, rangeEnd)) ? endCap : rangeEnd

  const freq = (task.frequency || '').toLowerCase()
  const occurrences = []

  if (freq === 'daily') {
    // Every day from max(rangeStart, baseDate's date, first-day-of-start) to effectiveEnd.
    const start = isBefore(rangeStart, baseDate) ? baseDate : rangeStart
    for (let d = start; !isAfter(d, effectiveEnd); d = addDays(d, 1)) {
      occurrences.push(d)
    }
  } else if (freq === 'weekly') {
    // Walk backwards to find the earliest occurrence still >= rangeStart,
    // then walk forward one week at a time until we pass effectiveEnd.
    let d = baseDate
    // Back-walk: subtract weeks until we're at or before rangeStart.
    while (isAfter(d, rangeStart)) {
      const prev = addWeeks(d, -1)
      if (isBefore(prev, rangeStart)) { d = addWeeks(d, -1); break }
      d = prev
    }
    // Forward-walk: collect occurrences in range.
    while (!isAfter(d, effectiveEnd)) {
      if (!isBefore(d, rangeStart) && !isAfter(d, effectiveEnd)) {
        occurrences.push(d)
      }
      d = addWeeks(d, 1)
    }
  } else if (freq === 'monthly') {
    // Same day-of-month as due_date, every month in range.
    const dom = getDate(baseDate)
    let monthCursor = new Date(getYear(rangeStart), getMonth(rangeStart), 1)
    while (!isAfter(monthCursor, rangeEnd)) {
      // Clamp dom to last day of month (e.g. 31st → 28/29/30 for shorter months).
      const lastDayOfMonth = new Date(getYear(monthCursor), getMonth(monthCursor) + 1, 0).getDate()
      const day = Math.min(dom, lastDayOfMonth)
      const candidate = new Date(getYear(monthCursor), getMonth(monthCursor), day,
        baseDate.getHours(), baseDate.getMinutes())
      if (!isBefore(candidate, rangeStart) && !isAfter(candidate, effectiveEnd)) {
        occurrences.push(candidate)
      }
      monthCursor = addMonthsFn(monthCursor, 1)
    }
  } else if (freq === 'yearly') {
    const mo = getMonth(baseDate)
    const dom = getDate(baseDate)
    let yearCursor = getYear(rangeStart)
    const endYear = getYear(effectiveEnd)
    while (yearCursor <= endYear) {
      const candidate = new Date(yearCursor, mo, dom, baseDate.getHours(), baseDate.getMinutes())
      if (!isBefore(candidate, rangeStart) && !isAfter(candidate, effectiveEnd)) {
        occurrences.push(candidate)
      }
      yearCursor += 1
    }
  } else {
    // 'custom' or unknown — try to classify the pattern text. Anything
    // sub-daily collapses to a daily occurrence since the calendar is
    // day-granularity. If we can't classify, just show the base date.
    const patternText = String(task.recurrence_pattern || '').toLowerCase()
    const isSubDaily = /\b(hour|minute|min\b|every \d+\s*h)/.test(patternText)
    const isDaily = /\b(daily|every day)\b/.test(patternText) || isSubDaily
    if (isDaily) {
      const start = isBefore(rangeStart, baseDate) ? baseDate : rangeStart
      for (let d = start; !isAfter(d, effectiveEnd); d = addDays(d, 1)) {
        occurrences.push(d)
      }
    } else if (isBefore(baseDate, effectiveEnd) && !isBefore(baseDate, rangeStart)) {
      occurrences.push(baseDate)
    }
  }

  return occurrences
}

export default function Calendar() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const [currentDate, setCurrentDate] = useState(new Date())
  const [selectedProject, setSelectedProject] = useState('')
  const [selectedPriority, setSelectedPriority] = useState([])
  const [showCompleted, setShowCompleted] = useState(false)

  // Default assignee = current user (Linear / Jira default: "My calendar").
  // Empty = "everyone visible to me".
  const [assigneeFilter, setAssigneeFilter] = useState(() => (user?.id ? [String(user.id)] : []))
  const [reporterFilter, setReporterFilter] = useState([])

  // Calculate the date range for the current view
  const monthStart = startOfMonth(currentDate)
  const monthEnd = endOfMonth(currentDate)
  const calendarStart = startOfWeek(monthStart)
  const calendarEnd = endOfWeek(monthEnd)

  // Get all days to display in the calendar grid
  const calendarDays = useMemo(() => {
    return eachDayOfInterval({ start: calendarStart, end: calendarEnd })
  }, [calendarStart, calendarEnd])

  // Fetch projects for filter dropdown
  const { data: projectsData } = useQuery({
    queryKey: ['projects', 'calendar'],
    queryFn: async () => {
      const res = await projectsApi.list({ page_size: 1000 })
      return res.data.results || res.data || []
    },
  })
  const projects = projectsData || []

  // Fetch the assignee roster once — re-used for the assignee/reporter filters.
  const { data: assignableUsersData } = useQuery({
    queryKey: ['assignable-users-calendar'],
    queryFn: async () => {
      const res = await tasksApi.assignableUsers()
      return res.data?.users || res.data || []
    },
    staleTime: 5 * 60 * 1000,
  })
  const assignableUsers = assignableUsersData || []

  // Build the query params shared by both fetches (in-range tasks + recurring tasks).
  const commonParams = useMemo(() => {
    const p = { mode: 'task', page_size: 500 }
    if (selectedProject) p.project = selectedProject
    if (selectedPriority.length > 0) p.priority = selectedPriority.join(',')
    // Done/in-progress scope:
    // - default hides done-category items (Open scope),
    // - `Show completed` opt-in includes them.
    if (!showCompleted) p.is_active = 'true'
    // Assignee: comma-separated ids + emails (same convention as Tasks page).
    if (assigneeFilter.length > 0) {
      const ids = assigneeFilter.filter(v => !v.startsWith('email:'))
      const emails = assigneeFilter.filter(v => v.startsWith('email:')).map(v => v.slice(6))
      if (ids.length) p.assignee_id = ids.join(',')
      if (emails.length) p.assignee_email = emails.join(',')
    }
    if (reporterFilter.length > 0) {
      const ids = reporterFilter.filter(v => !v.startsWith('email:'))
      const emails = reporterFilter.filter(v => v.startsWith('email:')).map(v => v.slice(6))
      if (ids.length) p.reporter_id = ids.join(',')
      if (emails.length) p.reporter_email = emails.join(',')
    }
    return p
  }, [selectedProject, selectedPriority, showCompleted, assigneeFilter, reporterFilter])

  // Query 1: tasks with a due_date inside the visible range.
  // Excludes recurring tasks (those come from Query 2) so we don't double-count.
  const { data: inRangeTasks = [], isLoading: loading1, error: error1 } = useQuery({
    queryKey: [
      'calendar-in-range',
      format(calendarStart, 'yyyy-MM-dd'),
      format(calendarEnd, 'yyyy-MM-dd'),
      commonParams,
    ],
    queryFn: async () => {
      const res = await itemsApi.list({
        ...commonParams,
        due_after: format(calendarStart, 'yyyy-MM-dd'),
        due_before: format(calendarEnd, 'yyyy-MM-dd'),
        is_recurring: 'false',
      })
      return res.data.results || res.data || []
    },
  })

  // Query 2: all recurring tasks (no due_date filter) within the current scope.
  // We expand occurrences client-side.
  const { data: recurringTasks = [], isLoading: loading2, error: error2 } = useQuery({
    queryKey: ['calendar-recurring', commonParams],
    queryFn: async () => {
      const res = await itemsApi.list({
        ...commonParams,
        is_recurring: 'true',
      })
      return res.data.results || res.data || []
    },
  })

  const isLoading = loading1 || loading2
  const error = error1 || error2

  // Group (task, occurrence-date) pairs by date.
  // - One-off tasks: one entry per due_date.
  // - Recurring tasks: expanded via getRecurringOccurrences and shown on
  //   every occurrence day in the visible range.
  const { tasksByDate, totalCount } = useMemo(() => {
    const grouped = {}
    let count = 0

    // One-off tasks: anchor on due_date.
    inRangeTasks.forEach((task) => {
      if (!task.due_date) return
      const dateKey = format(parseISO(task.due_date), 'yyyy-MM-dd')
      if (!grouped[dateKey]) grouped[dateKey] = []
      grouped[dateKey].push({ task, occurrence: parseISO(task.due_date), isRecurring: false })
      count += 1
    })

    // Recurring tasks: expand.
    recurringTasks.forEach((task) => {
      const occurrences = getRecurringOccurrences(task, calendarStart, calendarEnd)
      occurrences.forEach((occ) => {
        const dateKey = format(occ, 'yyyy-MM-dd')
        if (!grouped[dateKey]) grouped[dateKey] = []
        grouped[dateKey].push({ task, occurrence: occ, isRecurring: true })
      })
      if (occurrences.length > 0) count += 1  // count the unique task once
    })

    // Sort within each day by priority.
    const priorityOrder = { urgent: 0, high: 1, normal: 2, low: 3 }
    Object.values(grouped).forEach((entries) => {
      entries.sort((a, b) =>
        (priorityOrder[a.task.priority] ?? 2) - (priorityOrder[b.task.priority] ?? 2)
      )
    })

    return { tasksByDate: grouped, totalCount: count }
  }, [inRangeTasks, recurringTasks, calendarStart, calendarEnd])

  const goToPreviousMonth = () => setCurrentDate(subMonths(currentDate, 1))
  const goToNextMonth = () => setCurrentDate(addMonths(currentDate, 1))
  const goToToday = () => setCurrentDate(new Date())

  const handleTaskClick = (task, e) => {
    e.stopPropagation()
    // Navigate by item_key when available (prettier URL), falling back to id.
    navigate(`/tasks/${task.item_key || task.id}`)
  }

  // Priority options for the multi-select filter.
  const priorityOptions = useMemo(() => ([
    { value: 'urgent', label: 'Urgent' },
    { value: 'high',   label: 'High' },
    { value: 'normal', label: 'Normal' },
    { value: 'low',    label: 'Low' },
  ]), [])

  // Assignee options (default: "Me" pinned first, then every assignable user).
  const assigneeOptions = useMemo(() => ([
    ...(user?.id ? [{
      value: String(user.id),
      label: 'My calendar',
      sublabel: user.email,
      avatar: (user.email || '?')[0].toUpperCase(),
    }] : []),
    ...assignableUsers
      .filter(u => (u.user_id || u.email) && u.user_id !== user?.id)
      .map(u => ({
        value: u.user_id ? String(u.user_id) : `email:${u.email}`,
        label: u.user_name || u.name || u.email?.split('@')[0] || u.email,
        sublabel: u.email,
        avatar: ((u.user_name || u.name || u.email || '?')[0] || '?').toUpperCase(),
      })),
  ]), [assignableUsers, user])

  const reporterOptions = useMemo(() => ([
    ...(user?.id ? [{
      value: String(user.id),
      label: 'Reported by me',
      sublabel: user.email,
      avatar: (user.email || '?')[0].toUpperCase(),
    }] : []),
    ...assignableUsers
      .filter(u => (u.user_id || u.email) && u.user_id !== user?.id)
      .map(u => ({
        value: u.user_id ? String(u.user_id) : `email:${u.email}`,
        label: u.user_name || u.name || u.email?.split('@')[0] || u.email,
        sublabel: u.email,
        avatar: ((u.user_name || u.name || u.email || '?')[0] || '?').toUpperCase(),
      })),
  ]), [assignableUsers, user])

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div className="flex items-center gap-3">
          <CalendarIcon className="h-6 w-6 text-brand-600 dark:text-brand-400" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Calendar</h1>
        </div>

        {/* Navigation Controls */}
        <div className="flex items-center gap-2">
          <button onClick={goToPreviousMonth} className="btn-icon" title="Previous month">
            <ChevronLeft size={20} />
          </button>
          <button onClick={goToToday} className="btn-secondary text-sm">Today</button>
          <button onClick={goToNextMonth} className="btn-icon" title="Next month">
            <ChevronRight size={20} />
          </button>
          <span className="ml-2 text-lg font-semibold text-gray-900 dark:text-gray-100">
            {format(currentDate, 'MMMM yyyy')}
          </span>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3 p-3 bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-800">
        <Filter size={16} className="text-gray-500" />

        <select
          value={selectedProject}
          onChange={(e) => setSelectedProject(e.target.value)}
          className="input py-1.5 text-sm w-40"
        >
          <option value="">All Projects</option>
          {projects.map((project) => (
            <option key={project.id} value={project.id}>{project.name}</option>
          ))}
        </select>

        <MultiSelectFilter
          label="Any Priority"
          searchPlaceholder="Search priorities..."
          selectedValues={selectedPriority}
          onChange={setSelectedPriority}
          options={priorityOptions}
        />

        <MultiSelectFilter
          label="My calendar"
          searchPlaceholder="Search assignees..."
          selectedValues={assigneeFilter}
          onChange={setAssigneeFilter}
          options={assigneeOptions}
        />

        <MultiSelectFilter
          label="All Reporters"
          searchPlaceholder="Search reporters..."
          selectedValues={reporterFilter}
          onChange={setReporterFilter}
          options={reporterOptions}
        />

        <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input
            type="checkbox"
            checked={showCompleted}
            onChange={(e) => setShowCompleted(e.target.checked)}
            className="rounded border-gray-300 dark:border-gray-600"
          />
          Show completed
        </label>

        <div className="ml-auto text-sm text-gray-500 dark:text-gray-400">
          {totalCount} task{totalCount !== 1 ? 's' : ''}
        </div>
      </div>

      {/* Loading / Error states */}
      {isLoading && (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="h-6 w-6 animate-spin text-brand-600" />
          <span className="ml-2 text-gray-600 dark:text-gray-400">Loading tasks...</span>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 p-4 bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 rounded-lg">
          <AlertCircle size={20} />
          <span>Failed to load tasks: {error.message}</span>
        </div>
      )}

      {/* Calendar Grid */}
      {!isLoading && !error && (
        <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-800 overflow-hidden">
          {/* Weekday headers */}
          <div className="grid grid-cols-7 border-b border-gray-200 dark:border-gray-800">
            {WEEKDAYS.map((day) => (
              <div
                key={day}
                className="py-2 text-center text-sm font-medium text-gray-500 dark:text-gray-400"
              >
                {day}
              </div>
            ))}
          </div>

          {/* Calendar days */}
          <div className="grid grid-cols-7">
            {calendarDays.map((day, index) => {
              const dateKey = format(day, 'yyyy-MM-dd')
              const dayEntries = tasksByDate[dateKey] || []
              const isCurrentMonth = isSameMonth(day, currentDate)
              const isCurrentDay = isToday(day)

              return (
                <div
                  key={dateKey}
                  className={cn(
                    'min-h-[120px] p-1 border-b border-r border-gray-100 dark:border-gray-800',
                    index % 7 === 6 && 'border-r-0',
                    !isCurrentMonth && 'bg-gray-50 dark:bg-gray-950'
                  )}
                >
                  {/* Day number */}
                  <div
                    className={cn(
                      'flex items-center justify-center w-7 h-7 text-sm font-medium rounded-full mb-1',
                      isCurrentDay
                        ? 'bg-brand-600 text-white'
                        : isCurrentMonth
                        ? 'text-gray-900 dark:text-gray-100'
                        : 'text-gray-400 dark:text-gray-600'
                    )}
                  >
                    {format(day, 'd')}
                  </div>

                  {/* Tasks for this day */}
                  <div className="space-y-0.5 overflow-hidden">
                    {dayEntries.slice(0, 3).map((entry, entryIdx) => {
                      const { task, isRecurring } = entry
                      const StatusIcon = STATUS_ICONS[task.status]
                      const isDone = task.status_category === 'done' || task.status === 'completed'
                      return (
                        <button
                          key={`${task.id}-${entryIdx}`}
                          onClick={(e) => handleTaskClick(task, e)}
                          className={cn(
                            'w-full text-left px-1.5 py-0.5 rounded text-xs truncate transition-colors',
                            'hover:ring-2 hover:ring-brand-500 hover:ring-offset-1',
                            isDone
                              ? 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 line-through'
                              : 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100'
                          )}
                          title={`${task.item_key || `#${task.id}`}: ${task.title}`}
                        >
                          <span className="flex items-center gap-1">
                            <span
                              className={cn(
                                'w-1.5 h-1.5 rounded-full flex-shrink-0',
                                PRIORITY_COLORS[task.priority] || PRIORITY_COLORS.normal
                              )}
                            />
                            {isRecurring && (
                              <Repeat size={10} className="flex-shrink-0 text-gray-500" />
                            )}
                            {StatusIcon && !isRecurring && (
                              <StatusIcon size={10} className="flex-shrink-0 text-gray-500" />
                            )}
                            <span className="truncate">{task.title}</span>
                          </span>
                        </button>
                      )
                    })}
                    {dayEntries.length > 3 && (
                      <div className="text-xs text-gray-500 dark:text-gray-400 px-1.5">
                        +{dayEntries.length - 3} more
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Legend */}
      <div className="flex flex-wrap items-center gap-4 text-sm text-gray-600 dark:text-gray-400">
        <span className="font-medium">Priority:</span>
        <div className="flex items-center gap-1">
          <span className={cn('w-2 h-2 rounded-full', PRIORITY_COLORS.urgent)} />
          <span>Urgent</span>
        </div>
        <div className="flex items-center gap-1">
          <span className={cn('w-2 h-2 rounded-full', PRIORITY_COLORS.high)} />
          <span>High</span>
        </div>
        <div className="flex items-center gap-1">
          <span className={cn('w-2 h-2 rounded-full', PRIORITY_COLORS.normal)} />
          <span>Normal</span>
        </div>
        <div className="flex items-center gap-1">
          <span className={cn('w-2 h-2 rounded-full', PRIORITY_COLORS.low)} />
          <span>Low</span>
        </div>
        <span className="mx-2 text-gray-300 dark:text-gray-600">|</span>
        <div className="flex items-center gap-1">
          <Repeat size={12} className="text-gray-500" />
          <span>Recurring occurrence</span>
        </div>
      </div>
    </div>
  )
}
