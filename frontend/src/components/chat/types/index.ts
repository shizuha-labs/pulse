/**
 * TypeScript types for Shizuha Chat Widget
 */

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  toolCalls?: ToolCall[]
  mcpCalls?: MCPCall[]
  modelUsed?: string
  inputTokens?: number
  outputTokens?: number
  durationSeconds?: number
  errorMessage?: string
  createdAt: string
}

export interface ToolCall {
  tool: string
  input?: Record<string, unknown>
  output?: string
  durationMs?: number
}

export interface MCPCall {
  server: string
  tool: string
  input?: Record<string, unknown>
  output?: string
}

export interface ChatAgent {
  id: string
  name: string
  avatarUrl?: string | null
  roleName?: string
}

export interface ChatSession {
  id: string
  userId: string
  organizationId: number
  sourceService: string
  sourceUrl?: string
  executionMethod: string
  mcpServers: string[]
  status: 'active' | 'completed' | 'expired' | 'error'
  messageCount: number
  createdAt: string
  lastActivity: string
  expiresAt?: string
  /** Agent associated with this session */
  agent?: ChatAgent | null
}

export interface ChatWidgetProps {
  /** Base URL for the chatbot API (e.g., "/agent/api/chatbot") */
  apiBaseUrl: string

  /** Function to get the JWT auth token */
  getAuthToken: () => string | Promise<string>

  /** Service where chat was initiated (e.g., "pulse", "wiki") */
  sourceService: string

  /** Current page URL (optional) */
  sourceUrl?: string

  /** Specific agent ID to use for chat (optional - defaults to personal assistant) */
  agentId?: string

  /** Execution method to use (optional - defaults to agent's configured method) */
  executionMethod?: string

  /** Position of the chat button */
  position?: 'bottom-right' | 'bottom-left'

  /** Theme mode */
  theme?: 'light' | 'dark' | 'system'

  /** Custom button icon (React node) */
  buttonIcon?: React.ReactNode

  /** Custom welcome message */
  welcomeMessage?: string

  /** Input placeholder text */
  placeholder?: string

  /** Start with chat window open */
  defaultOpen?: boolean

  /** Persist session across page loads */
  persistSession?: boolean

  /** Show tool/MCP call details in messages */
  showToolCalls?: boolean

  /** Callback when session starts */
  onSessionStart?: (sessionId: string) => void

  /** Callback when message is received */
  onMessage?: (message: ChatMessage) => void

  /** Callback on error */
  onError?: (error: Error) => void

  /** Custom CSS class for the widget container */
  className?: string

  /** Z-index for the widget (default: 9999) */
  zIndex?: number
}

export interface ChatContextValue {
  /** Current session */
  session: ChatSession | null

  /** Message history */
  messages: ChatMessage[]

  /** Whether chat is open */
  isOpen: boolean

  /** Whether currently sending a message */
  isLoading: boolean

  /** Current error */
  error: Error | null

  /** Open the chat window */
  open: () => void

  /** Close the chat window */
  close: () => void

  /** Toggle chat window */
  toggle: () => void

  /** Send a message */
  sendMessage: (content: string) => Promise<void>

  /** Clear current session */
  clearSession: () => void

  /** Retry last failed message */
  retry: () => Promise<void>
}

export interface StreamEvent {
  type: 'session_start' | 'tool_start' | 'tool_complete' | 'content' | 'error' | 'complete'
  data: Record<string, unknown>
}

export interface UseChatSessionOptions {
  apiBaseUrl: string
  getAuthToken: () => string | Promise<string>
  sourceService: string
  sourceUrl?: string
  agentId?: string
  executionMethod?: string
  persistSession?: boolean
  onSessionStart?: (sessionId: string) => void
  onMessage?: (message: ChatMessage) => void
  onError?: (error: Error) => void
}

export interface UseStreamingMessageOptions {
  apiBaseUrl: string
  getAuthToken: () => string | Promise<string>
  sessionId: string | null
  sourceService: string
  agentId?: string
  executionMethod?: string
  onContent?: (delta: string) => void
  onToolStart?: (tool: string, input: Record<string, unknown>) => void
  onToolComplete?: (tool: string, durationMs: number) => void
  onComplete?: (message: ChatMessage) => void
  onError?: (error: Error) => void
}
