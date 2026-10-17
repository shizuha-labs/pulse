/**
 * Hook for managing chat sessions
 */

import { useState, useCallback, useEffect, useRef } from 'react'
import type { ChatMessage, ChatSession, UseChatSessionOptions } from '../types'

const SESSION_STORAGE_KEY = 'shizuha-chat-session'

export function useChatSession(options: UseChatSessionOptions) {
  const {
    apiBaseUrl,
    getAuthToken,
    sourceService,
    sourceUrl,
    agentId,
    executionMethod,
    persistSession = true,
    onSessionStart,
    onMessage,
    onError,
  } = options

  const [session, setSession] = useState<ChatSession | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const lastMessageRef = useRef<string | null>(null)

  // Load persisted session on mount
  useEffect(() => {
    if (persistSession) {
      const stored = localStorage.getItem(SESSION_STORAGE_KEY)
      if (stored) {
        try {
          const data = JSON.parse(stored)
          if (data.sourceService === sourceService && data.status === 'active') {
            setSession(data.session)
            loadSessionHistory(data.session.id)
          }
        } catch (e) {
          localStorage.removeItem(SESSION_STORAGE_KEY)
        }
      }
    }
  }, [sourceService, persistSession])

  // Persist session when it changes
  useEffect(() => {
    if (persistSession && session) {
      localStorage.setItem(
        SESSION_STORAGE_KEY,
        JSON.stringify({
          session,
          sourceService,
          status: session.status,
        })
      )
    }
  }, [session, sourceService, persistSession])

  const getHeaders = useCallback(async () => {
    const token = await getAuthToken()
    return {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    }
  }, [getAuthToken])

  const loadSessionHistory = useCallback(
    async (sessionId: string) => {
      try {
        const headers = await getHeaders()
        const response = await fetch(
          `${apiBaseUrl}/sessions/${sessionId}/history/`,
          { headers }
        )

        if (response.ok) {
          const history = await response.json()
          setMessages(
            history.map((msg: Record<string, unknown>) => ({
              id: msg.id as string,
              role: msg.role as 'user' | 'assistant' | 'system',
              content: msg.content as string,
              toolCalls: msg.tool_calls as ChatMessage['toolCalls'],
              mcpCalls: msg.mcp_calls as ChatMessage['mcpCalls'],
              createdAt: msg.created_at as string,
            }))
          )
        }
      } catch (e) {
        console.error('Failed to load session history:', e)
      }
    },
    [apiBaseUrl, getHeaders]
  )

  const createSession = useCallback(async () => {
    try {
      const headers = await getHeaders()
      const response = await fetch(`${apiBaseUrl}/sessions/`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          source_service: sourceService,
          source_url: sourceUrl,
          agent_id: agentId,
          execution_method: executionMethod,
        }),
      })

      if (!response.ok) {
        throw new Error('Failed to create session')
      }

      const data = await response.json()
      const newSession: ChatSession = {
        id: data.id,
        userId: data.user_id,
        organizationId: data.organization_id,
        sourceService: data.source_service,
        sourceUrl: data.source_url,
        executionMethod: data.execution_method,
        mcpServers: data.mcp_servers,
        status: data.status,
        messageCount: data.message_count,
        createdAt: data.created_at,
        lastActivity: data.last_activity,
        expiresAt: data.expires_at,
        agent: data.agent ? {
          id: data.agent.id,
          name: data.agent.name,
          avatarUrl: data.agent.avatar_url,
          roleName: data.agent.role_name,
        } : null,
      }

      setSession(newSession)
      setMessages([])
      onSessionStart?.(newSession.id)

      return newSession
    } catch (e) {
      const error = e instanceof Error ? e : new Error('Failed to create session')
      setError(error)
      onError?.(error)
      throw error
    }
  }, [apiBaseUrl, getHeaders, sourceService, sourceUrl, agentId, executionMethod, onSessionStart, onError])

  const sendMessage = useCallback(
    async (content: string) => {
      setIsLoading(true)
      setError(null)
      lastMessageRef.current = content

      // Add user message immediately
      const userMessage: ChatMessage = {
        id: `temp-${Date.now()}`,
        role: 'user',
        content,
        createdAt: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, userMessage])

      try {
        const headers = await getHeaders()
        const response = await fetch(`${apiBaseUrl}/message/`, {
          method: 'POST',
          headers,
          body: JSON.stringify({
            session_id: session?.id,
            content,
            source_service: sourceService,
            source_url: sourceUrl,
            agent_id: agentId,
            execution_method: executionMethod,
          }),
        })

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}))
          throw new Error(errorData.error || 'Failed to send message')
        }

        const data = await response.json()

        // Update session if new one was created
        if (!session || data.session_id !== session.id) {
          setSession({
            id: data.session_id,
            userId: '',
            organizationId: 0,
            sourceService,
            executionMethod: '',
            mcpServers: [],
            status: 'active',
            messageCount: 2,
            createdAt: new Date().toISOString(),
            lastActivity: new Date().toISOString(),
            // Note: Agent info will be populated when session is properly loaded
            agent: null,
          })
          onSessionStart?.(data.session_id)
        }

        // Update user message with real ID
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === userMessage.id ? { ...msg, id: `user-${Date.now()}` } : msg
          )
        )

        // Add assistant message
        const assistantMessage: ChatMessage = {
          id: data.message_id,
          role: 'assistant',
          content: data.content,
          toolCalls: data.tool_calls,
          mcpCalls: data.mcp_calls,
          modelUsed: data.model_used,
          inputTokens: data.input_tokens,
          outputTokens: data.output_tokens,
          durationSeconds: data.duration_seconds,
          createdAt: new Date().toISOString(),
        }
        setMessages((prev) => [...prev, assistantMessage])
        onMessage?.(assistantMessage)
      } catch (e) {
        const error = e instanceof Error ? e : new Error('Failed to send message')
        setError(error)
        onError?.(error)
        // Remove the temp user message on error
        setMessages((prev) => prev.filter((msg) => msg.id !== userMessage.id))
        throw error
      } finally {
        setIsLoading(false)
      }
    },
    [
      session,
      apiBaseUrl,
      getHeaders,
      sourceService,
      sourceUrl,
      agentId,
      executionMethod,
      onSessionStart,
      onMessage,
      onError,
    ]
  )

  const clearSession = useCallback(() => {
    setSession(null)
    setMessages([])
    setError(null)
    localStorage.removeItem(SESSION_STORAGE_KEY)
  }, [])

  const retry = useCallback(async () => {
    if (lastMessageRef.current) {
      await sendMessage(lastMessageRef.current)
    }
  }, [sendMessage])

  return {
    session,
    messages,
    isLoading,
    error,
    sendMessage,
    clearSession,
    retry,
    createSession,
  }
}
