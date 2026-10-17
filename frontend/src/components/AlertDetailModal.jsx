import { useState } from 'react'
import { X, AlertTriangle, Clock, ExternalLink, Hash, Tag, Server, Activity, CheckCircle, Bell, BellOff, Trash2 } from 'lucide-react'
import { cn } from '../utils/cn'
import { formatSmartDate, formatRelativeTime, getSeverityClasses, getStatusClasses } from '../utils/format'

/**
 * Modal component for displaying detailed alert information
 */
export default function AlertDetailModal({
  alert,
  onClose,
  onAcknowledge,
  onResolve,
  onSilence,
  onDelete,
}) {
  if (!alert) return null

  const severityClasses = getSeverityClasses(alert.severity)
  const statusClasses = getStatusClasses(alert.status)

  // Parse labels if they're a string
  const labels = typeof alert.labels === 'string'
    ? JSON.parse(alert.labels || '{}')
    : (alert.labels || {})

  // Parse payload if it's a string
  const payload = typeof alert.payload === 'string'
    ? JSON.parse(alert.payload || '{}')
    : (alert.payload || {})

  // Format date helper
  const formatDate = (dateStr) => {
    if (!dateStr) return null
    return new Date(dateStr).toLocaleString()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onClose}
      />
      <div className="relative bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header */}
        <div className={cn(
          'px-6 py-4 border-b border-gray-200 dark:border-gray-700',
          alert.severity === 'critical' && 'bg-red-50 dark:bg-red-900/20',
          alert.severity === 'error' && 'bg-orange-50 dark:bg-orange-900/20',
          alert.severity === 'warning' && 'bg-yellow-50 dark:bg-yellow-900/20',
          alert.severity === 'info' && 'bg-blue-50 dark:bg-blue-900/20',
        )}>
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-start gap-3">
              <AlertTriangle
                className={cn(
                  'mt-0.5 flex-shrink-0',
                  alert.severity === 'critical' && 'text-red-500',
                  alert.severity === 'error' && 'text-orange-500',
                  alert.severity === 'warning' && 'text-yellow-500',
                  alert.severity === 'info' && 'text-blue-500',
                )}
                size={24}
              />
              <div>
                <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                  {alert.title}
                </h2>
                <div className="flex flex-wrap items-center gap-2 mt-1">
                  <span className={cn('badge', severityClasses.bg, severityClasses.text)}>
                    {alert.severity}
                  </span>
                  <span className={cn('badge', statusClasses.bg, statusClasses.text)}>
                    {alert.status}
                  </span>
                  {alert.occurrence_count > 1 && (
                    <span className="badge badge-gray">
                      {alert.occurrence_count}x occurrences
                    </span>
                  )}
                </div>
              </div>
            </div>
            <button
              onClick={onClose}
              className="btn-icon flex-shrink-0"
            >
              <X size={20} />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {/* Description */}
          {alert.description && (
            <section>
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
                Description
              </h3>
              <p className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                {alert.description}
              </p>
            </section>
          )}

          {/* Source Information */}
          <section className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {alert.source && (
              <div className="flex items-start gap-2">
                <Server size={16} className="text-gray-400 mt-0.5 flex-shrink-0" />
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">Source</div>
                  <div className="text-sm text-gray-900 dark:text-gray-100">{alert.source}</div>
                </div>
              </div>
            )}
            {alert.source_id && (
              <div className="flex items-start gap-2">
                <Hash size={16} className="text-gray-400 mt-0.5 flex-shrink-0" />
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">Source ID</div>
                  <div className="text-sm text-gray-900 dark:text-gray-100 font-mono">{alert.source_id}</div>
                </div>
              </div>
            )}
            {alert.fingerprint && (
              <div className="flex items-start gap-2">
                <Hash size={16} className="text-gray-400 mt-0.5 flex-shrink-0" />
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">Fingerprint</div>
                  <div className="text-sm text-gray-900 dark:text-gray-100 font-mono truncate" title={alert.fingerprint}>
                    {alert.fingerprint.substring(0, 16)}...
                  </div>
                </div>
              </div>
            )}
            {alert.item_type && (
              <div className="flex items-start gap-2">
                <Tag size={16} className="text-gray-400 mt-0.5 flex-shrink-0" />
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">Type</div>
                  <div className="text-sm text-gray-900 dark:text-gray-100">{alert.item_type}</div>
                </div>
              </div>
            )}
          </section>

          {/* Source URL */}
          {alert.source_url && (
            <section>
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
                Source URL
              </h3>
              <a
                href={alert.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-indigo-600 dark:text-indigo-400 hover:underline text-sm"
              >
                {alert.source_url}
                <ExternalLink size={14} />
              </a>
            </section>
          )}

          {/* Timeline */}
          <section>
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
              Timeline
            </h3>
            <div className="space-y-2 text-sm">
              {alert.first_fired_at && (
                <div className="flex items-center gap-2">
                  <Clock size={14} className="text-gray-400" />
                  <span className="text-gray-500 dark:text-gray-400">First fired:</span>
                  <span className="text-gray-900 dark:text-gray-100">{formatDate(alert.first_fired_at)}</span>
                </div>
              )}
              {alert.last_fired_at && alert.last_fired_at !== alert.first_fired_at && (
                <div className="flex items-center gap-2">
                  <Activity size={14} className="text-gray-400" />
                  <span className="text-gray-500 dark:text-gray-400">Last fired:</span>
                  <span className="text-gray-900 dark:text-gray-100">{formatDate(alert.last_fired_at)}</span>
                </div>
              )}
              {alert.acknowledged_at && (
                <div className="flex items-center gap-2">
                  <Bell size={14} className="text-yellow-500" />
                  <span className="text-gray-500 dark:text-gray-400">Acknowledged:</span>
                  <span className="text-gray-900 dark:text-gray-100">
                    {formatDate(alert.acknowledged_at)}
                    {alert.acknowledged_by && ` by ${alert.acknowledged_by}`}
                  </span>
                </div>
              )}
              {alert.resolved_at && (
                <div className="flex items-center gap-2">
                  <CheckCircle size={14} className="text-green-500" />
                  <span className="text-gray-500 dark:text-gray-400">Resolved:</span>
                  <span className="text-gray-900 dark:text-gray-100">
                    {formatDate(alert.resolved_at)}
                    {alert.resolved_by && ` by ${alert.resolved_by}`}
                  </span>
                </div>
              )}
              {alert.silenced_until && (
                <div className="flex items-center gap-2">
                  <BellOff size={14} className="text-gray-500" />
                  <span className="text-gray-500 dark:text-gray-400">Silenced until:</span>
                  <span className="text-gray-900 dark:text-gray-100">{formatDate(alert.silenced_until)}</span>
                </div>
              )}
              <div className="flex items-center gap-2">
                <Clock size={14} className="text-gray-400" />
                <span className="text-gray-500 dark:text-gray-400">Created:</span>
                <span className="text-gray-900 dark:text-gray-100">{formatDate(alert.created_at)}</span>
              </div>
            </div>
          </section>

          {/* Labels */}
          {Object.keys(labels).length > 0 && (
            <section>
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
                Labels
              </h3>
              <div className="flex flex-wrap gap-2">
                {Object.entries(labels).map(([key, value]) => (
                  <span
                    key={key}
                    className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300"
                  >
                    <span className="font-medium">{key}:</span>
                    <span>{String(value)}</span>
                  </span>
                ))}
              </div>
            </section>
          )}

          {/* Resolution Note */}
          {alert.resolution_note && (
            <section>
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
                Resolution Note
              </h3>
              <p className="text-gray-700 dark:text-gray-300 text-sm whitespace-pre-wrap">
                {alert.resolution_note}
              </p>
            </section>
          )}

          {/* Payload */}
          {Object.keys(payload).length > 0 && (
            <section>
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
                Payload
              </h3>
              <pre className="text-xs bg-gray-100 dark:bg-gray-800 p-3 rounded-lg overflow-x-auto max-h-48">
                {JSON.stringify(payload, null, 2)}
              </pre>
            </section>
          )}

          {/* Metadata */}
          {alert.metadata && Object.keys(alert.metadata).length > 0 && (
            <section>
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
                Metadata
              </h3>
              <pre className="text-xs bg-gray-100 dark:bg-gray-800 p-3 rounded-lg overflow-x-auto max-h-48">
                {JSON.stringify(alert.metadata, null, 2)}
              </pre>
            </section>
          )}
        </div>

        {/* Footer Actions */}
        <FooterActions
          alert={alert}
          onClose={onClose}
          onAcknowledge={onAcknowledge}
          onResolve={onResolve}
          onSilence={onSilence}
          onDelete={onDelete}
        />
      </div>
    </div>
  )
}

