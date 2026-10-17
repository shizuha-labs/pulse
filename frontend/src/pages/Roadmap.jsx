import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircle,
  Calendar,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Filter,
  FolderKanban,
  Loader2,
} from 'lucide-react'
import {
  addDays,
  addMonths,
  differenceInCalendarDays,
  eachMonthOfInterval,
  endOfMonth,
  format,
  isAfter,
  isBefore,
  parseISO,
  startOfMonth,
} from 'date-fns'
import { itemsApi, projectsApi, sprintsApi } from '../services/api'
import { cn } from '../utils/cn'

const DAY_WIDTH = 7
const ROW_HEIGHT = 44
const GROUP_HEADER_HEIGHT = 40
const STATUS_STYLES = {
  done: {
    label: 'Done',
    icon: CheckCircle2,
    bar: 'bg-emerald-500',
    iconColor: 'text-emerald-500',
    badge: 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-200 dark:border-emerald-800',
  },
  behind: {
    label: 'Behind',
    icon: AlertCircle,
    bar: 'bg-red-500',
    iconColor: 'text-red-500',
    badge: 'bg-red-50 text-red-700 border-red-200 dark:bg-red-950 dark:text-red-200 dark:border-red-800',
  },
  at_risk: {
    label: 'At risk',
    icon: Clock,
    bar: 'bg-amber-500',
    iconColor: 'text-amber-500',
    badge: 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-200 dark:border-amber-800',
  },
  on_track: {
    label: 'On track',
    icon: Clock,
    bar: 'bg-brand-500',
    iconColor: 'text-brand-500',
    badge: 'bg-brand-50 text-brand-700 border-brand-200 dark:bg-brand-950 dark:text-brand-200 dark:border-brand-800',
  },
}

const RANGE_OPTIONS = [
  { value: 3, label: '3 months' },
  { value: 6, label: '6 months' },
  { value: 12, label: '12 months' },
]

function listFromResponse(data) {
  if (Array.isArray(data)) return data
  if (Array.isArray(data?.results)) return data.results
  if (Array.isArray(data?.items)) return data.items
  return []
}

function parseDate(value) {
  if (!value) return null
  const parsed = typeof value === 'string' ? parseISO(value) : new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value))
}

function deriveStatus(item, today) {
  const done = item.status_category === 'done' || ['done', 'completed', 'resolved', 'closed'].includes(item.status)
  if (done) return 'done'

  const due = parseDate(item.due_date)
  if (due && isBefore(due, today)) return 'behind'

  const percent = Number(item.percent_complete ?? 0)
  if (due && differenceInCalendarDays(due, today) <= 7 && percent < 80) return 'at_risk'

  return 'on_track'
}

function getTimelineDates(item) {
  const due = parseDate(item.due_date)
  const start = parseDate(item.start_date)
  const created = parseDate(item.created_at)

  if (!start && !due) return null

  let rangeStart = start || created || due
  let rangeEnd = due || start || created

  if (rangeStart && rangeEnd && isAfter(rangeStart, rangeEnd)) {
    const swap = rangeStart
    rangeStart = rangeEnd
    rangeEnd = swap
  }

  if (!start && due) {
    const estimateDays = item.story_points ? Math.max(3, Math.min(30, Number(item.story_points) * 2)) : 14
    rangeStart = addDays(due, -estimateDays)
  }

  if (rangeStart && rangeEnd && differenceInCalendarDays(rangeEnd, rangeStart) === 0) {
    rangeEnd = addDays(rangeStart, 1)
  }

  return { start: rangeStart, end: rangeEnd }
}

function itemLabel(item) {
  return [item.item_key, item.title].filter(Boolean).join(' · ')
}

