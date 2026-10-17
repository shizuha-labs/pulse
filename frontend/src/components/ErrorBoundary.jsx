import { Component } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

// PLS-915: every page route is lazy-loaded (PLAT-875 code splitting). If a
// route's chunk fails to fetch — transient network blips, server connection
// exhaustion (`net::ERR_INSUFFICIENT_RESOURCES`), or a stale hashed asset
// after a deploy — React.lazy throws during render. Without a boundary React
// unmounts the whole tree and the user sees a blank white page (body is just
// `<div id="root"></div>`), with no way to recover except a manual reload.
//
// This boundary catches those failures and renders a recoverable error state:
//  - chunk-load failures get a "Try again" that re-imports the chunk in place;
//  - any other render error gets a "Reload page" fallback.
// The shell (providers + Layout) stays mounted, so navigation still works.
//
// `key` is set by the caller to the current route path so a navigation to a
// different route resets the boundary instead of showing a stale error.

function isChunkLoadError(error) {
  // Vite/Webpack dynamic-import failures surface as TypeError with the module
  // URL in the message ("Failed to fetch dynamically imported module: <url>").
  // Rollup also emits a bare "Failed to fetch dynamically imported module".
  const message = String(error?.message || '')
  return (
    /Failed to fetch dynamically imported module/.test(message) ||
    /error loading dynamically imported module/.test(message) ||
    /Importing a module script failed/.test(message)
  )
}

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // Keep the failure visible to Sentry / console without crashing the app.
    if (typeof console !== 'undefined') {
      console.error('ErrorBoundary caught render error:', error, info)
    }
  }

  handleRetry = () => {
    // Re-render with a fresh state so the lazy() import is re-attempted.
    this.setState({ error: null })
  }

  handleReload = () => {
    window.location.reload()
  }

  render() {
    const { error } = this.state
    if (!error) {
      return this.props.children
    }

    const isChunkError = isChunkLoadError(error)
    const title = isChunkError
      ? 'This page failed to load'
      : 'Something went wrong'
    const description = isChunkError
      ? 'A part of the app could not be loaded, likely due to a temporary network issue. Try again, or reload the page.'
      : 'An unexpected error occurred while rendering this page. Reload the page to continue.'

    return (
      <div className="flex items-center justify-center min-h-[60vh] p-8">
        <div className="max-w-md w-full text-center">
          <div className="mx-auto w-12 h-12 rounded-full bg-amber-100 dark:bg-amber-900/40 flex items-center justify-center mb-4">
            <AlertTriangle size={24} className="text-amber-600 dark:text-amber-400" />
          </div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
            {title}
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-6">
            {description}
          </p>
          <div className="flex items-center justify-center gap-3">
            {isChunkError && (
              <button
                type="button"
                onClick={this.handleRetry}
                className="btn-primary inline-flex items-center gap-2"
              >
                <RefreshCw size={16} />
                Try again
              </button>
            )}
            <button
              type="button"
              onClick={this.handleReload}
              className="btn-secondary inline-flex items-center gap-2"
            >
              <RefreshCw size={16} />
              Reload page
            </button>
          </div>
        </div>
      </div>
    )
  }
}
