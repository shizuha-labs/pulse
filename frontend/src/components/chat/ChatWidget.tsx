/**
 * Main ChatWidget component that combines all chat UI elements
 */

import React from 'react'
import type { ChatWidgetProps } from './types'
import { ChatProvider } from './context/ChatContext'
import { ChatButton } from './ChatButton'
import { ChatWindow } from './ChatWindow'
import './styles/chat.css'

export const ChatWidget: React.FC<ChatWidgetProps> = ({
  apiBaseUrl,
  getAuthToken,
  sourceService,
  sourceUrl,
  agentId,
  executionMethod,
  position = 'bottom-right',
  theme = 'light',
  buttonIcon,
  welcomeMessage,
  placeholder,
  defaultOpen = false,
  persistSession = true,
  showToolCalls = true,
  onSessionStart,
  onMessage,
  onError,
  className = '',
  zIndex = 9999,
}) => {
  return (
    <ChatProvider
      apiBaseUrl={apiBaseUrl}
      getAuthToken={getAuthToken}
      sourceService={sourceService}
      sourceUrl={sourceUrl}
      agentId={agentId}
      executionMethod={executionMethod}
      persistSession={persistSession}
      defaultOpen={defaultOpen}
      onSessionStart={onSessionStart}
      onMessage={onMessage}
      onError={onError}
    >
      <div
        className={`shizuha-chat-widget ${className}`}
        style={{ zIndex }}
        data-theme={theme}
      >
        <ChatWindow
          position={position}
          welcomeMessage={welcomeMessage}
          placeholder={placeholder}
          showToolCalls={showToolCalls}
        />
        <ChatButton icon={buttonIcon} position={position} />
      </div>
    </ChatProvider>
  )
}

export default ChatWidget
