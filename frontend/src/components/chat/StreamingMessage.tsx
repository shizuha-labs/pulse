/**
 * Component for displaying a streaming message
 */

import React from 'react'

interface StreamingMessageProps {
  content: string
  activeTools: string[]
}

export const StreamingMessage: React.FC<StreamingMessageProps> = ({
  content,
  activeTools,
}) => {
  return (
    <div className="shzc-flex shzc-gap-3 shzc-animate-fade-in">
      {/* Avatar */}
      <div className="shzc-flex-shrink-0 shzc-w-8 shzc-h-8 shzc-rounded-full shzc-bg-shizuha-600 shzc-flex shzc-items-center shzc-justify-center">
        <span className="shzc-text-white shzc-text-sm shzc-font-medium">S</span>
      </div>

      <div className="shzc-flex-1 shzc-min-w-0">
        {/* Active tools indicator */}
        {activeTools.length > 0 && (
          <div className="shzc-mb-2 shzc-flex shzc-items-center shzc-gap-2 shzc-text-sm shzc-text-gray-500">
            <div className="shzc-w-4 shzc-h-4 shzc-border-2 shzc-border-shizuha-600 shzc-border-t-transparent shzc-rounded-full shzc-animate-spin" />
            <span>Using {activeTools.join(', ')}...</span>
          </div>
        )}

        {/* Streaming content */}
        {content ? (
          <div className="shzc-prose shzc-prose-sm shzc-max-w-none">
            <p className="shzc-whitespace-pre-wrap">{content}</p>
          </div>
        ) : (
          <div className="shzc-flex shzc-items-center shzc-gap-1">
            <div className="shzc-w-2 shzc-h-2 shzc-bg-gray-400 shzc-rounded-full shzc-animate-bounce" style={{ animationDelay: '0ms' }} />
            <div className="shzc-w-2 shzc-h-2 shzc-bg-gray-400 shzc-rounded-full shzc-animate-bounce" style={{ animationDelay: '150ms' }} />
            <div className="shzc-w-2 shzc-h-2 shzc-bg-gray-400 shzc-rounded-full shzc-animate-bounce" style={{ animationDelay: '300ms' }} />
          </div>
        )}
      </div>
    </div>
  )
}