function FooterActions({ alert, onClose, onAcknowledge, onResolve, onSilence, onDelete }) {
  const [confirmDelete, setConfirmDelete] = useState(false)

  return (
    <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 flex flex-wrap items-center gap-2">
      {alert.status === 'firing' && onAcknowledge && (
        <button
          onClick={() => { onAcknowledge(alert); onClose(); }}
          className="btn-secondary flex items-center gap-2"
        >
          <Bell size={16} />
          Acknowledge
        </button>
      )}
      {alert.status === 'firing' && onSilence && (
        <button
          onClick={() => { onSilence(alert); onClose(); }}
          className="btn-secondary flex items-center gap-2"
        >
          <BellOff size={16} />
          Silence
        </button>
      )}
      {onResolve && (alert.status === 'firing' || alert.status === 'acknowledged') && (
        <button
          onClick={() => { onResolve(alert); onClose(); }}
          className="btn-primary flex items-center gap-2"
        >
          <CheckCircle size={16} />
          Resolve
        </button>
      )}

      {/* Spacer */}
      <div className="flex-1" />

      {/* Delete */}
      {onDelete && !confirmDelete && (
        <button
          onClick={() => setConfirmDelete(true)}
          className="flex items-center gap-2 px-3 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30 rounded-lg transition-colors"
        >
          <Trash2 size={16} />
          Delete
        </button>
      )}
      {onDelete && confirmDelete && (
        <div className="flex items-center gap-2 bg-red-50 dark:bg-red-900/30 px-3 py-1.5 rounded-lg">
          <span className="text-sm text-red-700 dark:text-red-300">Delete this alert?</span>
          <button
            onClick={() => { onDelete(alert); onClose(); }}
            className="px-2 py-1 text-xs font-medium text-white bg-red-600 rounded hover:bg-red-700"
          >
            Yes
          </button>
          <button
            onClick={() => setConfirmDelete(false)}
            className="px-2 py-1 text-xs font-medium text-red-700 dark:text-red-300 hover:underline"
          >
            No
          </button>
        </div>
      )}
    </div>
  )
}
