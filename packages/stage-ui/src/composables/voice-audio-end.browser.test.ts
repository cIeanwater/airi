import en from '@proj-airi/i18n/locales/en'

import { createPinia, disposePinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'

import { useHearingStore } from '../stores/modules/hearing'
import { useVoiceAudioEnd } from './voice-audio-end'

let pinia: ReturnType<typeof createPinia>
let app: ReturnType<typeof createApp>
let judge: ReturnType<typeof useVoiceAudioEnd>['judge']

beforeEach(() => {
  pinia = createPinia()
  app = createApp({ setup() {
    useHearingStore().endJudgeAudioEndpoint = 'http://127.0.0.1:18085/v1/turn'
    judge = useVoiceAudioEnd().judge
    return () => null
  } })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).use(pinia)
  app.mount(document.createElement('div'))
})

afterEach(() => {
  app.unmount()
  disposePinia(pinia)
  localStorage.clear()
  vi.restoreAllMocks()
})

async function renderLoudAudio() {
  const context = new OfflineAudioContext(1, 24000, 48000)
  const tone = context.createOscillator()
  const gain = context.createGain()
  gain.gain.value = 1.6
  tone.connect(gain).connect(context.destination)
  tone.start()
  return (await context.startRendering()).getChannelData(0)
}

// ROOT CAUSE:
// Web Audio float samples can exceed full scale after gain or mixing.
// The HTTP endpoint requires normalized PCM. Normalize the request copy without clipping or changing the capture.
it('accepts full-scale Web Audio peaks without a turn-detection HTTP 400', async () => {
  const audio = await renderLoudAudio()
  const original = audio.slice()
  const network = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    const samples = new Float32Array(await new Response(init?.body).arrayBuffer())
    const peak = samples.reduce((value, sample) => Math.max(value, Math.abs(sample)), 0)
    if (peak > 1.001)
      return Response.json({ error: 'Audio must contain finite normalized float32 samples' }, { status: 400 })
    return Response.json({ complete: true, probability: 0.9 })
  })
  expect(audio.some(sample => Math.abs(sample) > 1)).toBe(true)
  await expect(judge(audio, 48000, new AbortController().signal)).resolves.toBe(true)
  expect(network).toHaveBeenCalledOnce()
  expect(audio).toEqual(original)
  const [, request] = network.mock.calls[0]!
  const submitted = new Float32Array(await new Response(request?.body).arrayBuffer())
  const sourcePeak = audio.reduce((value, sample) => Math.max(value, Math.abs(sample)), 0)
  expect(submitted.length).toBe(audio.length)
  expect(submitted.every(sample => Math.abs(sample) <= 1)).toBe(true)
  expect(submitted[100]).toBeCloseTo(audio[100] / sourcePeak, 6)
  expect(request?.credentials).toBe('omit')
})

it.each([Number.NaN, Number.POSITIVE_INFINITY])('rejects non-finite PCM before sending a request (%s)', async (sample) => {
  const network = vi.spyOn(globalThis, 'fetch')
  await expect(judge(new Float32Array([0, sample]), 48000, new AbortController().signal)).rejects.toThrow('finite')
  expect(network).not.toHaveBeenCalled()
})

it('includes the local endpoint validation reason in a failed judgement', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ error: 'Send at most eight seconds of the current turn' }, { status: 400 }))
  await expect(judge(new Float32Array(16000), 16000, new AbortController().signal)).rejects.toThrow('HTTP 400: Send at most eight seconds')
})

it('keeps the HTTP status when an endpoint returns a non-JSON error', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('Unavailable', { status: 503 }))
  await expect(judge(new Float32Array(16000), 16000, new AbortController().signal)).rejects.toThrow('HTTP 503')
})

it.skipIf(!import.meta.env.VITE_SMART_TURN_TEST_ENDPOINT)('sends high-gain Web Audio through the real local Smart Turn HTTP service', async () => {
  useHearingStore(pinia).endJudgeAudioEndpoint = import.meta.env.VITE_SMART_TURN_TEST_ENDPOINT
  const audio = await renderLoudAudio()
  expect(audio.some(sample => Math.abs(sample) > 1)).toBe(true)
  await expect(judge(audio, 48000, new AbortController().signal)).resolves.toBeTypeOf('boolean')
})
