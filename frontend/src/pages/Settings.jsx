import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Settings as SettingsIcon, Bell, Database, Link, ExternalLink, Palette, Sun, Moon, Monitor, MessageSquare, Download, ChevronRight } from 'lucide-react'
import { subscriptionsApi, fingerprintConfigsApi } from '../services/api'
import { PageLoader } from '../components/LoadingSpinner'
import EmptyState from '../components/EmptyState'
import { useTheme } from '../hooks/useTheme'
import { cn } from '../utils/cn'
import NotificationChannels from '../components/NotificationChannels'

export default function Settings() {
  const { theme, setTheme, isDark } = useTheme()
  const navigate = useNavigate()

  // Fetch subscriptions
  const { data: subscriptions, isLoading: subsLoading } = useQuery({
    queryKey: ['subscriptions'],
    queryFn: () => subscriptionsApi.list().then((res) => res.data),
  })

  // Fetch fingerprint configs
  const { data: fpConfigs, isLoading: fpLoading } = useQuery({
    queryKey: ['fingerprintConfigs'],
    queryFn: () => fingerprintConfigsApi.list().then((res) => res.data),
  })

  if (subsLoading || fpLoading) {
    return <PageLoader />
  }

  const subs = subscriptions?.results || subscriptions || []
  const configs = fpConfigs?.results || fpConfigs || []

  return (
    <div className="space-y-6 pb-20 lg:pb-0">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Settings</h1>
        <p className="text-gray-500 dark:text-gray-400">Configure notifications and integrations</p>
      </div>

      {/* Appearance */}
      <section className="card p-4">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
          <Palette size={18} />
          Appearance
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Choose your preferred color theme
        </p>
        <div className="flex gap-3">
          <button
            onClick={() => setTheme('light')}
            className={cn(
              'flex items-center gap-2 px-4 py-3 rounded-lg border-2 transition-all',
              theme === 'light'
                ? 'border-brand-500 bg-brand-50 dark:bg-brand-950'
                : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
            )}
          >
            <Sun size={20} className={theme === 'light' ? 'text-brand-600' : 'text-gray-500'} />
            <span className={cn(
              'font-medium',
              theme === 'light' ? 'text-brand-700 dark:text-brand-300' : 'text-gray-700 dark:text-gray-300'
            )}>Light</span>
          </button>
          <button
            onClick={() => setTheme('dark')}
            className={cn(
              'flex items-center gap-2 px-4 py-3 rounded-lg border-2 transition-all',
              theme === 'dark'
                ? 'border-brand-500 bg-brand-50 dark:bg-brand-950'
                : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
            )}
          >
            <Moon size={20} className={theme === 'dark' ? 'text-brand-400' : 'text-gray-500'} />
            <span className={cn(
              'font-medium',
              theme === 'dark' ? 'text-brand-700 dark:text-brand-300' : 'text-gray-700 dark:text-gray-300'
            )}>Dark</span>
          </button>
        </div>
      </section>

      {/* User Notification Channels */}
      <section className="card p-4">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
          <MessageSquare size={18} />
          Notification Channels
        </h2>
        <NotificationChannels />
      </section>

      {/* Integrations */}
      <section className="card p-4">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
          <Download size={18} />
          Integrations
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Import data from external tools or connect third-party services
        </p>
        <div className="space-y-3">
          <button
            onClick={() => navigate('/admin/jira-import')}
            className="w-full flex items-center justify-between p-4 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-left group"
          >
            <div className="flex items-center gap-4">
              <div className="w-10 h-10 rounded-lg bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" className="text-blue-600 dark:text-blue-400">
                  <path d="M12.005 2C6.478 2 2 6.478 2 12.005s4.478 10.005 10.005 10.005S22.01 17.532 22.01 12.005 17.532 2 12.005 2zm-.224 3.12h.448c.38 0 .687.308.687.687v4.93l3.39 3.39a.687.687 0 010 .972l-.316.316a.687.687 0 01-.972 0l-3.55-3.55a.687.687 0 01-.201-.486V5.807c0-.38.308-.687.514-.687z" fill="currentColor"/>
                </svg>
              </div>
              <div>
                <p className="font-medium text-gray-900 dark:text-gray-100">Import from Atlassian Jira</p>
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  Import projects, issues, workflows, and configurations from Jira Cloud or Server
                </p>
              </div>
            </div>
            <ChevronRight size={20} className="text-gray-400 group-hover:text-brand-500 transition-colors flex-shrink-0" />
          </button>
        </div>
      </section>

      {/* Quick Links */}
      <section className="card p-4">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
          <Link size={18} />
          Quick Links
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <a
            href="/api/docs/"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-gray-900 dark:text-gray-100"
          >
            <span>API Documentation (Swagger)</span>
            <ExternalLink size={16} className="text-gray-400" />
          </a>
          <a
            href="/api/redoc/"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-gray-900 dark:text-gray-100"
          >
            <span>API Documentation (ReDoc)</span>
            <ExternalLink size={16} className="text-gray-400" />
          </a>
          <a
            href="/admin/"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-gray-900 dark:text-gray-100"
          >
            <span>Django Admin</span>
            <ExternalLink size={16} className="text-gray-400" />
          </a>
          <a
            href="/api/schema/"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-gray-900 dark:text-gray-100"
          >
            <span>OpenAPI Schema (YAML)</span>
            <ExternalLink size={16} className="text-gray-400" />
          </a>
        </div>
      </section>

      {/* Subscriptions */}
      <section className="card p-4">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
          <Bell size={18} />
          Notification Subscriptions
        </h2>
        {subs.length > 0 ? (
          <div className="space-y-3">
            {subs.map((sub) => (
              <div
                key={sub.id}
                className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-700"
              >
                <div>
                  <p className="font-medium text-gray-900 dark:text-gray-100">{sub.name}</p>
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    Type: {sub.notify_type}
                    {sub.webhook_url && ` • ${sub.webhook_url}`}
                    {sub.redis_channel && ` • Channel: ${sub.redis_channel}`}
                  </p>
                </div>
                <span
                  className={`badge ${
                    sub.is_active ? 'badge-success' : 'badge-gray'
                  }`}
                >
                  {sub.is_active ? 'Active' : 'Inactive'}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-gray-500 dark:text-gray-400 text-sm">No subscriptions configured</p>
        )}
      </section>

      {/* Fingerprint Configs */}
      <section className="card p-4">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
          <Database size={18} />
          Fingerprint Configurations
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Configure how alerts are deduplicated per source
        </p>
        {configs.length > 0 ? (
          <div className="space-y-3">
            {configs.map((config) => (
              <div
                key={config.source}
                className="p-3 rounded-lg border border-gray-200 dark:border-gray-700"
              >
                <p className="font-medium text-gray-900 dark:text-gray-100">{config.source}</p>
                <div className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                  {config.label_fields?.length > 0 && (
                    <p>Labels: {config.label_fields.join(', ')}</p>
                  )}
                  {config.payload_fields?.length > 0 && (
                    <p>Payload: {config.payload_fields.join(', ')}</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-gray-500 dark:text-gray-400 text-sm">
            No fingerprint configurations. Using default (all labels).
          </p>
        )}
      </section>

      {/* System Info */}
      <section className="card p-4">
        <h2 className="font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center gap-2">
          <SettingsIcon size={18} />
          System Information
        </h2>
        <div className="space-y-2 text-sm">
          <div className="flex justify-between py-2 border-b border-gray-100 dark:border-gray-800">
            <span className="text-gray-500 dark:text-gray-400">Version</span>
            <span className="text-gray-900 dark:text-gray-100">1.0.0</span>
          </div>
          <div className="flex justify-between py-2 border-b border-gray-100 dark:border-gray-800">
            <span className="text-gray-500 dark:text-gray-400">API Base URL</span>
            <span className="text-gray-900 dark:text-gray-100">/api</span>
          </div>
          <div className="flex justify-between py-2">
            <span className="text-gray-500 dark:text-gray-400">Documentation Port</span>
            <span className="text-gray-900 dark:text-gray-100">8011</span>
          </div>
        </div>
      </section>
    </div>
  )
}
