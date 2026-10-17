import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Users, CheckSquare, ChevronDown, ChevronRight, BarChart3, Clock, AlertCircle, Layers } from 'lucide-react'
import { tasksApi } from '../services/api'
import { useOrganization } from '../contexts/OrganizationContext'
import { useAuth } from '../contexts/AuthContext'
import ItemCard from '../components/ItemCard'
import EmptyState from '../components/EmptyState'
import { PageLoader } from '../components/LoadingSpinner'
import { cn } from '../utils/cn'
import { formatTeamName } from '../utils/teamLabels'
import { AgentAvatar, StatusBadge } from '../components/shared'

function ExpandedTasks({ queryKey, queryFn, onTaskClick }) {
  const { data, isLoading } = useQuery({ queryKey, queryFn })
  if (isLoading) {
    return <div className="border-t border-gray-200 dark:border-gray-700 p-4 text-sm text-gray-500">Loading tasks…</div>
  }
  const tasks = data?.tasks || []
  if (tasks.length === 0) {
    return <div className="border-t border-gray-200 dark:border-gray-700 p-4 text-sm text-gray-500">No open tasks.</div>
  }
  return (
    <div className="border-t border-gray-200 dark:border-gray-700 p-4 space-y-2">
      {tasks.map((task) => (
        <ItemCard
          key={task.id}
          item={task}
          compact
          onClick={() => onTaskClick(task)}
          navigateOnClick
        />
      ))}
      {data?.truncated && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Showing the {tasks.length} most recently updated of {data.task_count}.
        </p>
      )}
    </div>
  )
}

