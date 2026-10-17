import { useQuery } from '@tanstack/react-query'
import {
  Plus,
  Edit,
  UserPlus,
  UserMinus,
  CheckCircle,
  RotateCcw,
  MessageSquare,
  Paperclip,
  Link,
  Unlink,
  Eye,
  EyeOff,
  Flag,
  ArrowRight,
  FolderInput,
  GitPullRequest,
  GitMerge,
  Loader2,
} from 'lucide-react'
import { itemsApi } from '../services/api'
import { cn } from '../utils/cn'
import { formatDistanceToNow } from 'date-fns'

// Action type to icon and color mapping
const ACTION_CONFIG = {
  created: {
    icon: Plus,
    color: 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400',
    label: 'created this task',
  },
  updated: {
    icon: Edit,
    color: 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
    label: 'updated',
  },
  status_changed: {
    icon: ArrowRight,
    color: 'bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-400',
    label: 'changed status',
  },
  assigned: {
    icon: UserPlus,
    color: 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
    label: 'assigned',
  },
  unassigned: {
    icon: UserMinus,
    color: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
    label: 'unassigned',
  },
  priority_changed: {
    icon: Flag,
    color: 'bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400',
    label: 'changed priority',
  },
  moved: {
    icon: FolderInput,
    color: 'bg-indigo-100 text-indigo-600 dark:bg-indigo-900/30 dark:text-indigo-400',
    label: 'moved to project',
  },
  commented: {
    icon: MessageSquare,
    color: 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
    label: 'commented',
  },
  attachment_added: {
    icon: Paperclip,
    color: 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400',
    label: 'added attachment',
  },
  attachment_removed: {
    icon: Paperclip,
    color: 'bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400',
    label: 'removed attachment',
  },
  linked: {
    icon: Link,
    color: 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
    label: 'linked',
  },
  unlinked: {
    icon: Unlink,
    color: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
    label: 'unlinked',
  },
  watched: {
    icon: Eye,
    color: 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
    label: 'started watching',
  },
  unwatched: {
    icon: EyeOff,
    color: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
    label: 'stopped watching',
  },
  completed: {
    icon: CheckCircle,
    color: 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400',
    label: 'completed this task',
  },
  reopened: {
    icon: RotateCcw,
    color: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/30 dark:text-yellow-400',
    label: 'reopened this task',
  },
  pr_attached: {
    icon: GitPullRequest,
    color: 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/30 dark:text-emerald-400',
    label: 'attached a pull request',
  },
  pr_removed: {
    icon: GitPullRequest,
    color: 'bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400',
    label: 'removed a pull request',
  },
  pr_status_changed: {
    icon: GitMerge,
    color: 'bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-400',
    label: 'updated pull request status',
  },
}

function ActivityItem({ activity }) {
  const config = ACTION_CONFIG[activity.action] || {
    icon: Edit,
    color: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
    label: activity.action_display || activity.action,
  }
  const Icon = config.icon

  // Build description
  let description = config.label
  if (activity.field_name && activity.old_value && activity.new_value) {
    description = `changed ${activity.field_name} from "${activity.old_value}" to "${activity.new_value}"`
  } else if (activity.field_name && activity.new_value) {
    description = `${config.label} ${activity.field_name}: "${activity.new_value}"`
  } else if (activity.new_value) {
    description = `${config.label}: "${activity.new_value}"`
  }

  return (
    <div className="flex gap-3">
      {/* Icon */}
      <div className={cn(
        'w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0',
        config.color
      )}>
        <Icon size={14} />
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0 pt-1">
        <p className="text-sm text-gray-900 dark:text-gray-100">
          <span className="font-medium">
            {activity.user_name || activity.user_email?.split('@')[0] || 'Someone'}
          </span>{' '}
          <span className="text-gray-600 dark:text-gray-400">
            {description}
          </span>
        </p>
        <p className="text-xs text-gray-500 dark:text-gray-500 mt-0.5">
          {formatDistanceToNow(new Date(activity.created_at), { addSuffix: true })}
        </p>
      </div>
    </div>
  )
}

export default function ActivityTimeline({ itemId }) {
  const { data: activityData, isLoading, error } = useQuery({
    queryKey: ['task-activity', itemId],
    queryFn: async () => {
      const res = await itemsApi.getActivity(itemId)
      return res.data
    },
    enabled: !!itemId,
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="animate-spin text-gray-400" size={24} />
      </div>
    )
  }

  if (error) {
    return (
      <p className="text-sm text-red-500 text-center py-4">
        Failed to load activity
      </p>
    )
  }

  const activities = activityData?.activities || []

  if (activities.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
        No activity yet
      </p>
    )
  }

  return (
    <div className="space-y-4">
      {activities.map((activity, index) => (
        <div key={activity.id} className="relative">
          {/* Connecting line */}
          {index < activities.length - 1 && (
            <div className="absolute left-4 top-8 w-px h-full bg-gray-200 dark:bg-gray-700 -translate-x-1/2" />
          )}
          <ActivityItem activity={activity} />
        </div>
      ))}
    </div>
  )
}
