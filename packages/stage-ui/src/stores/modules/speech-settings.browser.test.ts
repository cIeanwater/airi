import type { Component } from 'vue'

import en from '@proj-airi/i18n/locales/en'

import { MotionPlugin } from '@vueuse/motion'
import { createPinia, disposePinia } from 'pinia'
import { createSyncedPiniaPlugin } from 'pinia-plugin-synced'
import { afterEach, expect, it, vi } from 'vitest'
import { createApp, h } from 'vue'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'

import SpeechSettings from '../../../../stage-pages/src/pages/settings/modules/speech.vue'

import { injectKeyPiniaSynced } from '../../libs/pinia/synced-context'
import { captureAnalyticsEvent, enableAnalyticsCapture, isAnalyticsAvailableInBuild } from '../../libs/product-signals/client'
import { useProviderConfigStore } from '../providers/config'
import { useProviderStore } from '../providers/provider'
import { useAiriCardStore } from './airi-card'
import { useSpeechStore } from './speech'

// Analytics delivery is external IO. Exercise the real page and stores while
// recording its outgoing payload instead of sending product events.
vi.mock('../../libs/product-signals/client', { spy: true })

const cleanups: Array<() => void> = []

/** Mounts a real renderer with a separate Pinia and BroadcastChannel runtime. */
function mountRenderer(namespace: string, page?: Component, withCards = false) {
  const pinia = createPinia()
  const runtime = createSyncedPiniaPlugin({ namespace, leadership: page ? 'follower-only' : 'leader-only' })
  pinia.use(runtime.plugin)
  const container = document.createElement('div')
  document.body.append(container)
  const app = createApp({
    setup() {
      useSpeechStore()
      if (withCards)
        useAiriCardStore()
      return () => page ? h(page) : null
    },
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  app.provide(injectKeyPiniaSynced, runtime)
    .use(pinia)
    .use(router)
    .use(MotionPlugin)
    .use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    .mount(container)
  cleanups.push(() => {
    app.unmount()
    disposePinia(pinia)
    runtime.dispose()
    container.remove()
  })
  return { app, pinia, runtime, container, speech: useSpeechStore(pinia) }
}

afterEach(() => {
  for (const cleanup of cleanups.splice(0))
    cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

// https://github.com/moeru-ai/airi/actions/runs/34348745853/job/102456521103
// ROOT CAUSE: Page initialization and provider watchers awaited model RPCs
// without handling transport disposal. Passing assertions hid a teardown rejection.
it.each(['mount', 'provider change'])('handles interrupted model discovery after %s', async (trigger) => {
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => Response.json({ voices: [], models: [], flux: 0 })))
  const namespace = `speech-settings:${crypto.randomUUID()}`
  const leader = mountRenderer(namespace)
  await vi.waitFor(() => expect(leader.runtime.isLeader()).toBe(true))
  await useProviderConfigStore(leader.pinia).ensureProvider('microsoft-speech', 'microsoft-speech', {
    apiKey: 'key',
    baseUrl: 'https://voices.invalid/v1/',
    region: 'eastasia',
  })
  await leader.speech.selectProviderModel('speech-noop', '')
  let completed = 0
  useProviderStore(leader.pinia).$onAction(({ name, after }) => {
    if (name === 'loadModelsForConfiguredProviders')
      after(() => completed++)
  })
  let blocked = 0
  let interrupt = trigger === 'mount'
  const postMessage = BroadcastChannel.prototype.postMessage
  vi.spyOn(BroadcastChannel.prototype, 'postMessage').mockImplementation(function (this: BroadcastChannel, message) {
    if (interrupt && JSON.stringify(message).includes('loadModelsForConfiguredProviders')) {
      blocked++
      return
    }
    postMessage.call(this, message)
  })
  const follower = mountRenderer(namespace, SpeechSettings)
  const globalErrors = vi.fn()
  follower.app.config.errorHandler = globalErrors
  if (trigger === 'provider change') {
    await vi.waitFor(() => expect(completed).toBeGreaterThan(0))
    interrupt = true
    await leader.speech.selectProviderModel('microsoft-speech', 'v1')
  }
  await vi.waitFor(() => expect(blocked).toBeGreaterThan(0))
  follower.runtime.dispose()
  await vi.waitFor(() => expect(globalErrors.mock.calls.length > 0 || follower.container.textContent?.includes('Pinia sync runtime was disposed before the RPC completed.')).toBe(true))
  expect(globalErrors).not.toHaveBeenCalled()
  await vi.waitFor(() => expect(follower.container.textContent).toContain('Pinia sync runtime was disposed before the RPC completed.'))
})

// https://github.com/moeru-ai/airi/pull/2490#discussion_r3964866483
// ROOT CAUSE: The click handler read the old model before the leader RPC
// committed the provider. Analytics must use the completed selection receipt.
it('reports the committed provider and model after a settings-page click', async () => {
  localStorage.clear()
  vi.mocked(captureAnalyticsEvent).mockReset().mockReturnValue(true)
  vi.mocked(enableAnalyticsCapture).mockReturnValue(true)
  vi.mocked(isAnalyticsAvailableInBuild).mockReturnValue(true)
  vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => Response.json({ voices: [], models: [], flux: 0 })))
  const namespace = `speech-settings:${crypto.randomUUID()}`
  const leader = mountRenderer(namespace)
  await vi.waitFor(() => expect(leader.runtime.isLeader()).toBe(true))
  await useProviderConfigStore(leader.pinia).ensureProvider('microsoft-speech', 'microsoft-speech', {
    apiKey: 'key',
    baseUrl: 'https://voices.invalid/v1/',
    region: 'eastasia',
  })
  await useProviderStore(leader.pinia).forceProviderConfigured('microsoft-speech')
  await leader.speech.selectProviderModel('speech-noop', 'previous-model')
  const follower = mountRenderer(namespace, SpeechSettings)
  await vi.waitFor(() => expect(follower.speech.activeSpeechModel).toBe('previous-model'))
  await vi.waitFor(() => expect(follower.container.querySelector('input[value="microsoft-speech"]')).not.toBeNull())
  const input = follower.container.querySelector<HTMLInputElement>('input[value="microsoft-speech"]')!
  input.click()
  await vi.waitFor(() => expect(vi.mocked(captureAnalyticsEvent).mock.calls.filter(([name]) => name === 'tts_provider_selected')).toHaveLength(1))
  expect(captureAnalyticsEvent).toHaveBeenCalledWith('tts_provider_selected', expect.objectContaining({
    tts_provider_id: 'microsoft-speech',
    tts_model_id: 'v1',
    source: 'settings',
  }))
  expect(follower.speech.activeSpeechProvider).toBe('microsoft-speech')
})

