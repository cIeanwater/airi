import type { SpeechInputAttempt, SpeechSubmission, TranscriptionEvent } from '@proj-airi/core-agent'
import type { PcmBlock } from '@proj-airi/pipelines-audio'

import en from '@proj-airi/i18n/locales/en'

import { VoiceController } from '@proj-airi/core-agent'
import { AudioInput, createPushStream } from '@proj-airi/pipelines-audio'
import { createPinia, disposePinia } from 'pinia'
import { expect, it, vi } from 'vitest'
import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'

import { useVoiceAudioEnd } from '../../composables/voice-audio-end'
import { useHearingStore } from '../../stores/modules/hearing'
import { SileroVad } from '../../workers/vad/silero-vad'
import { createVoiceActivityPlugin } from './voice-activity-plugin'
import { createVoiceAudioEndPlugin } from './voice-audio-end-plugin'

const endpoint = import.meta.env.VITE_SMART_TURN_TEST_ENDPOINT
const fixture = import.meta.env.VITE_SMART_TURN_TEST_FIXTURE

it('keeps silence and deterministic background noise out of voice turns with real Silero', async () => {
  const source = createPushStream<PcmBlock>()
  const input = new AudioInput({ live: true, open: () => source.stream }, { historyMs: 360 })
  const controller = new VoiceController({ audio: input, transcriber: () => ({ transcribe: () => new ReadableStream<TranscriptionEvent>() }) })
  const vad = new SileroVad()
  const judge = vi.fn(async () => true)
  const onInput = vi.fn()
  const probabilities: number[] = []
  controller.use(createVoiceAudioEndPlugin({ enabled: () => true, judge }), { grants: ['input-control'] })
  controller.use(createVoiceActivityPlugin({
    detect: async (audio, signal) => {
      const probability = await vad.score(audio, signal)
      probabilities.push(probability)
      return probability
    },
    target: () => ({ sessionId: 'noise', interruptTurns: [] }),
    silenceMs: 6000,
    onInput,
  }), { grants: ['input-control', 'cancel-input'] })
  try {
    let seed = 17
    for (let frame = 0; frame < 16000 * 2; frame += 512) {
      const samples = new Float32Array(512)
      if (frame >= 16000) {
        for (let index = 0; index < samples.length; index++) {
          seed = (seed * 1664525 + 1013904223) >>> 0
          samples[index] = ((seed / 0x100000000) * 2 - 1) * 0.05
        }
      }
      source.write({ range: { sourceId: 'noise', startFrame: frame, endFrame: frame + 512 }, sampleRate: 16000, channels: [samples] })
    }
    await vi.waitFor(() => expect(probabilities.length).toBe(63), { timeout: 10_000 })
    expect(Math.max(...probabilities)).toBeLessThan(0.6)
    expect(onInput).not.toHaveBeenCalled()
    expect(judge).not.toHaveBeenCalled()
  }
  finally {
    await controller.close()
    await vad.close()
  }
}, 20_000)

it.skipIf(!endpoint || !fixture)('ends public Chinese speech through real Silero and Smart Turn before batch ASR publishes text', async () => {
  const pinia = createPinia()
  let judge: ReturnType<typeof useVoiceAudioEnd>['judge'] | undefined
  const app = createApp({ setup() {
    useHearingStore().endJudgeAudioEndpoint = endpoint
    judge = useVoiceAudioEnd().judge
    return () => null
  } })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).use(pinia)
  app.mount(document.createElement('div'))
  if (!judge)
    throw new Error('Audio end adapter did not mount')
  const judgeAudio = vi.fn(judge)
  const source = createPushStream<PcmBlock>()
  const input = new AudioInput({ live: true, open: () => source.stream }, { historyMs: 360 })
  const submit = vi.fn<(submission: SpeechSubmission) => Promise<{ status: 'drafted', draftId: string }>>(async () => ({ status: 'drafted', draftId: 'synthetic' }))
  let published = false
  let attempt: SpeechInputAttempt | undefined
  const controller = new VoiceController({ audio: input, submit, transcriber: () => ({
    transcribe: request => new ReadableStream<TranscriptionEvent>({ async start(output) {
      for await (const _block of request.audio) {
        // This fixture models batch ASR. No transcript exists while raw audio is being judged.
      }
      published = true
      output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'speech', revision: 1, text: '公开中文测试片段', final: true, tokens: [] }] })
      output.enqueue({ type: 'complete', revision: 1 })
      output.close()
    } }),
  }) })
  const vad = new SileroVad()
  let judgedBeforeText = false
  controller.use(createVoiceAudioEndPlugin({ enabled: () => true, judge: (audio, rate, signal) => {
    judgedBeforeText = !published
    return judgeAudio(audio, rate, signal)
  } }), { grants: ['input-control'] })
  controller.use(createVoiceActivityPlugin({
    detect: (audio, signal) => vad.score(audio, signal),
    target: () => ({ sessionId: 'synthetic-chinese', interruptTurns: [] }),
    silenceMs: 6000,
    onInput: () => { attempt = controller.activeInput },
  }), { grants: ['input-control', 'cancel-input'] })
  try {
    const response = await fetch(fixture)
    expect(response.ok).toBe(true)
    const recording = new Float32Array(await response.arrayBuffer())
    const samples = new Float32Array(recording.length + 16000 * 2)
    samples.set(recording)
    for (let frame = 0; frame < samples.length; frame += 512) {
      source.write({ range: { sourceId: 'fixture', startFrame: frame, endFrame: frame + 512 }, sampleRate: 16000, channels: [samples.slice(frame, frame + 512)] })
      await new Promise(resolve => setTimeout(resolve, 2))
      if (attempt && attempt.state.phase !== 'capturing')
        break
    }
    await vi.waitFor(() => expect(submit).toHaveBeenCalledOnce(), { timeout: 10_000 })
    expect(judgeAudio).toHaveBeenCalled()
    expect(judgedBeforeText).toBe(true)
    expect(submit.mock.calls[0][0].sessionId).toBe('synthetic-chinese')
  }
  finally {
    await controller.close()
    await vad.close()
    app.unmount()
    disposePinia(pinia)
    localStorage.clear()
  }
}, 30_000)
