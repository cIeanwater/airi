import type { ChatHistoryItem } from '../../../../types/chat'

import en from '@proj-airi/i18n/locales/en'

import { PiniaColada } from '@pinia/colada'
import { createPinia, disposePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render } from 'vitest-browser-vue'
import { page } from 'vitest/browser'
import { defineComponent, h, nextTick, shallowRef } from 'vue'
import { createI18n } from 'vue-i18n'

import MessageContextDialog from '../components/message-context-dialog.vue'

import { useAuthStore } from '../../../../stores/auth'
import { useChatSessionStore } from '../../../../stores/chat/session-store'
import { useAiriCardStore } from '../../../../stores/modules/airi-card'
import { useConsciousnessStore } from '../../../../stores/modules/consciousness'
import { useProviderConfigStore } from '../../../../stores/providers/config'
import { useMessageContext } from './use-message-context'

import '@unocss/reset/tailwind.css'
import 'virtual:uno.css'

const model = 'gemma4-e2b-int4'
const endpoint = 'http://127.0.0.1:18083/v1/'
const unavailable = { status: 'unavailable', reason: 'not-recorded' }
function saved(sessionId: string, match: 'id' | 'content' = 'id') {
  return { status: 'found', match, records: [{
    request: { messages: [{ role: 'system', content: 'Saved personality before the edit' }], stream: true },
    model_input: { rendered_prompt: '<original model template>' },
    model_output_raw_text: 'Original answer user assistant',
    response: { choices: [{ index: 0, message: { role: 'assistant', content: 'Original answer' } }] },
    stream_events: [{ choices: [{ delta: { content: 'Original answer' } }] }],
    ...(match === 'id' ? { correlation: { session_id: sessionId, turn_id: 'question', message_id: 'answer' } } : {}),
    additional_recorder_field: { exact: ['unchanged', 123, null] },
  }] }
}
const question: ChatHistoryItem = { role: 'user', id: 'question', content: 'Synthetic question', createdAt: 1_700_000_000_000 }
const answer: ChatHistoryItem = { role: 'assistant', id: 'answer', content: 'Displayed answer', createdAt: 1_700_000_001_000, slices: [], tool_results: [] }
const requests: { url: string, body: Record<string, unknown>, headers: Headers, credentials: RequestCredentials | undefined }[] = []
let respond: () => Promise<Response>
let disposeContext: (() => Promise<void>) | undefined

beforeEach(async () => {
  await page.viewport(1280, 720)
  localStorage.clear()
  requests.length = 0
  respond = async () => Response.json(unavailable)
  // The model recorder is the only substituted boundary. Vue, Pinia, cards, and providers remain real.
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, options) => {
    const url = String(input)
    if (url.endsWith('/debug/message-context')) {
      requests.push({ url, body: JSON.parse(String(options?.body)), headers: new Headers(options?.headers), credentials: options?.credentials })
      return respond()
    }
    return Response.json({ data: [], voices: [] }, { status: 503 })
  })
})

afterEach(async () => {
  await disposeContext?.()
  disposeContext = undefined
  vi.restoreAllMocks()
  localStorage.clear()
})

