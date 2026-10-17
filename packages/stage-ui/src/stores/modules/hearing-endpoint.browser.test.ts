import type { LeadershipMode, SyncedPiniaRuntime } from 'pinia-plugin-synced'

import en from '@proj-airi/i18n/locales/en'

import { createPinia, disposePinia } from 'pinia'
import { createSyncedPiniaPlugin } from 'pinia-plugin-synced'
import { afterEach, expect, it, vi } from 'vitest'
import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'

import { useHearingStore } from './hearing'

const contexts: { pinia: ReturnType<typeof createPinia>, runtime: SyncedPiniaRuntime, dispose: () => void }[] = []

function createWindow(namespace: string, leadership: LeadershipMode) {
  const pinia = createPinia()
  const runtime = createSyncedPiniaPlugin({ callTimeout: 1000, namespace, leadership })
  pinia.use(runtime.plugin)
  let store: ReturnType<typeof useHearingStore> | undefined
  const app = createApp({ setup() {
    store = useHearingStore()
    return () => null
  } })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).use(pinia)
  app.mount(document.createElement('div'))
  contexts.push({ pinia, runtime, dispose: () => app.unmount() })
  if (!store)
    throw new Error('Hearing store did not mount')
  return { store, runtime }
}

afterEach(() => {
  for (const context of contexts.splice(0)) {
    context.dispose()
    context.runtime.dispose()
    disposePinia(context.pinia)
  }
  vi.restoreAllMocks()
  localStorage.clear()
})

it('replicates the audio endpoint without a follower snapshot proposal', async () => {
  const namespace = `hearing-endpoint-${crypto.randomUUID()}`
  const stage = createWindow(namespace, 'leader-only')
  await vi.waitFor(() => expect(stage.runtime.isLeader()).toBe(true))
  const settings = createWindow(namespace, 'follower-only')
  await vi.waitFor(() => expect(settings.runtime.getLeaderId()).toBe(stage.runtime.participantId))
  const traffic = vi.spyOn(BroadcastChannel.prototype, 'postMessage')
  stage.store.endJudgeAudioEndpoint = 'http://127.0.0.1:18085/v1/turn'
  stage.store.endJudgeMaxSilenceMs = 2000
  await vi.waitFor(() => expect(settings.store.endJudgeAudioEndpoint).toBe(stage.store.endJudgeAudioEndpoint))
  await vi.waitFor(() => expect(settings.store.endJudgeMaxSilenceMs).toBe(2000))
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(traffic).not.toHaveBeenCalledWith(expect.objectContaining({ name: 'onCall', rest: expect.arrayContaining(['replaceState']) }))
})

it('defaults to direct transcription without a second chat-model request', async () => {
  const stage = createWindow(`hearing-default-${crypto.randomUUID()}`, 'leader-only')
  await vi.waitFor(() => expect(stage.runtime.isLeader()).toBe(true))
  expect(stage.store.rephraseEnabled).toBe(false)
  expect(stage.store.endJudgeMaxSilenceMs).toBe(6000)
  stage.store.rephraseEnabled = true
  stage.store.resetState()
  expect(stage.store.rephraseEnabled).toBe(false)
})
