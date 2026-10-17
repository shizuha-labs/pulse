/**
 * Component for displaying the list of chat messages
 */

import React, { useRef, useEffect } from 'react'
import type { ChatMessage } from './types'
import { ToolCallDisplay } from './ToolCallDisplay'

interface MessageListProps {
  messages: ChatMessage[]
  showToolCalls?: boolean
}

const UserAvatar: React.FC = () => (
  <div className="shzc-flex-shrink-0 shzc-w-8 shzc-h-8 shzc-rounded-full shzc-bg-gray-400 shzc-flex shzc-items-center shzc-justify-center">
    <svg
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
      className="shzc-w-5 shzc-h-5 shzc-text-white"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z"
      />
    </svg>
  </div>
)

const AssistantAvatar: React.FC = () => (
  <div className="shzc-flex-shrink-0 shzc-w-8 shzc-h-8 shzc-rounded-full shzc-bg-shizuha-600 shzc-flex shzc-items-center shzc-justify-center">
    <span className="shzc-text-white shzc-text-sm shzc-font-medium">S</span>
  </div>
)

interface MessageItemProps {
  message: ChatMessage
  showToolCalls?: boolean
}

const MessageItem: React.FC<MessageItemProps> = ({ message, showToolCalls }) => {
  const isUser = message.role === 'user'

  return (
    <div
      className={`shzc-flex shzc-gap-3 shzc-animate-slide-up ${
        isUser ? 'shzc-flex-row-reverse' : ''
      }`}
    >
      {isUser ? <UserAvatar /> : <AssistantAvatar />}

      <div className={`shzc-flex-1 shzc-min-w-0 ${isUser ? 'shzc-text-right' : ''}`}>
        <div
          className={`
            shzc-inline-block shzc-max-w-[85%]
            shzc-px-4 shzc-py-2
            shzc-rounded-2xl
            ${
              isUser
                ? 'shzc-bg-shizuha-600 shzc-text-white shzc-rounded-tr-sm'
                : 'shzc-bg-gray-100 shzc-text-gray-900 shzc-rounded-tl-sm'
            }
          `}
        >
          <p className="shzc-whitespace-pre-wrap shzc-text-sm">{message.content}</p>
        </div>

        {/* Show tool calls for assistant messages */}
        {!isUser && showToolCalls && (
          <ToolCallDisplay
            toolCalls={message.toolCalls}
            mcpCalls={message.mcpCalls}
          />
        )}

        {/* Error message */}
        {message.errorMessage && (
          <div className="shzc-mt-1 shzc-text-xs shzc-text-red-600">
            Error: {message.errorMessage}
          </div>
        )}

        {/* Metadata */}
        {!isUser && message.durationSeconds !== undefined && (
          <div className="shzc-mt-1 shzc-text-xs shzc-text-gray-400">
            {message.durationSeconds.toFixed(1)}s
            {message.modelUsed && ` · ${message.modelUsed.split('-').slice(-2).join('-')}`}
          </div>
        )}
      </div>
    </div>
  )
}

export const MessageList: React.FC<MessageListProps> = ({
  messages,
  showToolCalls = true,
}) => {
  const containerRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    const container = containerRef.current
    if (container) {
      container.scrollTop = container.scrollHeight
    }
  }, [messages])

  if (messages.length === 0) {
    return (
      <div className="shzc-flex-1 shzc-flex shzc-items-center shzc-justify-center shzc-text-gray-500 shzc-text-sm">
        <p>Start a conversation...</p>
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      className="shzc-flex-1 shzc-overflow-y-auto shzc-p-4 shzc-space-y-4"
    >
      {messages.map((message) => (
        <MessageItem
          key={message.id}
          message={message}
          showToolCalls={showToolCalls}
        />
      ))}
    </div>
  )
}