async function context(showDialog = false) {
  const pinia = createPinia()
  const open = shallowRef(false)
  let state: {
    auth: ReturnType<typeof useAuthStore>
    cards: ReturnType<typeof useAiriCardStore>
    sessions: ReturnType<typeof useChatSessionStore>
    consciousness: ReturnType<typeof useConsciousnessStore>
    config: ReturnType<typeof useProviderConfigStore>
    loader: ReturnType<typeof useMessageContext>
    initialized: Promise<void>
  } | undefined
  const view = render(defineComponent({
    setup() {
      const cards = useAiriCardStore()
      state = {
        auth: useAuthStore(),
        cards,
        sessions: useChatSessionStore(),
        consciousness: useConsciousnessStore(),
        config: useProviderConfigStore(),
        loader: useMessageContext(),
        initialized: cards.initialize(),
      }
      return () => showDialog
        ? h(MessageContextDialog, { 'message': answer, 'open': open.value, 'onUpdate:open': value => open.value = value })
        : h('div')
    },
  }), { global: { plugins: [pinia, PiniaColada, createI18n({ legacy: false, locale: 'en', messages: { en } })] } })
  if (!state)
    throw new Error('The integration component did not initialize')
  const instance = state
  const pendingActions = new Set<Promise<void>>()
  const stopObserving = instance.sessions.$onAction(({ after, onError }) => {
    let finish: () => void = () => {}
    const completion = new Promise<void>(resolve => finish = resolve)
    pendingActions.add(completion)
    const complete = () => {
      pendingActions.delete(completion)
      finish()
    }
    after(complete)
    onError(complete)
  })
  disposeContext = async () => {
    open.value = false
    if (instance.auth.user)
      instance.auth.user = null
    await nextTick()
    // Account watchers own async hydration. Drain their real actions before destroying Pinia.
    while (pendingActions.size > 0)
      await Promise.all([...pendingActions])
    stopObserving()
    instance.sessions.dispose()
    view.unmount()
    disposePinia(pinia)
  }
  await instance.initialized
  instance.config.ensureProvider('local-context-test', 'openai-compatible', { baseUrl: endpoint, apiKey: 'private-provider-key' })
  await instance.cards.updateActiveCardConsciousness({ provider: 'local-context-test', model })
  const sessionId = await instance.sessions.createSession('default', { messages: [question, answer] })
  respond = async () => Response.json(saved(sessionId))
  // Warm the real provider metadata and transport before each identity-race test.
  await instance.consciousness.getChatProviderInstance('local-context-test')
  open.value = showDialog
  return { ...instance, sessionId, open, view }
}

