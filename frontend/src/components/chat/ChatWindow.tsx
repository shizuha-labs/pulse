/**
 * Chat window component containing header, messages, and input
 */

import React from 'react'
import { useChatContext } from './context/ChatContext'
import { MessageList } from './MessageList'
import { MessageInput } from './MessageInput'

interface ChatWindowProps {
  position?: 'bottom-right' | 'bottom-left'
  welcomeMessage?: string
  placeholder?: string
  showToolCalls?: boolean
}

const MinimizeIcon: React.FC = () => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    strokeWidth={1.5}
    stroke="currentColor"
    className="shzc-w-5 shzc-h-5"
  >
    <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 12h-15" />
  </svg>
)

const TrashIcon: React.FC = () => (
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
      d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"
    />
  </svg>
)

export const ChatWindow: React.FC<ChatWindowProps> = ({
  position = 'bottom-right',
  welcomeMessage,
  placeholder,
  showToolCalls = true,
}) => {
  const { isOpen, close, messages, error, clearSession, isLoading } = useChatContext()

  if (!isOpen) return null

  const positionClasses = {
    'bottom-right': 'shzc-right-5 shzc-bottom-24',
    'bottom-left': 'shzc-left-5 shzc-bottom-24',
  }

  // Prepend welcome message if provided and no messages yet
  const displayMessages =
    messages.length === 0 && welcomeMessage
      ? [
          {
            id: 'welcome',
            role: 'assistant' as const,
            content: welcomeMessage,
            createdAt: new Date().toISOString(),
          },
        ]
      : messages

  return (
    <div
      className={`
        shzc-fixed ${positionClasses[position]}
        shzc-w-[400px] shzc-max-w-[calc(100vw-40px)]
        shzc-h-[600px] shzc-max-h-[calc(100vh-120px)]
        shzc-bg-white
        shzc-rounded-2xl
        shzc-shadow-2xl
        shzc-flex shzc-flex-col
        shzc-overflow-hidden
        shzc-animate-slide-up
        shzc-border shzc-border-gray-200
      `}
    >
      {/* Header */}
      <div className="shzc-flex shzc-items-center shzc-justify-between shzc-px-4 shzc-py-3 shzc-bg-shizuha-600 shzc-text-white">
        <div className="shzc-flex shzc-items-center shzc-gap-2">
          <div className="shzc-w-8 shzc-h-8 shzc-rounded-full shzc-bg-white/20 shzc-flex shzc-items-center shzc-justify-center">
            <span className="shzc-text-sm shzc-font-bold">S</span>
          </div>
          <div>
            <h3 className="shzc-font-semibold shzc-text-sm">Shizuha</h3>
            <p className="shzc-text-xs shzc-text-white/70">
              {isLoading ? 'Thinking...' : 'Online'}
            </p>
          </div>
        </div>

        <div className="shzc-flex shzc-items-center shzc-gap-1">
          <button
            onClick={clearSession}
            className="shzc-p-1.5 shzc-rounded-lg hover:shzc-bg-white/10 shzc-transition-colors"
            title="Clear conversation"
          >
            <TrashIcon />
          </button>
          <button
            onClick={close}
            className="shzc-p-1.5 shzc-rounded-lg hover:shzc-bg-white/10 shzc-transition-colors"
            title="Minimize"
          >
            <MinimizeIcon />
          </button>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="shzc-px-4 shzc-py-2 shzc-bg-red-50 shzc-border-b shzc-border-red-100">
          <p className="shzc-text-sm shzc-text-red-600">
            {error.message}
          </p>
        </div>
      )}

      {/* Messages */}
      <MessageList messages={displayMessages} showToolCalls={showToolCalls} />

      {/* Input */}
      <MessageInput placeholder={placeholder} />
    </div>
  )
}
