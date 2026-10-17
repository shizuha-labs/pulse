/**
 * Hook for persisting chat history in local storage
 */

import { useState, useCallback, useEffect } from 'react'
import type { ChatMessage } from '../types'

const HISTORY_STORAGE_KEY = 'shizuha-chat-history'
const MAX_HISTORY_SIZE = 100

interface StoredHistory {
  sessionId: string
  messages: ChatMessage[]
  timestamp: number
}

export function useChatHistory(sessionId: string | null) {
  const [history, setHistory] = useState<ChatMessage[]>([])

  // Load history from storage on mount
  useEffect(() => {
    if (!sessionId) {
      setHistory([])
      return
    }

    try {
      const stored = localStorage.getItem(HISTORY_STORAGE_KEY)
      if (stored) {
        const data: StoredHistory[] = JSON.parse(stored)
        const sessionHistory = data.find((h) => h.sessionId === sessionId)
        if (sessionHistory) {
          setHistory(sessionHistory.messages)
        }
      }
    } catch (e) {
      console.warn('Failed to load chat history:', e)
    }
  }, [sessionId])

  const saveHistory = useCallback(
    (messages: ChatMessage[]) => {
      if (!sessionId) return

      try {
        const stored = localStorage.getItem(HISTORY_STORAGE_KEY)
        let data: StoredHistory[] = stored ? JSON.parse(stored) : []

        // Update or add session history
        const existingIndex = data.findIndex((h) => h.sessionId === sessionId)
        const newEntry: StoredHistory = {
          sessionId,
          messages: messages.slice(-MAX_HISTORY_SIZE),
          timestamp: Date.now(),
        }

        if (existingIndex >= 0) {
          data[existingIndex] = newEntry
        } else {
          data.push(newEntry)
        }

        // Clean up old sessions (keep last 10)
        data = data
          .sort((a, b) => b.timestamp - a.timestamp)
          .slice(0, 10)

        localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(data))
        setHistory(messages)
      } catch (e) {
        console.warn('Failed to save chat history:', e)
      }
    },
    [sessionId]
  )

  const clearHistory = useCallback(() => {
    if (!sessionId) return

    try {
      const stored = localStorage.getItem(HISTORY_STORAGE_KEY)
      if (stored) {
        const data: StoredHistory[] = JSON.parse(stored)
        const filtered = data.filter((h) => h.sessionId !== sessionId)
        localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(filtered))
      }
      setHistory([])
    } catch (e) {
      console.warn('Failed to clear chat history:', e)
    }
  }, [sessionId])

  const addMessage = useCallback(
    (message: ChatMessage) => {
      setHistory((prev) => {
        const newHistory = [...prev, message]
        saveHistory(newHistory)
        return newHistory
      })
    },
    [saveHistory]
  )

  const updateLastMessage = useCallback(
    (update: Partial<ChatMessage>) => {
      setHistory((prev) => {
        if (prev.length === 0) return prev
        const newHistory = [
          ...prev.slice(0, -1),
          { ...prev[prev.length - 1], ...update },
        ]
        saveHistory(newHistory)
        return newHistory
      })
    },
    [saveHistory]
  )

  return {
    history,
    saveHistory,
    clearHistory,
    addMessage,
    updateLastMessage,
  }
}
