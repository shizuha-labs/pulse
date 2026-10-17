import { formatTeamName } from './teamLabels.js'

export function canonicalTeamName(teamName) {
  return String(teamName || '').trim().toLowerCase().replace(/_/g, '-').replace(/\s+/g, '-')
}

export function assigneeDisplayName(task) {
  if (!task?.assignee_email) return ''
  return task.assignee_name || task.assignee_email.split('@')[0]
}

export function assigneeRosterTeams(rosterData) {
  const memberships = Array.isArray(rosterData?.memberships) ? rosterData.memberships : []
  return memberships
    .map((membership) => membership?.team_name)
    .filter(Boolean)
}

export function formatAssigneeRosterTeams(rosterData) {
  return assigneeRosterTeams(rosterData).map(formatTeamName)
}

export function isAssigneeOutsideAssignmentGroup(task, rosterData) {
  const group = canonicalTeamName(task?.assignment_group)
  if (!group || !task?.assignee_email) return false
  const teams = assigneeRosterTeams(rosterData).map(canonicalTeamName)
  return teams.length > 0 && !teams.includes(group)
}
