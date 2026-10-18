import type { SpeechAudio, TranscriptionEvent } from '@proj-airi/core-agent'
import type { PcmBlock } from '@proj-airi/pipelines-audio'

import { BrowserPlayback } from '@proj-airi/audio/browser'
import { AudioInput, createPushStream, Playback } from '@proj-airi/pipelines-audio'
import { createPinia, disposePinia } from 'pinia'
import { afterEach, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h } from 'vue'
import { createI18n } from 'vue-i18n'

import { useVoiceListening } from '../composables/voice-input-mode'
import { SileroVad } from '../workers/vad/silero-vad'
import { useLlmStreamingControlStore } from './ai/chat-llm/streaming-control'
import { useSpeakingStore } from './audio'
import { useChatStore } from './chat'
import { useChatSessionStore } from './chat/session-store'
import { useHearingStore } from './modules/hearing'
import { useSettingsAudioDevice } from './settings/audio-device'
import { useVoiceStore } from './voice'

const cleanups: (() => Promise<void> | void)[] = []

afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse())
    await cleanup()
  vi.restoreAllMocks()
  localStorage.clear()
})

// ROOT CAUSE:
// The default listener rejected all speech during playback when the browser did not report echo cancellation.
// Continuous VAD now confirms interruption. Browser echo cancellation stays requested, and external echo classifiers remain optional.
it.each(['generating', 'playing'] as const)('interrupts a %s response through default listening without reported echo cancellation and sends the next input', async (stage) => {
  const pinia = createPinia()
  let voice!: ReturnType<typeof useVoiceStore>
  const app = createApp(defineComponent({ setup() {
    voice = useVoiceStore()
    useVoiceListening()
    return () => h('div')
  } }))
  app.use(pinia).use(createI18n({ legacy: false, locale: 'en', messages: { en: {} }, missingWarn: false, fallbackWarn: false }))
  const element = document.body.appendChild(document.createElement('div'))
  app.mount(element)
  cleanups.push(async () => {
    await voice.stopListening()
    await voice.controller.close()
    app.unmount()
    disposePinia(pinia)
    element.remove()
  })

  const hearing = useHearingStore(pinia)
  hearing.activeTranscriptionProvider = 'openai-compatible-audio-transcription'
  hearing.activeTranscriptionModel = 'synthetic-test'
  hearing.inputMode = 'always-on'
  hearing.autoSendEnabled = true
  hearing.autoSendDelay = 64
  hearing.endJudgeEnabled = false
  hearing.rephraseEnabled = false
  const sessions = useChatSessionStore(pinia)
  const sessionId = await sessions.createSession('default')
  const chat = useChatStore(pinia)
  // The chat transport is outside this voice test. Store state, persistence, admission, and playback remain real.
  const submitted = vi.spyOn(chat, 'submit').mockImplementation(async payload => ({ sessionId: payload.sessionId, messageId: payload.messageId }))
  const cancelled = vi.spyOn(chat, 'cancelTurn')
  const controlCancelled = vi.spyOn(useLlmStreamingControlStore(pinia), 'cancelTurn')
  const received: number[][] = []
  const transcribe = vi.spyOn(hearing, 'createTranscriber').mockImplementation(() => ({ transcribe: request => new ReadableStream<TranscriptionEvent>({ async start(output) {
    const samples: number[] = []
    const index = received.push(samples)
    for await (const block of request.audio)
      samples.push(...block.channels[0])
    const text = `Synthetic question ${index}`
    output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'text', revision: 1, text, final: true, tokens: [] }] })
    output.enqueue({ type: 'complete', revision: 1 })
    output.close()
  } }) }))
  // Model inference is the external boundary. Ordered windows and the default store admission policy still run.
  const detect = vi.spyOn(SileroVad.prototype, 'score').mockImplementation(async window => window.channels[0][0])
  const frames = createPushStream<PcmBlock>()
  const input = new AudioInput({ live: true, open: () => frames.stream }, { historyMs: 360 })
  cleanups.push(() => input.close())
  const devices = useSettingsAudioDevice(pinia)
  // Device enumeration settles before synthetic input attaches, so a late default-device change cannot replace it.
  expect(await devices.askPermission()).toBe(true)
  await expect.poll(() => devices.selectedAudioInput).not.toBe('')
  await expect.poll(() => devices.stream).toBeUndefined()
  voice.controller.replaceAudio(input)
  expect(devices.source.echoCancellation).toBe(false)
  expect(devices.deviceConstraints.audio).toMatchObject({ echoCancellation: true, noiseSuppression: true })
  useSpeakingStore(pinia).nowSpeaking = true

  const context = new AudioContext({ sampleRate: 16000 })
  await context.resume()
  const muted = context.createGain()
  muted.gain.value = 0
  muted.connect(context.destination)
  cleanups.push(() => context.close())
  const nodes: AudioBufferSourceNode[] = []
  const ended = vi.fn()
  const playback = new Playback(new BrowserPlayback(context, { destination: muted, onSource: (source) => {
    nodes.push(source)
    source.addEventListener('ended', ended, { once: true })
  } }))
  const parts = createPushStream<SpeechAudio>()
  const activePcm = createPushStream<PcmBlock>()
  const queuedPcmCancelled = vi.fn()
  const queuedPcm = createPushStream<PcmBlock>(queuedPcmCancelled)
  let outputSignal: AbortSignal | undefined
  const disconnect = voice.connectOutput(() => ({ playback, stream: async (text, signal) => {
    outputSignal = signal
    void text.pipeTo(new WritableStream(), { signal }).catch(() => {})
    return parts.stream
  } }))
  cleanups.push(disconnect)
  const turn = { sessionId, turnId: `synthetic-${stage}` }
  voice.startResponse(turn)
  const speech = voice.getSpeech(turn)!
  speech.write('Synthetic answer still in progress.')
  await expect.poll(() => outputSignal).toBeDefined()
  if (stage === 'playing') {
    parts.write({ audio: activePcm.stream })
    parts.write({ audio: queuedPcm.stream })
    activePcm.write({ range: { sourceId: 'synthetic-output', startFrame: 0, endFrame: 32000 }, sampleRate: 16000, channels: [new Float32Array(32000)] })
    await expect.poll(() => nodes.length).toBe(1)
  }

  devices.enabled = true
  let frame = 0
  async function push(speech: boolean) {
    frames.write({ range: { sourceId: 'synthetic-input', startFrame: frame, endFrame: frame + 512 }, sampleRate: 16000, channels: [new Float32Array(512).fill(speech ? 1 : 0)] })
    frame += 512
    await expect.poll(() => detect.mock.calls.length).toBe(frame / 512)
  }

  frames.write({ range: { sourceId: 'synthetic-input', startFrame: 0, endFrame: 8192 }, sampleRate: 16000, channels: [new Float32Array(8192)] })
  frame = 8192
  await expect.poll(() => detect.mock.calls.length).toBe(16)
  await push(true)
  await push(false)
  expect(outputSignal?.aborted).toBe(false)
  expect(cancelled).not.toHaveBeenCalled()
  for (let index = 0; index < 3; index++)
    await push(true)
  expect(outputSignal?.aborted).toBe(true)
  for (let index = 0; index < 9; index++)
    await push(true)
  await expect.poll(() => voice.controller.activeInput?.state.phase).toBe('capturing')
  const attempt = voice.controller.activeInput!
  expect(outputSignal?.aborted).toBe(true)
  expect(cancelled).toHaveBeenCalledWith(turn)
  expect(controlCancelled).toHaveBeenCalledWith(turn.turnId)
  expect(speech.write('Late token.')).toEqual({ status: 'closed' })
  await expect.poll(() => parts.isClosed()).toBe(true)
  parts.write({ audio: new ReadableStream<PcmBlock>({ start: output => output.close() }) })
  activePcm.write({ range: { sourceId: 'synthetic-output', startFrame: 32000, endFrame: 64000 }, sampleRate: 16000, channels: [new Float32Array(32000)] })
  if (stage === 'playing') {
    await expect.poll(() => ended).toHaveBeenCalledOnce()
    await expect.poll(() => queuedPcmCancelled).toHaveBeenCalledOnce()
    expect(nodes).toHaveLength(1)
  }
  await push(false)
  await push(false)
  expect((await attempt.done).status).toBe('committed')
  expect(submitted).toHaveBeenCalledWith(expect.objectContaining({ sessionId, text: 'Synthetic question 1' }))
  expect(received[0].filter(value => value === 1).length).toBeGreaterThanOrEqual(12 * 512)
  expect(transcribe).toHaveBeenCalledOnce()
  expect(frames.isClosed()).toBe(false)

  for (let index = 0; index < 12; index++)
    await push(true)
  const next = voice.controller.activeInput!
  await push(false)
  await push(false)
  expect((await next.done).status).toBe('committed')
  expect(submitted).toHaveBeenCalledTimes(2)
  expect(submitted).toHaveBeenLastCalledWith(expect.objectContaining({ sessionId, text: 'Synthetic question 2' }))
  expect(nodes).toHaveLength(stage === 'playing' ? 1 : 0)
})
