import type { AIRIStreamTranscriptionDelta } from '../providers/stream-transcription'

import { expect, it, vi } from 'vitest'

import { streamLocalTranscription } from './local-streaming-transcription'

const endpoint = import.meta.env.VITE_LOCAL_ASR_TEST_URL
const fixture = import.meta.env.VITE_LOCAL_ASR_TEST_FIXTURE
const model = 'zipformer-small-zh-en-int8'

it.each(['wss://example.invalid/voice', 'https://localhost:18087', 'ws://user:password@localhost:18087'])('rejects a non-local or credential-bearing transport URL: %s', (url) => {
  expect(() => streamLocalTranscription(url, model, new ReadableStream(), new AbortController().signal)).toThrow('loopback WebSocket')
})

it.skipIf(!endpoint || !fixture)('publishes real Chinese text before input closes, then flushes the final transcript', async () => {
  const response = await fetch(fixture)
  expect(response.ok).toBe(true)
  const context = new AudioContext({ sampleRate: 16000 })
  const buffer = await context.decodeAudioData(await response.arrayBuffer())
  await context.close()
  const samples = buffer.getChannelData(0)
  const pcm = Int16Array.from(samples, sample => Math.round(Math.min(1, Math.max(-1, sample)) * 32767))
  let input!: ReadableStreamDefaultController<Uint8Array>
  const audio = new ReadableStream<Uint8Array>({ start: controller => input = controller })
  const result = streamLocalTranscription(endpoint, model, audio, new AbortController().signal)
  const updates: AIRIStreamTranscriptionDelta[] = []
  const full = result.fullStream.pipeTo(new WritableStream({ write: (update) => {
    updates.push(update)
  } }))
  const text = result.textStream.pipeTo(new WritableStream())
  for (let offset = 0; offset < pcm.length; offset += 1600)
    input.enqueue(new Uint8Array(pcm.slice(offset, offset + 1600).buffer))
  await vi.waitFor(() => expect(updates.some(update => update.type === 'transcript.text.snapshot' && !update.isFinal && update.text.includes('你好'))).toBe(true), { timeout: 5000 })
  input.close()
  await expect(result.text).resolves.toBe('你好我叫小林今天想聊一聊学校里的趣事')
  await Promise.all([full, text])
  expect(updates.at(-1)).toMatchObject({ type: 'transcript.text.snapshot', isFinal: true })
})

it.skipIf(!endpoint)('cancels microphone input and pending output when the caller aborts', async () => {
  const cancelled = vi.fn()
  const abort = new AbortController()
  const audio = new ReadableStream<Uint8Array>({ cancel: cancelled })
  const result = streamLocalTranscription(endpoint, model, audio, abort.signal)
  const text = result.text.catch((error: unknown) => error)
  const full = result.fullStream.pipeTo(new WritableStream()).catch((error: unknown) => error)
  const deltas = result.textStream.pipeTo(new WritableStream()).catch((error: unknown) => error)
  abort.abort(new Error('Synthetic input cancelled'))
  expect(await text).toMatchObject({ message: 'Synthetic input cancelled' })
  await Promise.all([full, deltas])
  expect(cancelled).toHaveBeenCalledOnce()
})

it.skipIf(!endpoint)('reports an invalid model without publishing a transcript', async () => {
  const result = streamLocalTranscription(endpoint, 'invalid-model', new ReadableStream<Uint8Array>(), new AbortController().signal)
  const full = result.fullStream.pipeTo(new WritableStream()).catch((error: unknown) => error)
  const deltas = result.textStream.pipeTo(new WritableStream()).catch((error: unknown) => error)
  await expect(result.text).rejects.toThrow('Invalid local streaming transcription request')
  await Promise.all([full, deltas])
})
