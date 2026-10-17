/**
 * Chat message input component
 */

import React, { useState, useRef, useEffect } from 'react'
import { useChatContext } from './context/ChatContext'

interface MessageInputProps {
  placeholder?: string
}

const SendIcon: React.FC = () => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    strokeWidth={1.5}
    stroke="currentColor"
    className="shzc-w-5 shzc-h-5"
  >
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5"
    />
  </svg>
)

export const MessageInput: React.FC<MessageInputProps> = ({
  placeholder = 'Type a message...',
}) => {
  const { sendMessage, isLoading } = useChatContext()
  const [input, setInput] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Auto-resize textarea
  useEffect(() => {
    const textarea = textareaRef.current
    if (textarea) {
      textarea.style.height = 'auto'
      textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`
    }
  }, [input])

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!input.trim() || isLoading) return

    const message = input.trim()
    setInput('')

    try {
      await sendMessage(message)
    } catch {
      // Error is handled by context
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="shzc-p-3 shzc-border-t shzc-border-gray-200 shzc-bg-white"
    >
      <div className="shzc-flex shzc-items-end shzc-gap-2">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={isLoading}
          rows={1}
          className="
            shzc-flex-1
            shzc-resize-none
            shzc-rounded-lg
            shzc-border shzc-border-gray-300
            shzc-px-3 shzc-py-2
            shzc-text-sm
            shzc-text-gray-900
            shzc-placeholder-gray-500
            focus:shzc-outline-none focus:shzc-ring-2 focus:shzc-ring-shizuha-500 focus:shzc-border-transparent
            disabled:shzc-bg-gray-100 disabled:shzc-cursor-not-allowed
          "
        />
        <button
          type="submit"
          disabled={!input.trim() || isLoading}
          className="
            shzc-p-2
            shzc-rounded-lg
            shzc-bg-shizuha-600
            shzc-text-white
            shzc-transition-colors
            hover:shzc-bg-shizuha-700
            disabled:shzc-bg-gray-300 disabled:shzc-cursor-not-allowed
            focus:shzc-outline-none focus:shzc-ring-2 focus:shzc-ring-shizuha-500
          "
          aria-label="Send message"
        >
          {isLoading ? (
            <div className="shzc-w-5 shzc-h-5 shzc-border-2 shzc-border-white shzc-border-t-transparent shzc-rounded-full shzc-animate-spin" />
          ) : (
            <SendIcon />
          )}
        </button>
      </div>
      <p className="shzc-mt-1 shzc-text-xs shzc-text-gray-500">
        Press Enter to send, Shift+Enter for new line
      </p>
    </form>
  )
}
