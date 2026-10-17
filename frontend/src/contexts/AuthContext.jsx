import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import api from '../services/api'
import { isDefiniteAuthFailure, restoreStoredUser } from '../utils/authSession'

const AuthContext = createContext(null)

// Token storage keys
const ACCESS_TOKEN_KEY = 'shizuha_access_token'
const REFRESH_TOKEN_KEY = 'shizuha_refresh_token'
const USER_KEY = 'shizuha_user'

// Get login URL - redirect to unified Shizuha ID login
const getLoginUrl = () => {
  // Redirect to Shizuha ID with continue parameter pointing back to current app
  const currentPath = window.location.pathname
  const continueUrl = encodeURIComponent(currentPath || '/pulse/')
  return `/id/login?continue=${continueUrl}`
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isAuthenticated, setIsAuthenticated] = useState(false)

  // Initialize auth state from localStorage
  useEffect(() => {
    const initAuth = async () => {
      const accessToken = localStorage.getItem(ACCESS_TOKEN_KEY)
      const storedUser = localStorage.getItem(USER_KEY)

      if (accessToken && storedUser) {
        try {
          // Set the token in axios headers
          api.defaults.headers.common['Authorization'] = `Bearer ${accessToken}`

          // Verify token is still valid by fetching user
          const response = await api.get('/auth/user/')
          setUser(response.data)
          setIsAuthenticated(true)
        } catch (error) {
          console.error('Token validation failed:', error)
          const keepStoredSession = () => {
            const parsed = restoreStoredUser(storedUser)
            if (parsed) {
              setUser(parsed)
              setIsAuthenticated(true)
              return true
            }
            return false
          }
          // 5xx / timeout / overlay 504: keep the stored session. Only a
          // definite 401 is a dead session — otherwise ProtectedRoute sends
          // the operator back to /id/login in a loop.
          if (!isDefiniteAuthFailure(error)) {
            if (!keepStoredSession()) clearAuthState()
          } else {
            const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY)
            if (refreshToken) {
              try {
                await refreshAccessToken(refreshToken)
                if (!keepStoredSession()) {
                  setIsAuthenticated(true)
                }
              } catch (refreshErr) {
                if (isDefiniteAuthFailure(refreshErr) || !keepStoredSession()) {
                  clearAuthState()
                }
              }
            } else if (!keepStoredSession()) {
              clearAuthState()
            }
          }
        }
      }
      setIsLoading(false)
    }

    initAuth()
  }, [])

  // Setup axios interceptor for token refresh
  useEffect(() => {
    const interceptor = api.interceptors.response.use(
      (response) => response,
      async (error) => {
        const originalRequest = error.config
        const url = originalRequest?.url || ''

        // CRITICAL: if the refresh endpoint itself returns 401, the refresh
        // token is invalid/expired — log out immediately. Do NOT retry, or
        // we'll recurse infinitely (the retry calls refresh, refresh 401s,
        // interceptor fires again, retries refresh, loop).
        //
        // Same for the login endpoint — 401 there means bad credentials, not
        // a stale access token.
        if (error.response?.status === 401 &&
            (url.includes('/auth/refresh/') || url.includes('/auth/login/'))) {
          clearAuthState()
          if (!url.includes('/auth/login/')) {
            window.location.href = getLoginUrl()
          }
          return Promise.reject(error)
        }

        // If 401 and not already retrying, try to refresh token
        if (error.response?.status === 401 && !originalRequest._retry) {
          originalRequest._retry = true

          const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY)
          if (refreshToken) {
            try {
              const newAccessToken = await refreshAccessToken(refreshToken)
              originalRequest.headers['Authorization'] = `Bearer ${newAccessToken}`
              return api(originalRequest)
            } catch (refreshErr) {
              if (isDefiniteAuthFailure(refreshErr)) {
                clearAuthState()
                window.location.href = getLoginUrl()
              }
              return Promise.reject(error)
            }
          } else {
            clearAuthState()
            window.location.href = getLoginUrl()
          }
        }
        return Promise.reject(error)
      }
    )

    return () => api.interceptors.response.eject(interceptor)
  }, [])

  const refreshAccessToken = async (refreshToken) => {
    const response = await api.post('/auth/refresh/', { refresh: refreshToken })
    const newAccessToken = response.data.access
    localStorage.setItem(ACCESS_TOKEN_KEY, newAccessToken)
    api.defaults.headers.common['Authorization'] = `Bearer ${newAccessToken}`

    // If new refresh token provided, save it
    if (response.data.refresh) {
      localStorage.setItem(REFRESH_TOKEN_KEY, response.data.refresh)
    }

    return newAccessToken
  }

  const clearAuthState = () => {
    localStorage.removeItem(ACCESS_TOKEN_KEY)
    localStorage.removeItem(REFRESH_TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    delete api.defaults.headers.common['Authorization']
    setUser(null)
    setIsAuthenticated(false)
  }

  const login = useCallback(async (username, password) => {
    const response = await api.post('/auth/login/', { username, password })
    const { user: userData, tokens } = response.data

    // Save tokens and user
    localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access)
    localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh)
    localStorage.setItem(USER_KEY, JSON.stringify(userData))

    // Set auth header
    api.defaults.headers.common['Authorization'] = `Bearer ${tokens.access}`

    setUser(userData)
    setIsAuthenticated(true)

    return userData
  }, [])

  const register = useCallback(async (data) => {
    const response = await api.post('/auth/register/', data)
    const { user: userData, tokens } = response.data

    // Save tokens and user
    localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access)
    localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh)
    localStorage.setItem(USER_KEY, JSON.stringify(userData))

    // Set auth header
    api.defaults.headers.common['Authorization'] = `Bearer ${tokens.access}`

    setUser(userData)
    setIsAuthenticated(true)

    return userData
  }, [])

  const logout = useCallback(async () => {
    try {
      const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY)
      if (refreshToken) {
        await api.post('/auth/logout/', { refresh: refreshToken })
      }
    } catch (error) {
      console.error('Logout error:', error)
    } finally {
      clearAuthState()
    }
  }, [])

  const updateUser = useCallback(async (data) => {
    const response = await api.patch('/auth/user/', data)
    const updatedUser = response.data
    localStorage.setItem(USER_KEY, JSON.stringify(updatedUser))
    setUser(updatedUser)
    return updatedUser
  }, [])

  const changePassword = useCallback(async (oldPassword, newPassword, newPassword2) => {
    const response = await api.post('/auth/password/change/', {
      old_password: oldPassword,
      new_password: newPassword,
      new_password2: newPassword2,
    })

    // Update tokens if returned
    if (response.data.tokens) {
      localStorage.setItem(ACCESS_TOKEN_KEY, response.data.tokens.access)
      localStorage.setItem(REFRESH_TOKEN_KEY, response.data.tokens.refresh)
      api.defaults.headers.common['Authorization'] = `Bearer ${response.data.tokens.access}`
    }

    return response.data
  }, [])

  const getOAuthStatus = useCallback(async () => {
    const response = await api.get('/auth/oauth/status/')
    return response.data
  }, [])

  const getOAuthUrl = useCallback(async (provider) => {
    const response = await api.get(`/auth/social/${provider}/`)
    return response.data.url
  }, [])

  const value = {
    user,
    isLoading,
    isAuthenticated,
    login,
    register,
    logout,
    updateUser,
    changePassword,
    getOAuthStatus,
    getOAuthUrl,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}

export default AuthContext