// https://github.com/moeru-ai/airi/pull/2490#discussion_r3967708960
// ROOT CAUSE: Manual input bypassed the guarded computed setter. A rejected
// leader RPC reached Vue's global handler instead of the page error display.
it('shows a manual model transport failure in the settings page', async () => {
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn<typeof fetch>(async () => Response.json({ voices: [], data: [] })))
  const namespace = `speech-settings:${crypto.randomUUID()}`
  const leader = mountRenderer(namespace)
  await vi.waitFor(() => expect(leader.runtime.isLeader()).toBe(true))
  const provider = 'openai-compatible-audio-speech'
  await useProviderConfigStore(leader.pinia).ensureProvider(provider, provider, { apiKey: 'key', baseUrl: 'https://voices.invalid/v1/' })
  await useProviderStore(leader.pinia).forceProviderConfigured(provider)
  await leader.speech.selectProviderModel(provider, 'tts-1')
  const follower = mountRenderer(namespace, SpeechSettings)
  await vi.waitFor(() => expect(follower.container.querySelector('input[placeholder="tts-1"]')).not.toBeNull())
  await new Promise(resolve => setTimeout(resolve, 100))
  const globalErrors = vi.fn()
  follower.app.config.errorHandler = globalErrors
  const postMessage = BroadcastChannel.prototype.postMessage
  vi.spyOn(BroadcastChannel.prototype, 'postMessage').mockImplementation(function (this: BroadcastChannel, message) {
    if (JSON.stringify(message).includes('selectProviderModel'))
      throw new Error('Model selection transport unavailable')
    postMessage.call(this, message)
  })
  const input = follower.container.querySelector<HTMLInputElement>('input[placeholder="tts-1"]')!
  input.value = 'custom-model'
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await new Promise(resolve => setTimeout(resolve, 100))
  expect(globalErrors).not.toHaveBeenCalled()
  expect(follower.container.textContent).toContain('Model selection transport unavailable')
  expect(leader.speech.activeSpeechModel).toBe('tts-1')
})

it('shows local voice profiles, preserves the card selection, and commits a new voice across renderers', async () => {
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input) => {
    if (String(input).endsWith('/audio/voices')) {
      return Response.json({ model: 'gpt-sovits-v2proplus', voices: [
        { id: 'anime-playful', name: '动漫 · 轻快俏皮', description: 'Bright synthetic reference', gender: 'female' },
        { id: 'anime-soft', name: '动漫 · 轻柔明亮', description: 'Soft synthetic reference', gender: 'female' },
      ] })
    }
    return Response.json({ data: [], voices: [] })
  }))
  const namespace = `speech-settings:${crypto.randomUUID()}`
  const leader = mountRenderer(namespace, undefined, true)
  await vi.waitFor(() => expect(leader.runtime.isLeader()).toBe(true))
  const provider = 'openai-compatible-audio-speech'
  await useProviderConfigStore(leader.pinia).ensureProvider(provider, provider, {
    apiKey: 'local',
    baseUrl: 'http://127.0.0.1:18084/v1/',
    model: 'gpt-sovits-v2proplus',
    voice: 'student-bright',
  })
  await useProviderStore(leader.pinia).forceProviderConfigured(provider)
  const cards = useAiriCardStore(leader.pinia)
  await cards.initialize()
  await cards.updateActiveCardSpeech({ provider, model: 'gpt-sovits-v2proplus', voice_id: 'anime-playful' })
  const follower = mountRenderer(namespace, SpeechSettings)
  await vi.waitFor(() => expect(follower.container.textContent).toContain('动漫 · 轻柔明亮'))
  expect(leader.speech.activeSpeechVoiceId).toBe('anime-playful')
  expect(follower.speech.activeSpeechVoice?.name).toBe('动漫 · 轻快俏皮')
  expect(follower.speech.activeSpeechVoice?.languages).toEqual([{ code: 'zh', title: 'Chinese' }])
  expect(follower.container.querySelector('input[placeholder="Enter voice name (e.g., \'alloy\', \'echo\')"]')).toBeNull()
  const voice = Array.from(follower.container.querySelectorAll('input')).find(input => input.value === 'anime-soft')
  expect(voice).toBeDefined()
  voice!.click()
  await vi.waitFor(() => expect(leader.speech.activeSpeechVoiceId).toBe('anime-soft'))
  await vi.waitFor(() => expect(cards.getModules(cards.activeCardId).speech.voice_id).toBe('anime-soft'))
})