export default function TeamTasks() {
  const navigate = useNavigate()
  const { currentOrganization, projects } = useOrganization()
  const { user } = useAuth()
  const [selectedProject, setSelectedProject] = useState('')
  const [showCompleted, setShowCompleted] = useState(false)
  const [expandedMembers, setExpandedMembers] = useState({})
  const [viewMode, setViewMode] = useState('tasks') // 'tasks', 'teams', or 'workload'

  // Fetch team tasks grouped by assignee or team
  const groupBy = viewMode === 'teams' ? 'team' : 'assignee'
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['teamTasks', currentOrganization?.id, selectedProject, showCompleted, groupBy],
    queryFn: () =>
      tasksApi.teamTasks({
        organization: currentOrganization?.id,
        project: selectedProject || undefined,
        include_completed: showCompleted,
        group_by: groupBy,
      }).then((res) => res.data),
  })

  // Fetch user workload statistics
  const { data: workloadData, isLoading: workloadLoading } = useQuery({
    queryKey: ['userWorkload', currentOrganization?.id, selectedProject],
    queryFn: () =>
      tasksApi.userWorkload({
        organization: currentOrganization?.id,
        project: selectedProject || undefined,
      }).then((res) => res.data),
    enabled: viewMode === 'workload',
  })

  const teamMembers = data?.team_members || []
  const teams = data?.teams || []
  const workloadUsers = workloadData?.users || []

  const toggleMember = (key) => {
    setExpandedMembers((prev) => ({
      ...prev,
      [key]: !prev[key],
    }))
  }

  const isMemberExpanded = (key) => expandedMembers[key] === true

  const handleTaskClick = (task) => {
    const taskKey = task.item_key || task.id
    navigate(`/tasks/${taskKey}`)
  }

  if (isLoading) {
    return <PageLoader />
  }

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Team Tasks
          </h1>
          <p className="text-gray-500 dark:text-gray-400">
            View tasks assigned to your team members
          </p>
        </div>
      </div>

      {/* View Mode Toggle */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => setViewMode('tasks')}
          className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-lg border transition-colors',
            viewMode === 'tasks'
              ? 'bg-brand-100 dark:bg-brand-900 border-brand-300 dark:border-brand-700 text-brand-700 dark:text-brand-300'
              : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'
          )}
        >
          <Users size={18} />
          By Assignee
        </button>
        <button
          onClick={() => setViewMode('teams')}
          className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-lg border transition-colors',
            viewMode === 'teams'
              ? 'bg-brand-100 dark:bg-brand-900 border-brand-300 dark:border-brand-700 text-brand-700 dark:text-brand-300'
              : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'
          )}
        >
          <Layers size={18} />
          By Team
        </button>
        <button
          onClick={() => setViewMode('workload')}
          className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-lg border transition-colors',
            viewMode === 'workload'
              ? 'bg-brand-100 dark:bg-brand-900 border-brand-300 dark:border-brand-700 text-brand-700 dark:text-brand-300'
              : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'
          )}
        >
          <BarChart3 size={18} />
          Workload Stats
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        {/* Project Filter */}
        <select
          value={selectedProject}
          onChange={(e) => setSelectedProject(e.target.value)}
          className="input w-auto"
        >
          <option value="">All Projects</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.key} - {p.name}
            </option>
          ))}
        </select>

        {/* Show Completed Toggle (in tasks and teams views) */}
        {(viewMode === 'tasks' || viewMode === 'teams') && (
          <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              checked={showCompleted}
              onChange={(e) => setShowCompleted(e.target.checked)}
              className="rounded border-gray-300 text-brand-600 focus:ring-brand-500"
            />
            Show completed
          </label>
        )}

        {/* Task count */}
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {viewMode === 'teams' ? (
            <>
              {teams.length} team{teams.length !== 1 ? 's' : ''} •{' '}
              {teams.reduce((acc, t) => acc + t.task_count, 0)} total tasks
            </>
          ) : (
            <>
              {teamMembers.length} team member{teamMembers.length !== 1 ? 's' : ''} •{' '}
              {teamMembers.reduce((acc, m) => acc + m.task_count, 0)} total tasks
            </>
          )}
        </span>
      </div>

      {/* Tasks View */}
      {viewMode === 'tasks' && (
        <>
          {teamMembers.length > 0 ? (
            <div className="space-y-4">
              {teamMembers.map((member) => {
                const memberKey = member.assignee_id || member.assignee_email
                const isExpanded = isMemberExpanded(memberKey)
                const isCurrentUser = member.assignee_id === user?.id

                return (
                  <div
                    key={memberKey}
                    className="card overflow-hidden"
                  >
                    {/* Member Header */}
                    <button
                      onClick={() => toggleMember(memberKey)}
                      className="w-full flex items-center justify-between p-4 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                    >
                      <div className="flex items-center gap-3">
                        <AgentAvatar
                          user_id={member.assignee_id}
                          display_name={member.assignee_name || member.assignee_email}
                          size="lg"
                        />
                        <div className="text-left">
                          <h3 className="font-medium text-gray-900 dark:text-gray-100 flex items-center gap-1.5">
                            {member.assignee_name || member.assignee_email?.split('@')[0] || member.assignee_email}
                            {isCurrentUser && (
                              <span className="text-xs text-brand-600 dark:text-brand-400">(You)</span>
                            )}
                            <StatusBadge status={member.availability_status ?? 'unknown'} size="sm" />
                          </h3>
                          <p className="text-sm text-gray-500 dark:text-gray-400">
                            {member.task_count} task{member.task_count !== 1 ? 's' : ''}
                          </p>
                        </div>
                      </div>
                      {isExpanded ? (
                        <ChevronDown size={20} className="text-gray-400" />
                      ) : (
                        <ChevronRight size={20} className="text-gray-400" />
                      )}
                    </button>

                    {/* Task List */}
                    {isExpanded && (
                      <ExpandedTasks
                        queryKey={['teamMemberTasks', currentOrganization?.id, selectedProject, showCompleted, member.assignee_email]}
                        queryFn={() =>
                          tasksApi.teamTasks({
                            organization: currentOrganization?.id,
                            project: selectedProject || undefined,
                            include_completed: showCompleted,
                            group_by: 'assignee',
                            assignee_email: member.assignee_email,
                          }).then((res) => res.data)
                        }
                        onTaskClick={handleTaskClick}
                      />
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <EmptyState
              icon={Users}
              title="No team tasks"
              description={
                selectedProject
                  ? 'No tasks found for team members in the selected project'
                  : 'No tasks found for team members in your accessible projects'
              }
            />
          )}
        </>
      )}

      {/* Teams View */}
      {viewMode === 'teams' && (
        <>
          {teams.length > 0 ? (
            <div className="space-y-4">
              {teams.map((team) => {
                const teamKey = team.team_name
                const isExpanded = isMemberExpanded(teamKey)
                const isUnassigned = team.team_name === 'Unassigned'

                return (
                  <div
                    key={teamKey}
                    className="card overflow-hidden"
                  >
                    {/* Team Header */}
                    <button
                      onClick={() => toggleMember(teamKey)}
                      className="w-full flex items-center justify-between p-4 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                    >
                      <div className="flex items-center gap-3">
                        <div
                          className={cn(
                            'w-10 h-10 rounded-full flex items-center justify-center',
                            isUnassigned
                              ? 'bg-gray-100 dark:bg-gray-800'
                              : 'bg-brand-100 dark:bg-brand-900'
                          )}
                        >
                          <Layers
                            size={20}
                            className={cn(
                              isUnassigned
                                ? 'text-gray-600 dark:text-gray-400'
                                : 'text-brand-600 dark:text-brand-400'
                            )}
                          />
                        </div>
                        <div className="text-left">
                          <h3 className="font-medium text-gray-900 dark:text-gray-100">
                            {formatTeamName(team.team_name)}
                          </h3>
                          <p className="text-sm text-gray-500 dark:text-gray-400">
                            {team.task_count} task{team.task_count !== 1 ? 's' : ''}
                          </p>
                        </div>
                      </div>
                      {isExpanded ? (
                        <ChevronDown size={20} className="text-gray-400" />
                      ) : (
                        <ChevronRight size={20} className="text-gray-400" />
                      )}
                    </button>

                    {/* Task List */}
                    {isExpanded && (
                      <ExpandedTasks
                        queryKey={['teamGroupTasks', currentOrganization?.id, selectedProject, showCompleted, team.team_name]}
                        queryFn={() =>
                          tasksApi.teamTasks({
                            organization: currentOrganization?.id,
                            project: selectedProject || undefined,
                            include_completed: showCompleted,
                            group_by: 'team',
                            team: team.team_name,
                          }).then((res) => res.data)
                        }
                        onTaskClick={handleTaskClick}
                      />
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <EmptyState
              icon={Layers}
              title="No team assignments"
              description={
                selectedProject
                  ? 'No tasks with team assignments found in the selected project'
                  : 'No tasks with team assignments found in your accessible projects'
              }
            />
          )}
        </>
      )}

      {/* Workload Stats View */}
      {viewMode === 'workload' && (
        <>
          {workloadLoading ? (
            <PageLoader />
          ) : workloadUsers.length > 0 ? (
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {workloadUsers.map((workloadUser) => {
                const isCurrentUser = workloadUser.user_id === user?.id
                const completionRate = workloadUser.completion_rate || 0

                return (
                  <div
                    key={workloadUser.user_id || workloadUser.email}
                    className="card p-4"
                  >
                    {/* User Header */}
                    <div className="flex items-center gap-3 mb-4">
                      <AgentAvatar
                        user_id={workloadUser.user_id}
                        display_name={workloadUser.display_name || workloadUser.email}
                        size="lg"
                      />
                      <div>
                        <h3 className="font-medium text-gray-900 dark:text-gray-100 flex items-center gap-1.5">
                          {workloadUser.display_name || workloadUser.email}
                          {isCurrentUser && (
                            <span className="text-xs text-brand-600 dark:text-brand-400">(You)</span>
                          )}
                          <StatusBadge status={workloadUser.availability_status ?? 'unknown'} size="sm" />
                        </h3>
                        <p className="text-sm text-gray-500 dark:text-gray-400">
                          {workloadUser.assigned?.total ?? workloadUser.total ?? 0} assigned tasks
                        </p>
                      </div>
                    </div>

                    {/* Stats Grid - handle both flat and nested structure */}
                    <div className="grid grid-cols-2 gap-3">
                      <div className="bg-yellow-50 dark:bg-yellow-900/20 rounded-lg p-3">
                        <div className="flex items-center gap-2 text-yellow-600 dark:text-yellow-400 mb-1">
                          <Clock size={16} />
                          <span className="text-xs font-medium">Pending</span>
                        </div>
                        <p className="text-2xl font-bold text-yellow-700 dark:text-yellow-300">
                          {workloadUser.assigned?.pending ?? workloadUser.pending ?? 0}
                        </p>
                      </div>

                      <div className="bg-blue-50 dark:bg-blue-900/20 rounded-lg p-3">
                        <div className="flex items-center gap-2 text-blue-600 dark:text-blue-400 mb-1">
                          <BarChart3 size={16} />
                          <span className="text-xs font-medium">In Progress</span>
                        </div>
                        <p className="text-2xl font-bold text-blue-700 dark:text-blue-300">
                          {workloadUser.assigned?.in_progress ?? workloadUser.in_progress ?? 0}
                        </p>
                      </div>

                      <div className="bg-green-50 dark:bg-green-900/20 rounded-lg p-3">
                        <div className="flex items-center gap-2 text-green-600 dark:text-green-400 mb-1">
                          <CheckSquare size={16} />
                          <span className="text-xs font-medium">Completed</span>
                        </div>
                        <p className="text-2xl font-bold text-green-700 dark:text-green-300">
                          {workloadUser.assigned?.completed ?? workloadUser.completed ?? 0}
                        </p>
                      </div>

                      <div className="bg-red-50 dark:bg-red-900/20 rounded-lg p-3">
                        <div className="flex items-center gap-2 text-red-600 dark:text-red-400 mb-1">
                          <AlertCircle size={16} />
                          <span className="text-xs font-medium">Overdue</span>
                        </div>
                        <p className="text-2xl font-bold text-red-700 dark:text-red-300">
                          {workloadUser.assigned?.overdue ?? workloadUser.overdue ?? 0}
                        </p>
                      </div>
                    </div>

                    {/* Completion Rate */}
                    <div className="mt-4 pt-3 border-t border-gray-200 dark:border-gray-700">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-sm text-gray-600 dark:text-gray-400">
                          Completion Rate
                        </span>
                        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                          {completionRate.toFixed(0)}%
                        </span>
                      </div>
                      <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                        <div
                          className={cn(
                            'h-2 rounded-full transition-all',
                            completionRate >= 80
                              ? 'bg-green-500'
                              : completionRate >= 50
                              ? 'bg-yellow-500'
                              : 'bg-red-500'
                          )}
                          style={{ width: `${Math.min(completionRate, 100)}%` }}
                        />
                      </div>
                    </div>

                    {/* Created Tasks */}
                    {workloadUser.created > 0 && (
                      <div className="mt-3 text-xs text-gray-500 dark:text-gray-400">
                        Created {workloadUser.created} task{workloadUser.created !== 1 ? 's' : ''}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <EmptyState
              icon={BarChart3}
              title="No workload data"
              description={
                selectedProject
                  ? 'No workload statistics found for the selected project'
                  : 'No workload statistics found for your accessible projects'
              }
            />
          )}
        </>
      )}

    </div>
  )
}
