/**
 * Component for displaying tool/MCP calls in messages
 */

import React, { useState } from 'react'
import type { ToolCall, MCPCall } from './types'

interface ToolCallDisplayProps {
  toolCalls?: ToolCall[]
  mcpCalls?: MCPCall[]
}

const ChevronIcon: React.FC<{ expanded: boolean }> = ({ expanded }) => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    strokeWidth={1.5}
    stroke="currentColor"
    className={`shzc-w-4 shzc-h-4 shzc-transition-transform ${expanded ? 'shzc-rotate-180' : ''}`}
  >
    <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
  </svg>
)

const WrenchIcon: React.FC = () => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    strokeWidth={1.5}
    stroke="currentColor"
    className="shzc-w-4 shzc-h-4"
  >
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26m-1.745 1.437l1.745-1.437m6.615 8.206L15.75 15.75M4.867 19.125h.008v.008h-.008v-.008z"
    />
  </svg>
)

const ServerIcon: React.FC = () => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    strokeWidth={1.5}
    stroke="currentColor"
    className="shzc-w-4 shzc-h-4"
  >
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      d="M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0a3 3 0 01-3 3m0 3h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008zm-3 6h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008z"
    />
  </svg>
)

export const ToolCallDisplay: React.FC<ToolCallDisplayProps> = ({
  toolCalls = [],
  mcpCalls = [],
}) => {
  const [expanded, setExpanded] = useState(false)

  const hasTools = toolCalls.length > 0 || mcpCalls.length > 0

  if (!hasTools) return null

  const totalCalls = toolCalls.length + mcpCalls.length

  return (
    <div className="shzc-mt-2 shzc-border shzc-border-gray-200 shzc-rounded-lg shzc-overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="
          shzc-w-full shzc-px-3 shzc-py-2
          shzc-flex shzc-items-center shzc-justify-between
          shzc-bg-gray-50
          shzc-text-sm shzc-text-gray-600
          hover:shzc-bg-gray-100
          shzc-transition-colors
          shzc-border-0 shzc-cursor-pointer
        "
      >
        <span className="shzc-flex shzc-items-center shzc-gap-2">
          <WrenchIcon />
          {totalCalls} tool call{totalCalls !== 1 ? 's' : ''} used
        </span>
        <ChevronIcon expanded={expanded} />
      </button>

      {expanded && (
        <div className="shzc-p-3 shzc-space-y-2 shzc-bg-white">
          {toolCalls.map((call, i) => (
            <div
              key={`tool-${i}`}
              className="shzc-flex shzc-items-start shzc-gap-2 shzc-text-sm"
            >
              <span className="shzc-px-2 shzc-py-0.5 shzc-bg-blue-100 shzc-text-blue-700 shzc-rounded shzc-font-mono shzc-text-xs">
                {call.tool}
              </span>
              {call.durationMs !== undefined && (
                <span className="shzc-text-gray-500 shzc-text-xs">
                  {call.durationMs}ms
                </span>
              )}
            </div>
          ))}

          {mcpCalls.map((call, i) => (
            <div
              key={`mcp-${i}`}
              className="shzc-flex shzc-items-start shzc-gap-2 shzc-text-sm"
            >
              <span className="shzc-flex shzc-items-center shzc-gap-1 shzc-px-2 shzc-py-0.5 shzc-bg-purple-100 shzc-text-purple-700 shzc-rounded shzc-text-xs">
                <ServerIcon />
                {call.server}
              </span>
              <span className="shzc-px-2 shzc-py-0.5 shzc-bg-gray-100 shzc-text-gray-700 shzc-rounded shzc-font-mono shzc-text-xs">
                {call.tool}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
