import type { ChatHistoryItem } from '../types/chat'

export interface AgentSessionPort {
  ensureSession: (sessionId: string) => void
  getSessionMessages: (sessionId: string) => ChatHistoryItem[]
  /** Resolves after the appended history item reaches the host's durable storage. */
  appendSessionMessage: (sessionId: string, message: ChatHistoryItem) => Promise<void>
  getSessionGeneration: (sessionId: string) => number
}
