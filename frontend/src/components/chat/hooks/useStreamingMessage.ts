/**
 * Hook for handling streaming messages via SSE
 */

import { useState, useCallback, useRef } from 'react'
import type { ChatMessage, StreamEvent, UseStreamingMessageOptions } from '../types'

export function useStreamingMessage(options: UseStreamingMessageOptions) {
  const {
    apiBaseUrl,
    getAuthToken,
    sessionId,
    sourceService,
    agentId,
    executionMethod,
    onContent,
    onToolStart,
    onToolComplete,
    onComplete,
    onError,
  } = options

  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const [activeTools, setActiveTools] = useState<string[]>([])

  const abortControllerRef = useRef<AbortController | null>(null)

  const sendStreamingMessage = useCallback(
    async (content: string): Promise<void> => {
      setIsStreaming(true)
      setStreamingContent('')
      setActiveTools([])

      // Cancel any existing stream
      abortControllerRef.current?.abort()
      abortControllerRef.current = new AbortController()

      try {
        const token = await getAuthToken()

        const response = await fetch(`${apiBaseUrl}/message/stream/`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({
            session_id: sessionId,
            content,
            source_service: sourceService,
            agent_id: agentId,
            execution_method: executionMethod,
          }),
          signal: abortControllerRef.current.signal,
        })

        if (!response.ok) {
          throw new Error('Failed to start streaming')
        }

        const reader = response.body?.getReader()
        if (!reader) {
          throw new Error('No response body')
        }

        const decoder = new TextDecoder()
        let buffer = ''
        let accumulatedContent = ''
        const toolCalls: ChatMessage['toolCalls'] = []
        let finalMessage: ChatMessage | null = null

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })

          // Process complete SSE events
          const lines = buffer.split('\n')
          buffer = lines.pop() || '' // Keep incomplete line in buffer

          let eventType = ''
          let eventData = ''

          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7)
            } else if (line.startsWith('data: ')) {
              eventData = line.slice(6)

              if (eventType && eventData) {
                try {
                  const event: StreamEvent = {
                    type: eventType as StreamEvent['type'],
                    data: JSON.parse(eventData),
                  }

                  switch (event.type) {
                    case 'session_start':
                      // Session started
                      break

                    case 'content':
                      const delta = (event.data.delta as string) || ''
                      accumulatedContent += delta
                      setStreamingContent(accumulatedContent)
                      onContent?.(delta)
                      break

                    case 'tool_start':
                      const toolName = event.data.tool as string
                      setActiveTools((prev) => [...prev, toolName])
                      onToolStart?.(toolName, (event.data.input as Record<string, unknown>) || {})
                      break

                    case 'tool_complete':
                      const completedTool = event.data.tool as string
                      setActiveTools((prev) => prev.filter((t) => t !== completedTool))
                      onToolComplete?.(completedTool, (event.data.duration_ms as number) || 0)
                      toolCalls.push({
                        tool: completedTool,
                        durationMs: event.data.duration_ms as number,
                      })
                      break

                    case 'error':
                      const errorMsg = (event.data.message as string) || 'Unknown error'
                      throw new Error(errorMsg)

                    case 'complete':
                      const result = event.data.result as Record<string, unknown> | undefined
                      finalMessage = {
                        id: `stream-${Date.now()}`,
                        role: 'assistant',
                        content: accumulatedContent || (result?.content as string) || '',
                        toolCalls,
                        modelUsed: result?.model_used as string,
                        inputTokens: result?.input_tokens as number,
                        outputTokens: result?.output_tokens as number,
                        durationSeconds: event.data.duration_seconds as number,
                        createdAt: new Date().toISOString(),
                      }
                      break
                  }
                } catch (e) {
                  if (e instanceof SyntaxError) {
                    console.warn('Failed to parse SSE event data:', eventData)
                  } else {
                    throw e
                  }
                }
              }

              // Reset for next event
              eventType = ''
              eventData = ''
            }
          }
        }

        if (finalMessage) {
          onComplete?.(finalMessage)
        }
      } catch (e) {
        if ((e as Error).name === 'AbortError') {
          // Cancelled, not an error
          return
        }
        const error = e instanceof Error ? e : new Error('Streaming failed')
        onError?.(error)
        throw error
      } finally {
        setIsStreaming(false)
        setActiveTools([])
      }
    },
    [
      apiBaseUrl,
      getAuthToken,
      sessionId,
      sourceService,
      agentId,
      executionMethod,
      onContent,
      onToolStart,
      onToolComplete,
      onComplete,
      onError,
    ]
  )

  const cancelStream = useCallback(() => {
    abortControllerRef.current?.abort()
  }, [])

  return {
    isStreaming,
    streamingContent,
    activeTools,
    sendStreamingMessage,
    cancelStream,
  }
}
