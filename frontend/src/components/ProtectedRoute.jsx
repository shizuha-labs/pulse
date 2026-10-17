import { useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { Loader2 } from 'lucide-react'

/**
 * A wrapper component that protects routes from unauthenticated access.
 * Redirects to Shizuha ID login if not authenticated.
 */
export default function ProtectedRoute({ children }) {
  const { isAuthenticated, isLoading } = useAuth()
  const location = useLocation()

  // Show loading state while checking authentication
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-950">
        <div className="text-center">
          <Loader2 className="w-8 h-8 animate-spin text-brand-600 mx-auto" />
          <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading...</p>
        </div>
      </div>
    )
  }

  // Redirect to Shizuha ID login if not authenticated
  if (!isAuthenticated) {
    // Build continue URL with the current path
    const currentPath = location.pathname + location.search
    const continueUrl = encodeURIComponent(`/pulse${currentPath}`)
    // External redirect to Shizuha ID
    window.location.href = `/id/login?continue=${continueUrl}`
    return null
  }

  return children
}