describe('message context with real sessions and providers', () => {
  it('reads complete saved JSON for the default character without rebuilding its prompt', async () => {
    const state = await context()
    expect(state.cards.activeCardId).toBe('default')
    state.cards.activeCard!.description = 'Changed personality after the original request'
    expect(await state.loader.load(answer)).toEqual(saved(state.sessionId))
    expect(requests).toHaveLength(1)
    expect(requests[0].url).toBe(`${endpoint}debug/message-context`)
    expect(requests[0].body).toEqual({ message_id: answer.id, session_id: state.sessionId, content: answer.content, created_at: answer.createdAt })
    expect(requests[0].headers.get('Authorization')).toBeNull()
    expect(requests[0].headers.get('Cookie')).toBeNull()
    expect(requests[0].credentials).toBe('omit')
    expect(requests[0].body).not.toHaveProperty('messages')
  })

  it('uses the next assistant answer when the selected item is the user question', async () => {
    const state = await context()
    expect(await state.loader.load(question)).toEqual(saved(state.sessionId))
    expect(requests[0].body.message_id).toBe(answer.id)
    expect(requests[0].body.session_id).toBe(state.sessionId)
  })

  it('does not associate an unanswered question with a later user turn', async () => {
    const state = await context()
    await state.sessions.createSession('default', { messages: [question, { ...question, id: 'second-question' }, answer] })
    expect(await state.loader.load(question)).toEqual(unavailable)
    expect(requests).toHaveLength(0)
  })

  it('passes exact historical response text and time for saved content matching', async () => {
    const state = await context()
    const historical: ChatHistoryItem = { ...answer, id: 'migrated-id', content: 'Historical answer <|ACT {"emotion":"happy"}|>' }
    const historicalSession = await state.sessions.createSession('default', { messages: [question, historical] })
    respond = async () => Response.json(saved(historicalSession, 'content'))
    expect(await state.loader.load(historical)).toEqual(saved(historicalSession, 'content'))
    expect(requests[0].body.content).toBe(historical.content)
    expect(requests[0].body.created_at).toBe(historical.createdAt)
  })

  it('refuses a selected item that does not belong to the active session', async () => {
    const state = await context()
    await state.sessions.createSession('default', { messages: [{ ...question, id: 'other-question' }, { ...answer, id: 'other-answer' }] })
    expect(await state.loader.load(answer)).toEqual(unavailable)
    expect(requests).toHaveLength(0)
  })

  it('refuses another account session before sending its answer to the recorder', async () => {
    const state = await context()
    // Auth changes synchronously. The session watcher clears the prior account on the next tick.
    state.auth.user = { id: 'new-account', name: 'Synthetic user', email: 'test@example.com', emailVerified: true, createdAt: new Date(0), updatedAt: new Date(0) }
    expect(await state.loader.load(answer)).toEqual(unavailable)
    expect(requests).toHaveLength(0)
    await state.sessions.activateCurrentUser()
  })

  it('keeps an unloaded session unchanged during a saved record lookup', async () => {
    const state = await context()
    state.sessions.applyRemoteSnapshot({ ...state.sessions.getSnapshot(), sessionMessages: {} })
    expect(await state.loader.load(answer)).toEqual(unavailable)
    expect(state.sessions.getSnapshot().sessionMessages).toEqual({})
    expect(requests).toHaveLength(0)
  })

  it('does not query the recorder after the session changes during provider resolution', async () => {
    const state = await context()
    const secondSession = await state.sessions.createSession('default', { setActive: false, messages: [{ ...question, id: 'new-question' }] })
    const pending = state.loader.load(answer)
    await state.sessions.setActiveSession(secondSession)
    expect(await pending).toEqual(unavailable)
    expect(requests).toHaveLength(0)
  })

  it('discards a recorder response after the active session changes', async () => {
    const state = await context()
    let finish: ((response: Response) => void) | undefined
    respond = () => new Promise(resolve => finish = resolve)
    const pending = state.loader.load(answer)
    await expect.poll(() => requests.length).toBe(1)
    await state.sessions.createSession('default', { messages: [{ ...question, id: 'new-question' }] })
    finish?.(Response.json(saved(state.sessionId)))
    expect(await pending).toEqual(unavailable)
  })

  it('discards a pending record after the user switches away and back to the same session', async () => {
    const state = await context()
    const secondSession = await state.sessions.createSession('default', { setActive: false, messages: [{ ...question, id: 'new-question' }] })
    let finish: ((response: Response) => void) | undefined
    respond = () => new Promise(resolve => finish = resolve)
    const pending = state.loader.load(answer)
    await expect.poll(() => requests.length).toBe(1)
    await state.sessions.setActiveSession(secondSession)
    await state.sessions.setActiveSession(state.sessionId)
    finish?.(Response.json(saved(state.sessionId)))
    expect(await pending).toEqual(unavailable)
  })

  it('discards a recorder response after the account changes', async () => {
    const state = await context()
    let finish: ((response: Response) => void) | undefined
    respond = () => new Promise(resolve => finish = resolve)
    const pending = state.loader.load(answer)
    await expect.poll(() => requests.length).toBe(1)
    state.auth.user = { id: 'new-account', name: 'Synthetic user', email: 'test@example.com', emailVerified: true, createdAt: new Date(0), updatedAt: new Date(0) }
    finish?.(Response.json(saved(state.sessionId)))
    expect(await pending).toEqual(unavailable)
    await state.sessions.activateCurrentUser()
  })

  it('does not send context queries to a remote provider using the same model ID', async () => {
    const state = await context()
    await state.config.updateProviderConfig('local-context-test', { baseUrl: 'https://remote.example/v1/', apiKey: 'private-provider-key' }, 'configured')
    expect(await state.loader.load(answer)).toEqual({ status: 'unavailable', reason: 'unsupported-provider' })
    expect(requests).toHaveLength(0)
  })

  it('closes the saved JSON dialog when the active session changes', async () => {
    const state = await context(true)
    await expect.element(page.getByLabelText('Complete message record JSON')).toHaveTextContent('Saved personality before the edit')
    await state.sessions.createSession('default', { messages: [{ ...question, id: 'new-question' }] })
    await expect.element(page.getByRole('dialog')).not.toBeInTheDocument()
  })

  it('closes the saved JSON dialog when the account changes', async () => {
    const state = await context(true)
    await expect.element(page.getByLabelText('Complete message record JSON')).toHaveTextContent('Saved personality before the edit')
    state.auth.user = { id: 'new-account', name: 'Synthetic user', email: 'test@example.com', emailVerified: true, createdAt: new Date(0), updatedAt: new Date(0) }
    await expect.element(page.getByRole('dialog')).not.toBeInTheDocument()
    await state.sessions.activateCurrentUser()
  })
})
