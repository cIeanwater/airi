import type { ChatHistoryItem } from '../../../../types/chat'

import { computed, watch } from 'vue'

import { fetchLocalMessageContext, resolveMessageContextTarget } from '../../../../libs/local-message-context'
import { useAuthStore } from '../../../../stores/auth'
import { useChatSessionStore } from '../../../../stores/chat/session-store'
import { useAiriCardStore } from '../../../../stores/modules/airi-card'
import { useConsciousnessStore } from '../../../../stores/modules/consciousness'

/** Each dialog read keeps the selected session and account across asynchronous provider resolution. */
export function useMessageContext() {
  const sessions = useChatSessionStore()
  const cards = useAiriCardStore()
  const consciousness = useConsciousnessStore()
  const auth = useAuthStore()
  const selectionKey = computed(() => JSON.stringify([auth.userId, sessions.activeSessionId]))
  let selectionVersion = 0
  // A synchronous version also invalidates requests when a window switches away and back before an await completes.
  watch(selectionKey, () => selectionVersion++, { flush: 'sync' })

  async function load(message: ChatHistoryItem) {
    const sessionId = sessions.activeSessionId
    const accountId = auth.userId
    const version = selectionVersion
    const meta = sessions.sessionMetas[sessionId]
    const messages = sessions.getSessionMessagesIfLoaded(sessionId)
    if (!meta || meta.userId !== accountId || !messages)
      return { status: 'unavailable', reason: 'not-recorded' } as const
    const target = resolveMessageContextTarget(messages, message)
    if (!target || target.role !== 'assistant')
      return { status: 'unavailable', reason: 'not-recorded' } as const
    const selection = cards.getModules(meta.characterId).consciousness
    if (!selection.provider || !selection.model)
      return { status: 'unavailable', reason: 'unsupported-provider' } as const
    const provider = await consciousness.getChatProviderInstance(selection.provider)
    if (selectionVersion !== version || sessions.sessionMetas[sessionId]?.userId !== accountId)
      return { status: 'unavailable', reason: 'not-recorded' } as const
    if (!provider)
      return { status: 'unavailable', reason: 'unsupported-provider' } as const
    const content = typeof target.content === 'string'
      ? target.content
      : (target.content ?? []).filter(part => part.type === 'text').map(part => part.text).join('')
    const result = await fetchLocalMessageContext(provider.generation(selection.model), {
      message_id: target.id,
      session_id: sessionId,
      ...(content ? { content } : {}),
      created_at: target.createdAt,
    })
    return selectionVersion === version && sessions.sessionMetas[sessionId]?.userId === accountId
      ? result
      : { status: 'unavailable', reason: 'not-recorded' } as const
  }

  return { load, selectionKey }
}
