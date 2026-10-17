import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  Plus,
  Trash2,
  TestTube2,
  Power,
  PowerOff,
  Edit2,
  Check,
  X,
  MessageCircle,
  Hash,
} from 'lucide-react'
import { notificationChannelsApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { cn } from '../utils/cn'

const CHANNEL_TYPES = [
  { value: 'discord', label: 'Discord', icon: Hash, color: 'text-indigo-500' },
  { value: 'telegram', label: 'Telegram', icon: MessageCircle, color: 'text-blue-500' },
]

const SEVERITY_OPTIONS = [
  { value: 'info', label: 'Info (All)' },
  { value: 'warning', label: 'Warning+' },
  { value: 'error', label: 'Error+' },
  { value: 'critical', label: 'Critical Only' },
]

export default function NotificationChannels() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [editingChannel, setEditingChannel] = useState(null)
  const [testingChannel, setTestingChannel] = useState(null)
  const [testResult, setTestResult] = useState(null)

  // Fetch channels for current user (backend filters by authenticated user)
  const { data, isLoading, error } = useQuery({
    queryKey: ['notificationChannels', user?.id],
    queryFn: () => notificationChannelsApi.list().then((res) => res.data),
    enabled: !!user,
  })

  // Create mutation
  const createMutation = useMutation({
    mutationFn: (data) => notificationChannelsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationChannels'] })
      setShowCreateModal(false)
    },
  })

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, data }) => notificationChannelsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationChannels'] })
      setEditingChannel(null)
    },
  })

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (id) => notificationChannelsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationChannels'] })
    },
  })

  // Toggle mutation
  const toggleMutation = useMutation({
    mutationFn: (id) => notificationChannelsApi.toggle(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notificationChannels'] })
    },
  })

  // Test mutation
  const testMutation = useMutation({
    mutationFn: (id) => notificationChannelsApi.test(id),
    onSuccess: (response) => {
      setTestResult({ success: true, message: response.data.message })
      setTimeout(() => setTestResult(null), 3000)
    },
    onError: (error) => {
      setTestResult({
        success: false,
        message: error.response?.data?.error || 'Test failed',
      })
      setTimeout(() => setTestResult(null), 5000)
    },
    onSettled: () => {
      setTestingChannel(null)
    },
  })

  const handleTest = (channelId) => {
    setTestingChannel(channelId)
    setTestResult(null)
    testMutation.mutate(channelId)
  }

  // Handle both paginated and non-paginated responses
  const channels = data?.results || data || []

  if (isLoading) {
    return (
      <div className="animate-pulse space-y-3">
        <div className="h-16 bg-gray-200 dark:bg-gray-700 rounded-lg" />
        <div className="h-16 bg-gray-200 dark:bg-gray-700 rounded-lg" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-red-500 dark:text-red-400 text-sm">
        Error loading notification channels
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header with add button */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="font-medium text-gray-900 dark:text-gray-100">
            Your Notification Channels
          </h3>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Get notified via Discord or Telegram
          </p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="btn-primary flex items-center gap-2"
        >
          <Plus size={16} />
          Add Channel
        </button>
      </div>

      {/* Test result toast */}
      {testResult && (
        <div
          className={cn(
            'p-3 rounded-lg flex items-center gap-2 animate-fade-in',
            testResult.success
              ? 'bg-success-100 text-success-800 dark:bg-success-900/30 dark:text-success-300'
              : 'bg-critical-100 text-critical-800 dark:bg-critical-900/30 dark:text-critical-300'
          )}
        >
          {testResult.success ? <Check size={18} /> : <X size={18} />}
          {testResult.message}
        </div>
      )}

      {/* Channels list */}
      {channels.length > 0 ? (
        <div className="space-y-3">
          {channels.map((channel) => (
            <ChannelCard
              key={channel.id}
              channel={channel}
              onEdit={() => setEditingChannel(channel)}
              onDelete={() => deleteMutation.mutate(channel.id)}
              onToggle={() => toggleMutation.mutate(channel.id)}
              onTest={() => handleTest(channel.id)}
              isTesting={testingChannel === channel.id}
              isDeleting={deleteMutation.isPending}
            />
          ))}
        </div>
      ) : (
        <div className="text-center py-8 text-gray-500 dark:text-gray-400">
          <Bell size={32} className="mx-auto mb-2 opacity-50" />
          <p>No notification channels configured</p>
          <p className="text-sm mt-1">
            Add Discord or Telegram to receive notifications
          </p>
        </div>
      )}

      {/* Create Modal */}
      {showCreateModal && (
        <ChannelModal
          onClose={() => setShowCreateModal(false)}
          onSave={(data) => createMutation.mutate(data)}
          isSaving={createMutation.isPending}
        />
      )}

      {/* Edit Modal */}
      {editingChannel && (
        <ChannelModal
          channel={editingChannel}
          onClose={() => setEditingChannel(null)}
          onSave={(data) =>
            updateMutation.mutate({ id: editingChannel.id, data })
          }
          isSaving={updateMutation.isPending}
        />
      )}
    </div>
  )
}

