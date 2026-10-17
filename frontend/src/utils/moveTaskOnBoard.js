// Board columns are categories. Workflow tasks reject a direct status PATCH
// ("Status changes must go through workflow transitions"), and the boards used
// to swallow that 400, so the card never left its column.

const COLUMN_STATUS = {
  todo: ['todo', 'pending', 'open'],
  pending: ['todo', 'pending', 'open'],
  open: ['todo', 'pending', 'open'],
  backlog: ['todo', 'pending', 'open', 'backlog'],
  in_progress: ['in_progress'],
  done: ['done', 'completed'],
  completed: ['done', 'completed'],
  scheduled: ['scheduled'],
}

export const BOARD_MOVE_NEEDS_TASK_PAGE =
  'Open the task to move it. This step asks for information a drag cannot send.'

export function columnStatusSlugs(columnId) {
  const key = String(columnId || '').trim().toLowerCase()
  return COLUMN_STATUS[key] ? [...COLUMN_STATUS[key]] : (key ? [key] : [])
}

function categoryForColumn(columnId) {
  const key = String(columnId || '').trim().toLowerCase()
  if (key === 'pending' || key === 'open' || key === 'backlog' || key === 'todo') return 'todo'
  if (key === 'completed' || key === 'done') return 'done'
  return key
}

export function taskAlreadyInColumn(item, columnId) {
  if (!item) return false
  const status = String(item.status || '').trim().toLowerCase()
  const category = String(item.status_category || '').trim().toLowerCase()
  if (status && columnStatusSlugs(columnId).includes(status)) return true
  const columnCategory = categoryForColumn(columnId)
  return Boolean(category && columnCategory && category === columnCategory)
}

function transitionList(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.transitions)) return payload.transitions
  if (Array.isArray(payload?.data?.transitions)) return payload.data.transitions
  return []
}

function screenBlocksDrag(transition) {
  const fields = transition?.screen_fields || []
  return fields.some((field) => field && field.required)
}

export function pickBoardTransition(payload, columnId) {
  const wanted = columnStatusSlugs(columnId)
  const matches = transitionList(payload).filter((transition) => {
    const toStatus = String(transition?.to_status || '').trim().toLowerCase()
    return toStatus && wanted.includes(toStatus) && !screenBlocksDrag(transition)
  })
  for (const slug of wanted) {
    const found = matches.find(
      (transition) => String(transition.to_status).trim().toLowerCase() === slug,
    )
    if (found) return found
  }
  return null
}

export function patchStatusForColumn(columnId) {
  const key = String(columnId || '').trim().toLowerCase()
  if (key === 'todo') return 'pending'
  if (key === 'done') return 'completed'
  return columnStatusSlugs(columnId)[0] || key
}

export async function moveTaskToColumn(item, columnId, api) {
  if (!item?.id) {
    throw new Error('No task to move')
  }
  if (taskAlreadyInColumn(item, columnId)) {
    return { moved: false, reason: 'same-column' }
  }
  if (item.workflow_name) {
    const response = await api.getAvailableTransitions(item.id)
    const picked = pickBoardTransition(response?.data ?? response, columnId)
    if (!picked) {
      const error = new Error(BOARD_MOVE_NEEDS_TASK_PAGE)
      error.code = 'board-transition-unavailable'
      throw error
    }
    await api.executeTransition(item.id, {
      to_status: picked.to_status,
      transition_id: picked.id,
    })
    return { moved: true, via: 'transition', status: picked.to_status }
  }
  const status = patchStatusForColumn(columnId)
  await api.update(item.id, { status })
  return { moved: true, via: 'patch', status }
}