export default function Roadmap() {
  const navigate = useNavigate()
  const [rangeMonths, setRangeMonths] = useState(6)
  const [viewStart, setViewStart] = useState(() => startOfMonth(new Date()))
  const [selectedProject, setSelectedProject] = useState('')
  const [swimlane, setSwimlane] = useState('project')

  const viewEnd = useMemo(() => endOfMonth(addMonths(viewStart, rangeMonths - 1)), [viewStart, rangeMonths])
  const totalDays = Math.max(1, differenceInCalendarDays(viewEnd, viewStart) + 1)
  const timelineWidth = totalDays * DAY_WIDTH
  const monthTicks = useMemo(() => eachMonthOfInterval({ start: viewStart, end: viewEnd }), [viewStart, viewEnd])

  const projectsQuery = useQuery({
    queryKey: ['roadmap-projects'],
    queryFn: () => projectsApi.listAll(),
    staleTime: 5 * 60 * 1000,
  })

  const itemsQuery = useQuery({
    queryKey: ['roadmap-items', selectedProject],
    queryFn: async () => {
      const params = { mode: 'task', page_size: 1000, include_archived: 'false' }
      if (selectedProject) params.project = selectedProject
      const res = await itemsApi.list(params)
      return listFromResponse(res.data)
    },
    staleTime: 60 * 1000,
  })

  const sprintsQuery = useQuery({
    queryKey: ['roadmap-sprints', selectedProject],
    queryFn: async () => {
      const params = { page_size: 500 }
      if (selectedProject) params.project = selectedProject
      const res = await sprintsApi.list(params)
      return listFromResponse(res.data)
    },
    staleTime: 5 * 60 * 1000,
  })

  const today = useMemo(() => new Date(), [])
  const rows = useMemo(() => {
    const groups = new Map()
    for (const item of itemsQuery.data || []) {
      const dates = getTimelineDates(item)
      if (!dates) continue
      if (isBefore(dates.end, viewStart) || isAfter(dates.start, viewEnd)) continue

      const groupKey = swimlane === 'team'
        ? (item.assignment_group || 'Unassigned team')
        : (item.project_name || item.project_key || 'No project')
      const groupMeta = groups.get(groupKey) || { key: groupKey, items: [] }
      groupMeta.items.push({ ...item, timeline: dates, roadmapStatus: deriveStatus(item, today) })
      groups.set(groupKey, groupMeta)
    }

    return [...groups.values()]
      .map((group) => ({
        ...group,
        items: group.items.sort((a, b) => a.timeline.start - b.timeline.start),
      }))
      .sort((a, b) => {
        const aStart = a.items[0]?.timeline.start?.getTime() || 0
        const bStart = b.items[0]?.timeline.start?.getTime() || 0
        return aStart - bStart || a.key.localeCompare(b.key)
      })
  }, [itemsQuery.data, swimlane, today, viewEnd, viewStart])

  const milestoneMarkers = useMemo(() => {
    return (sprintsQuery.data || [])
      .map((sprint) => ({
        id: sprint.id,
        name: sprint.name,
        date: parseDate(sprint.end_date),
        project: sprint.project_key || sprint.project_name,
      }))
      .filter((marker) => marker.date && !isBefore(marker.date, viewStart) && !isAfter(marker.date, viewEnd))
  }, [sprintsQuery.data, viewEnd, viewStart])

  const scheduledCount = rows.reduce((sum, group) => sum + group.items.length, 0)
  const loading = projectsQuery.isLoading || itemsQuery.isLoading || sprintsQuery.isLoading
  const error = projectsQuery.error || itemsQuery.error || sprintsQuery.error

  const offsetForDate = (date) => clamp(differenceInCalendarDays(date, viewStart), 0, totalDays) * DAY_WIDTH
  const widthForRange = (start, end) => Math.max(16, (differenceInCalendarDays(end, start) + 1) * DAY_WIDTH)

  return (
    <div className="p-6 space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
            <FolderKanban size={16} />
            Enterprise roadmap
          </div>
          <h1 className="mt-1 text-2xl font-bold text-gray-900 dark:text-white">Roadmap timeline</h1>
          <p className="mt-1 max-w-3xl text-sm text-gray-600 dark:text-gray-400">
            Read-only Gantt-style view over existing Pulse start dates, due dates, projects, sprints, and workflow status.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setViewStart(startOfMonth(addMonths(viewStart, -rangeMonths)))}
            className="btn-secondary"
            aria-label="Previous range"
          >
            <ChevronLeft size={16} />
          </button>
          <button
            type="button"
            onClick={() => setViewStart(startOfMonth(new Date()))}
            className="btn-secondary"
          >
            Today
          </button>
          <button
            type="button"
            onClick={() => setViewStart(startOfMonth(addMonths(viewStart, rangeMonths)))}
            className="btn-secondary"
            aria-label="Next range"
          >
            <ChevronRight size={16} />
          </button>
        </div>
      </div>

      <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-800 dark:bg-gray-900">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300">
              <Filter size={16} />
              Filters
            </div>
            <select
              value={selectedProject}
              onChange={(e) => setSelectedProject(e.target.value)}
              className="input w-64"
            >
              <option value="">All projects</option>
              {(projectsQuery.data || []).map((project) => (
                <option key={project.id} value={project.id}>{project.key} — {project.name}</option>
              ))}
            </select>
            <select
              value={swimlane}
              onChange={(e) => setSwimlane(e.target.value)}
              className="input w-44"
            >
              <option value="project">Project swimlanes</option>
              <option value="team">Team swimlanes</option>
            </select>
            <select
              value={rangeMonths}
              onChange={(e) => setRangeMonths(Number(e.target.value))}
              className="input w-36"
            >
              {RANGE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            {Object.entries(STATUS_STYLES).map(([key, style]) => (
              <span key={key} className={cn('inline-flex items-center gap-1 rounded-full border px-2 py-1', style.badge)}>
                <span className={cn('h-2 w-2 rounded-full', style.bar)} />
                {style.label}
              </span>
            ))}
          </div>
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
          Failed to load roadmap data. Refresh or check the Pulse API health.
        </div>
      )}

      <div className="rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-800 dark:bg-gray-900">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-200 px-4 py-3 dark:border-gray-800">
          <div>
            <div className="font-semibold text-gray-900 dark:text-white">
              {format(viewStart, 'MMM yyyy')} – {format(viewEnd, 'MMM yyyy')}
            </div>
            <div className="text-sm text-gray-500 dark:text-gray-400">
              {scheduledCount} scheduled item{scheduledCount === 1 ? '' : 's'} · {milestoneMarkers.length} sprint milestone{milestoneMarkers.length === 1 ? '' : 's'}
            </div>
          </div>
          {loading && (
            <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
              <Loader2 size={16} className="animate-spin" /> Loading roadmap…
            </div>
          )}
        </div>

        {!loading && scheduledCount === 0 ? (
          <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
            <Calendar size={42} className="mb-3 text-gray-400" />
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">No scheduled roadmap items</h2>
            <p className="mt-1 max-w-md text-sm text-gray-500 dark:text-gray-400">
              Add start dates and due dates to epics, tasks, or releases to populate the roadmap timeline.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <div className="min-w-full" style={{ width: Math.max(900, timelineWidth + 320) }}>
              <div className="sticky top-0 z-10 grid grid-cols-[320px_1fr] border-b border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-950">
                <div className="border-r border-gray-200 px-4 py-3 text-sm font-medium text-gray-700 dark:border-gray-800 dark:text-gray-300">
                  {swimlane === 'team' ? 'Team / workstream' : 'Project'}
                </div>
                <div className="relative h-12" style={{ width: timelineWidth }}>
                  {monthTicks.map((month) => {
                    const left = offsetForDate(month)
                    const next = addMonths(month, 1)
                    const width = Math.max(60, (differenceInCalendarDays(next, month)) * DAY_WIDTH)
                    return (
                      <div
                        key={month.toISOString()}
                        className="absolute top-0 h-full border-l border-gray-200 px-2 py-3 text-xs font-medium uppercase tracking-wide text-gray-500 dark:border-gray-800 dark:text-gray-400"
                        style={{ left, width }}
                      >
                        {format(month, 'MMM yyyy')}
                      </div>
                    )
                  })}
                  <div
                    className="absolute bottom-0 top-0 w-px bg-brand-500/70"
                    style={{ left: offsetForDate(today) }}
                    title="Today"
                  />
                </div>
              </div>

              <div className="relative">
                {milestoneMarkers.map((marker) => (
                  <div
                    key={marker.id}
                    className="pointer-events-none absolute top-0 z-0 h-full border-l border-dashed border-purple-300 dark:border-purple-700"
                    style={{ left: 320 + offsetForDate(marker.date) }}
                    title={`${marker.name} · ${format(marker.date, 'MMM d')}`}
                  />
                ))}

                {rows.map((group) => (
                  <div key={group.key}>
                    <div className="grid grid-cols-[320px_1fr] border-b border-gray-200 bg-gray-50/80 dark:border-gray-800 dark:bg-gray-950/80" style={{ height: GROUP_HEADER_HEIGHT }}>
                      <div className="flex items-center gap-2 border-r border-gray-200 px-4 text-sm font-semibold text-gray-900 dark:border-gray-800 dark:text-white">
                        <FolderKanban size={15} className="text-brand-500" />
                        <span className="truncate">{group.key}</span>
                        <span className="rounded-full bg-gray-200 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">{group.items.length}</span>
                      </div>
                      <div className="relative" style={{ width: timelineWidth }} />
                    </div>

                    {group.items.map((item) => {
                      const style = STATUS_STYLES[item.roadmapStatus]
                      const StatusIcon = style.icon
                      const left = offsetForDate(item.timeline.start)
                      const width = widthForRange(item.timeline.start, item.timeline.end)
                      return (
                        <div key={item.id} className="grid grid-cols-[320px_1fr] border-b border-gray-100 dark:border-gray-800" style={{ height: ROW_HEIGHT }}>
                          <button
                            type="button"
                            onClick={() => navigate(`/tasks/${item.item_key || item.id}`)}
                            className="flex min-w-0 items-center gap-2 border-r border-gray-200 px-4 text-left hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-800/60"
                          >
                            <StatusIcon size={15} className={cn('flex-shrink-0', style.iconColor)} />
                            <div className="min-w-0">
                              <div className="truncate text-sm font-medium text-gray-900 dark:text-white">{itemLabel(item)}</div>
                              <div className="truncate text-xs text-gray-500 dark:text-gray-400">
                                {item.assignment_group || 'Unassigned'} · {item.status}
                              </div>
                            </div>
                          </button>
                          <div className="relative" style={{ width: timelineWidth }}>
                            <button
                              type="button"
                              onClick={() => navigate(`/tasks/${item.item_key || item.id}`)}
                              className={cn('absolute top-2 h-7 rounded-full px-3 text-left text-xs font-medium text-white shadow-sm transition hover:brightness-110', style.bar)}
                              style={{ left, width }}
                              title={`${itemLabel(item)} · ${format(item.timeline.start, 'MMM d')} → ${format(item.timeline.end, 'MMM d')} · ${style.label}`}
                            >
                              <span className="block truncate">{item.item_key || item.title}</span>
                            </button>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="rounded-xl border border-blue-200 bg-blue-50 p-4 text-sm text-blue-800 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200">
        Phase 1 is read-only by design: it makes scheduled work visible without adding a new scheduling write path. Drag-to-schedule and dependency editing can build on this route later.
      </div>
    </div>
  )
}
