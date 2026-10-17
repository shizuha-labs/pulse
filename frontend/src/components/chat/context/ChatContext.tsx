/**
 * React Context for Chat Widget state management
 */

import React, { createContext, useContext, useState, useCallback, useMemo } from 'react'
import type { ChatContextValue, ChatWidgetProps } from '../types'
import { useChatSession } from '../hooks/useChatSession'
import { useStreamingMessage } from '../hooks/useStreamingMessage'

const ChatContext = createContext<ChatContextValue | null>(null)

export function useChatContext(): ChatContextValue {
  const context = useContext(ChatContext)
  if (!context) {
    throw new Error('useChatContext must be used within a ChatProvider')
  }
  return context
}

interface ChatProviderProps extends Pick<
  ChatWidgetProps,
  | 'apiBaseUrl'
  | 'getAuthToken'
  | 'sourceService'
  | 'sourceUrl'
  | 'agentId'
  | 'executionMethod'
  | 'persistSession'
  | 'defaultOpen'
  | 'onSessionStart'
  | 'onMessage'
  | 'onError'
> {
  children: React.ReactNode
}

export function ChatProvider({
  children,
  apiBaseUrl,
  getAuthToken,
  sourceService,
  sourceUrl,
  agentId,
  executionMethod,
  persistSession = true,
  defaultOpen = false,
  onSessionStart,
  onMessage,
  onError,
}: ChatProviderProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen)

  const {
    session,
    messages,
    isLoading: isSessionLoading,
    error: sessionError,
    sendMessage: sendSessionMessage,
    clearSession,
    retry: retrySession,
  } = useChatSession({
    apiBaseUrl,
    getAuthToken,
    sourceService,
    sourceUrl,
    agentId,
    executionMethod,
    persistSession,
    onSessionStart,
    onMessage,
    onError,
  })

  const {
    isStreaming,
    streamingContent: _streamingContent,
    activeTools: _activeTools,
    sendStreamingMessage: _sendStreamingMessage,
    cancelStream,
  } = useStreamingMessage({
    apiBaseUrl,
    getAuthToken,
    sessionId: session?.id || null,
    sourceService,
    agentId,
    executionMethod,
    onComplete: (message) => {
      onMessage?.(message)
    },
    onError,
  })

  const open = useCallback(() => setIsOpen(true), [])
  const close = useCallback(() => {
    setIsOpen(false)
    cancelStream()
  }, [cancelStream])
  const toggle = useCallback(() => setIsOpen((prev) => !prev), [])

  const sendMessage = useCallback(
    async (content: string) => {
      await sendSessionMessage(content)
    },
    [sendSessionMessage]
  )

  const retry = useCallback(async () => {
    await retrySession()
  }, [retrySession])

  // Combine loading states
  const isLoading = isSessionLoading || isStreaming

  // Use session error
  const error = sessionError

  const value: ChatContextValue = useMemo(
    () => ({
      session,
      messages,
      isOpen,
      isLoading,
      error,
      open,
      close,
      toggle,
      sendMessage,
      clearSession,
      retry,
    }),
    [session, messages, isOpen, isLoading, error, open, close, toggle, sendMessage, clearSession, retry]
  )

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>
}

export { ChatContext }
