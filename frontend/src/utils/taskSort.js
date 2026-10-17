export const DEFAULT_TASK_SORT = 'priority-desc'

export const PRIORITY_ORDER = { urgent: 4, high: 3, normal: 2, low: 1 }

export function compareTasksBySort(sortOption = DEFAULT_TASK_SORT) {
  const [field, order] = sortOption.split('-')
  const multiplier = order === 'asc' ? 1 : -1

  return (a, b) => {
    if (field === 'priority') {
      const aPriority = PRIORITY_ORDER[a.priority] ?? PRIORITY_ORDER.normal
      const bPriority = PRIORITY_ORDER[b.priority] ?? PRIORITY_ORDER.normal
      const priorityDiff = (aPriority - bPriority) * multiplier
      if (priorityDiff !== 0) return priorityDiff

      // Stable secondary sort inside a priority bucket: oldest first so default
      // priority ordering does not churn equal-priority items on each refresh.
      return new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime()
    }

    if (field === 'due_date') {
      const aDate = a.due_date ? new Date(a.due_date).getTime() : Infinity
      const bDate = b.due_date ? new Date(b.due_date).getTime() : Infinity
      return (aDate - bDate) * multiplier
    }

    if (field === 'title') {
      return String(a.title || '').localeCompare(String(b.title || '')) * multiplier
    }

    // created_at: desc=newest first, asc=oldest first.
    const aCreated = new Date(a.created_at || 0).getTime()
    const bCreated = new Date(b.created_at || 0).getTime()
    return (aCreated - bCreated) * multiplier
  }
}