function ChannelCard({
  channel,
  onEdit,
  onDelete,
  onToggle,
  onTest,
  isTesting,
  isDeleting,
}) {
  const channelType = CHANNEL_TYPES.find((t) => t.value === channel.channel_type)
  const Icon = channelType?.icon || Bell

  return (
    <div
      className={cn(
        'p-4 rounded-lg border transition-all',
        channel.is_active
          ? 'border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900'
          : 'border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900/50 opacity-60'
      )}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-3 flex-1">
          <div
            className={cn(
              'p-2 rounded-lg',
              channel.channel_type === 'discord'
                ? 'bg-indigo-100 dark:bg-indigo-900/30'
                : 'bg-blue-100 dark:bg-blue-900/30'
            )}
          >
            <Icon size={20} className={channelType?.color} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <h4 className="font-medium text-gray-900 dark:text-gray-100">
                {channel.name}
              </h4>
              <span
                className={cn(
                  'badge',
                  channel.is_active ? 'badge-success' : 'badge-gray'
                )}
              >
                {channel.is_active ? 'Active' : 'Inactive'}
              </span>
            </div>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
              {channelType?.label}
              {channel.channel_type === 'telegram' &&
                channel.telegram_chat_id &&
                ` • Chat: ${channel.telegram_chat_id}`}
            </p>
            {/* Notification preferences */}
            <div className="flex flex-wrap gap-2 mt-2">
              {channel.notify_on_reminder && (
                <span className="text-xs px-2 py-0.5 bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-300 rounded">
                  Reminders
                </span>
              )}
              {channel.notify_on_overdue && (
                <span className="text-xs px-2 py-0.5 bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300 rounded">
                  Overdue
                </span>
              )}
              {channel.notify_on_alert && (
                <span className="text-xs px-2 py-0.5 bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300 rounded">
                  Alerts
                </span>
              )}
              {channel.notify_on_action && (
                <span className="text-xs px-2 py-0.5 bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300 rounded">
                  Actions
                </span>
              )}
            </div>
            {channel.last_error && (
              <p className="text-xs text-critical-600 dark:text-critical-400 mt-1">
                Last error: {channel.last_error}
              </p>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1">
          <button
            onClick={onTest}
            disabled={isTesting || !channel.is_active}
            className="btn-icon"
            title="Send test notification"
          >
            {isTesting ? (
              <span className="animate-spin">
                <TestTube2 size={16} />
              </span>
            ) : (
              <TestTube2 size={16} className="text-gray-500" />
            )}
          </button>
          <button
            onClick={onToggle}
            className="btn-icon"
            title={channel.is_active ? 'Disable' : 'Enable'}
          >
            {channel.is_active ? (
              <Power size={16} className="text-success-500" />
            ) : (
              <PowerOff size={16} className="text-gray-400" />
            )}
          </button>
          <button onClick={onEdit} className="btn-icon" title="Edit">
            <Edit2 size={16} className="text-gray-500" />
          </button>
          <button
            onClick={onDelete}
            disabled={isDeleting}
            className="btn-icon"
            title="Delete"
          >
            <Trash2 size={16} className="text-critical-500" />
          </button>
        </div>
      </div>
    </div>
  )
}

function ChannelModal({ channel, onClose, onSave, isSaving }) {
  const isEdit = !!channel
  // Note: user_id is set automatically by the backend from the authenticated user
  const [formData, setFormData] = useState({
    name: channel?.name || '',
    channel_type: channel?.channel_type || 'discord',
    is_active: channel?.is_active ?? true,
    discord_webhook_url: channel?.discord_webhook_url || '',
    telegram_bot_token: channel?.telegram_bot_token || '',
    telegram_chat_id: channel?.telegram_chat_id || '',
    notify_on_reminder: channel?.notify_on_reminder ?? true,
    notify_on_overdue: channel?.notify_on_overdue ?? true,
    notify_on_alert: channel?.notify_on_alert ?? true,
    notify_on_action: channel?.notify_on_action ?? true,
    min_alert_severity: channel?.min_alert_severity || 'warning',
    quiet_hours_start: channel?.quiet_hours_start || '',
    quiet_hours_end: channel?.quiet_hours_end || '',
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    // Clean up data based on channel type
    const data = { ...formData }
    if (data.channel_type === 'discord') {
      delete data.telegram_bot_token
      delete data.telegram_chat_id
    } else {
      delete data.discord_webhook_url
    }
    // Remove empty quiet hours
    if (!data.quiet_hours_start) delete data.quiet_hours_start
    if (!data.quiet_hours_end) delete data.quiet_hours_end

    onSave(data)
  }

  const currentType = CHANNEL_TYPES.find((t) => t.value === formData.channel_type)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
            {isEdit ? 'Edit Notification Channel' : 'Add Notification Channel'}
          </h2>
          <button onClick={onClose} className="btn-icon">
            <X size={20} />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="flex-1 overflow-y-auto p-4">
          <div className="space-y-4">
            {/* Channel Type */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Channel Type
              </label>
              <div className="flex gap-3">
                {CHANNEL_TYPES.map((type) => {
                  const Icon = type.icon
                  return (
                    <button
                      key={type.value}
                      type="button"
                      onClick={() =>
                        setFormData({ ...formData, channel_type: type.value })
                      }
                      className={cn(
                        'flex-1 flex items-center justify-center gap-2 p-3 rounded-lg border-2 transition-all',
                        formData.channel_type === type.value
                          ? 'border-brand-500 bg-brand-50 dark:bg-brand-950'
                          : 'border-gray-200 dark:border-gray-700 hover:border-gray-300'
                      )}
                    >
                      <Icon size={20} className={type.color} />
                      <span className="font-medium text-gray-900 dark:text-gray-100">
                        {type.label}
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>

            {/* Name */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Channel Name *
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) =>
                  setFormData({ ...formData, name: e.target.value })
                }
                className="input"
                placeholder="e.g., My Discord Server"
                required
              />
            </div>

            {/* Discord Config */}
            {formData.channel_type === 'discord' && (
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Webhook URL *
                </label>
                <input
                  type="url"
                  value={formData.discord_webhook_url}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      discord_webhook_url: e.target.value,
                    })
                  }
                  className="input"
                  placeholder="https://discord.com/api/webhooks/..."
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Create a webhook in your Discord server settings
                </p>
              </div>
            )}

            {/* Telegram Config */}
            {formData.channel_type === 'telegram' && (
              <>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Bot Token *
                  </label>
                  <input
                    type="password"
                    value={formData.telegram_bot_token}
                    onChange={(e) =>
                      setFormData({
                        ...formData,
                        telegram_bot_token: e.target.value,
                      })
                    }
                    className="input"
                    placeholder="123456:ABC-DEF..."
                    required
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Get this from @BotFather on Telegram
                  </p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Chat ID *
                  </label>
                  <input
                    type="text"
                    value={formData.telegram_chat_id}
                    onChange={(e) =>
                      setFormData({
                        ...formData,
                        telegram_chat_id: e.target.value,
                      })
                    }
                    className="input"
                    placeholder="123456789"
                    required
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Your Telegram user ID or group chat ID
                  </p>
                </div>
              </>
            )}

            {/* Notification Preferences */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Notify Me For
              </label>
              <div className="grid grid-cols-2 gap-2">
                {[
                  { key: 'notify_on_reminder', label: 'Task Reminders' },
                  { key: 'notify_on_overdue', label: 'Overdue Tasks' },
                  { key: 'notify_on_alert', label: 'System Alerts' },
                  { key: 'notify_on_action', label: 'Pending Actions' },
                ].map(({ key, label }) => (
                  <label
                    key={key}
                    className="flex items-center gap-2 p-2 rounded-lg border border-gray-200 dark:border-gray-700 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                  >
                    <input
                      type="checkbox"
                      checked={formData[key]}
                      onChange={(e) =>
                        setFormData({ ...formData, [key]: e.target.checked })
                      }
                      className="rounded border-gray-300 text-brand-600 focus:ring-brand-500"
                    />
                    <span className="text-sm text-gray-700 dark:text-gray-300">
                      {label}
                    </span>
                  </label>
                ))}
              </div>
            </div>

            {/* Alert Severity */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Minimum Alert Severity
              </label>
              <select
                value={formData.min_alert_severity}
                onChange={(e) =>
                  setFormData({
                    ...formData,
                    min_alert_severity: e.target.value,
                  })
                }
                className="input"
              >
                {SEVERITY_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Quiet Hours */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                Quiet Hours (Optional)
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="time"
                  value={formData.quiet_hours_start}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      quiet_hours_start: e.target.value,
                    })
                  }
                  className="input flex-1"
                />
                <span className="text-gray-500">to</span>
                <input
                  type="time"
                  value={formData.quiet_hours_end}
                  onChange={(e) =>
                    setFormData({
                      ...formData,
                      quiet_hours_end: e.target.value,
                    })
                  }
                  className="input flex-1"
                />
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                No notifications will be sent during these hours
              </p>
            </div>
          </div>
        </form>

        {/* Footer */}
        <div className="p-4 border-t border-gray-200 dark:border-gray-700 flex gap-3">
          <button type="button" onClick={onClose} className="btn-secondary flex-1">
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={
              isSaving ||
              !formData.name ||
              (formData.channel_type === 'discord' &&
                !formData.discord_webhook_url) ||
              (formData.channel_type === 'telegram' &&
                (!formData.telegram_bot_token || !formData.telegram_chat_id))
            }
            className="btn-primary flex-1"
          >
            {isSaving ? 'Saving...' : isEdit ? 'Save Changes' : 'Add Channel'}
          </button>
        </div>
      </div>
    </div>
  )
}
