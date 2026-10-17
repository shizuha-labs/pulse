/**
 * Shizuha Chat Widget - AI-powered chatbot for Shizuha platform
 *
 * @example
 * ```tsx
 * import { ChatWidget } from '@shizuha/chat'
 *
 * function App() {
 *   return (
 *     <ChatWidget
 *       apiBaseUrl="/agent/api/chatbot"
 *       getAuthToken={() => localStorage.getItem('token') || ''}
 *       sourceService="pulse"
 *     />
 *   )
 * }
 * ```
 */

// Main components
export { ChatWidget, default } from './ChatWidget'
export { ShizuhaChatWidget } from './ChatWidgetWrapper'

// Individual components for custom compositions
export { ChatButton } from './ChatButton'
export { ChatWindow } from './ChatWindow'
export { MessageList } from './MessageList'
export { MessageInput } from './MessageInput'
export { StreamingMessage } from './StreamingMessage'
export { ToolCallDisplay } from './ToolCallDisplay'

// Context and hooks
export { ChatProvider, useChatContext } from './context/ChatContext'
export { useChatSession } from './hooks/useChatSession'
export { useStreamingMessage } from './hooks/useStreamingMessage'
export { useChatHistory } from './hooks/useChatHistory'

// Types
export type {
  ChatWidgetProps,
  ChatContextValue,
  ChatMessage,
  ChatSession,
  ToolCall,
  MCPCall,
  StreamEvent,
  UseChatSessionOptions,
  UseStreamingMessageOptions,
} from './types'
