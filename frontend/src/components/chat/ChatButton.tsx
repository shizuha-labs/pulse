/**
 * Floating chat button component
 */

import React from 'react'
import { useChatContext } from './context/ChatContext'

interface ChatButtonProps {
  icon?: React.ReactNode
  position?: 'bottom-right' | 'bottom-left'
  hasUnread?: boolean
}

const DefaultChatIcon: React.FC = () => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    strokeWidth={1.5}
    stroke="currentColor"
    className="shzc-w-6 shzc-h-6"
  >
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      d="M8.625 12a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H8.25m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0H12m4.125 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 01-2.555-.337A5.972 5.972 0 015.41 20.97a5.969 5.969 0 01-.474-.065 4.48 4.48 0 00.978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25z"
    />
  </svg>
)

const CloseIcon: React.FC = () => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    strokeWidth={1.5}
    stroke="currentColor"
    className="shzc-w-6 shzc-h-6"
  >
    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
  </svg>
)

export const ChatButton: React.FC<ChatButtonProps> = ({
  icon,
  position = 'bottom-right',
  hasUnread = false,
}) => {
  const { isOpen, toggle } = useChatContext()

  const positionClasses = {
    'bottom-right': 'shzc-right-5 shzc-bottom-5',
    'bottom-left': 'shzc-left-5 shzc-bottom-5',
  }

  return (
    <button
      onClick={toggle}
      className={`
        shzc-fixed ${positionClasses[position]}
        shzc-w-14 shzc-h-14
        shzc-rounded-full
        shzc-bg-shizuha-600 hover:shzc-bg-shizuha-700
        shzc-text-white
        shzc-shadow-lg hover:shzc-shadow-xl
        shzc-transition-all shzc-duration-200
        shzc-flex shzc-items-center shzc-justify-center
        shzc-cursor-pointer
        shzc-border-0
        focus:shzc-outline-none focus:shzc-ring-2 focus:shzc-ring-shizuha-500 focus:shzc-ring-offset-2
      `}
      aria-label={isOpen ? 'Close chat' : 'Open chat'}
    >
      {isOpen ? <CloseIcon /> : icon || <DefaultChatIcon />}

      {/* Unread indicator */}
      {hasUnread && !isOpen && (
        <span className="shzc-absolute shzc-top-0 shzc-right-0 shzc-w-3 shzc-h-3 shzc-bg-red-500 shzc-rounded-full shzc-animate-pulse" />
      )}
    </button>
  )
}
