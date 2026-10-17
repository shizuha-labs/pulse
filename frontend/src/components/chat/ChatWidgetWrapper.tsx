/**
 * Pre-configured ChatWidget wrapper for Shizuha platform
 *
 * This component provides a ready-to-use chat widget that automatically
 * integrates with Shizuha authentication and configuration.
 *
 * @example
 * ```jsx
 * import { ShizuhaChatWidget } from '@shizuha/chat'
 *
 * function App() {
 *   return (
 *     <div>
 *       <YourApp />
 *       <ShizuhaChatWidget sourceService="pulse" />
 *     </div>
 *   )
 * }
 * ```
 */

import React, { useCallback } from 'react'
import { ChatWidget } from './ChatWidget'
import type { ChatWidgetProps } from './types'

const ACCESS_TOKEN_KEY = 'shizuha_access_token'

interface ShizuhaChatWidgetProps extends Omit<ChatWidgetProps, 'apiBaseUrl' | 'getAuthToken'> {
  /** Override the API base URL (default: /agent/api/chatbot) */
  apiBaseUrl?: string
}

/**
 * Pre-configured ChatWidget for Shizuha platform
 *
 * Automatically uses the standard Shizuha auth token from localStorage.
 */
export const ShizuhaChatWidget: React.FC<ShizuhaChatWidgetProps> = ({
  apiBaseUrl = '/agent/api/chatbot',
  ...props
}) => {
  const getAuthToken = useCallback(() => {
    return localStorage.getItem(ACCESS_TOKEN_KEY) || ''
  }, [])

  return (
    <ChatWidget
      apiBaseUrl={apiBaseUrl}
      getAuthToken={getAuthToken}
      {...props}
    />
  )
}

export default ShizuhaChatWidget
