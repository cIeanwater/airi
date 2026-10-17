import type { SpeechAudio, SpeechOutput } from '@proj-airi/core-agent'
import type { PcmBlock, TtsRequest } from '@proj-airi/pipelines-audio'

import type { ReadableSpan } from './use-io-tracer'

import { createPushStream, Playback } from '@proj-airi/pipelines-audio'
import { IOAttributes, IOSpanNames } from '@proj-airi/stage-shared'
import { afterEach, expect, it, vi } from 'vitest'

import { traceSpeechOutput } from './speech-output-trace'
import { onIOSpan } from './use-io-tracer'

const spans: ReadableSpan[] = []
onIOSpan(span => spans.push(span))

afterEach(() => {
  spans.length = 0
})

function request(text: string): TtsRequest {
  return { turnId: 'reply', streamId: 'stream', intentId: 'intent', segmentId: 'segment-1', sequence: 0, text, special: null, reason: 'hard', priority: 0, createdAt: 0 }
}

function spanNamed(name: string) {
  return spans.find(span => span.name === name)
}

it('passes the first streaming PCM block immediately and sums all parts at the original synthesis EOF time', async () => {
  const parts = createPushStream<SpeechAudio>()
  const pcm = createPushStream<PcmBlock>()
  const decoded = Promise.withResolvers<{ duration: number }>()
  const trace = traceSpeechOutput({ sessionId: 'alice', turnId: 'stream-reply' }, {
    playback: new Playback({ nowMs: () => 0, play: vi.fn() }),
    stream: async () => parts.stream,
  }, () => decoded.promise)
  if (!('stream' in trace.output))
    throw new Error('Expected streaming synthesis')

  const text = new ReadableStream<string>({ start(controller) {
    controller.close()
  } })
  const output = (await trace.output.stream(text, new AbortController().signal)).getReader()
  parts.write({ text: '第一句。', audio: pcm.stream })
  const part = await output.read()
  if (part.done || part.value.audio instanceof Blob)
    throw new Error('Expected PCM part')
  const audio = part.value.audio.getReader()
  const first: PcmBlock = { range: { sourceId: 'tts', startFrame: 0, endFrame: 2 }, sampleRate: 1000, channels: [new Float32Array([0.1, 0.2])] }
  pcm.write(first)
  expect((await audio.read()).value).toBe(first)
  expect(spanNamed(IOSpanNames.TTSSynthesis)).toBeUndefined()
  pcm.write({ range: { sourceId: 'tts', startFrame: 2, endFrame: 5 }, sampleRate: 1000, channels: [new Float32Array(3)] })
  pcm.close()
  expect((await audio.read()).value?.range.endFrame).toBe(5)
  expect((await audio.read()).done).toBe(true)

  parts.write({ text: '第二句。', audio: new Blob(['synthetic speech']) })
  parts.close()
  expect((await output.read()).done).toBe(false)
  expect((await output.read()).done).toBe(true)
  const synthesizedBefore = Date.now()
  expect(spanNamed(IOSpanNames.TTSSynthesis)).toBeUndefined()
  decoded.resolve({ duration: 0.25 })
  await vi.waitFor(() => expect(spanNamed(IOSpanNames.TTSSynthesis)).toBeDefined())
  trace.end()

  const synthesis = spanNamed(IOSpanNames.TTSSynthesis)!
  expect(synthesis.attributes[IOAttributes.TTSAudioDurationMs]).toBe(255)
  expect(synthesis.attributes[IOAttributes.TTSText]).toBe('第一句。第二句。')
  expect(synthesis.endTime[0] * 1000 + synthesis.endTime[1] / 1e6).toBeLessThanOrEqual(synthesizedBefore)
  expect(synthesis.attributes[IOAttributes.TTSCanceled]).toBeUndefined()
})

it('records synthesis, playback, and turn spans with the fields that timelines join', async () => {
  const started = vi.fn()
  const ended = vi.fn()
  const output: SpeechOutput = {
    playback: new Playback({ nowMs: () => 0, play: vi.fn() }),
    synthesize: async () => new Blob(['speech']),
    onPlaybackStart: started,
    onPlaybackEnd: ended,
  }
  const trace = traceSpeechOutput({ sessionId: 'alice', turnId: 'reply' }, output, async () => ({ duration: 0.5 }))
  if (!('synthesize' in trace.output))
    throw new Error('Expected pipeline synthesis')

  await trace.output.synthesize(request('Hello.'), new AbortController().signal)
  await vi.waitFor(() => expect(spanNamed(IOSpanNames.TTSSynthesis)).toBeDefined())
  const clip = { id: 'segment-1', text: 'Hello.' }
  trace.output.onPlaybackStart?.(clip)
  trace.output.onPlaybackEnd?.(clip, { status: 'stopped', reason: 'speech-input' })
  trace.end()

  const synthesis = spanNamed(IOSpanNames.TTSSynthesis)!
  expect(synthesis.attributes[IOAttributes.TurnId]).toBe('reply')
  expect(synthesis.attributes[IOAttributes.TTSSegmentId]).toBe('segment-1')
  expect(synthesis.attributes[IOAttributes.TTSText]).toBe('Hello.')
  expect(synthesis.attributes[IOAttributes.TTSChunkReason]).toBe('hard')
  expect(synthesis.attributes[IOAttributes.TTSAudioDurationMs]).toBe(500)
  const playback = spanNamed(IOSpanNames.AudioPlayback)!
  expect(playback.attributes[IOAttributes.TTSSegmentId]).toBe('segment-1')
  expect(playback.attributes[IOAttributes.TTSInterrupted]).toBe(true)
  expect(playback.attributes[IOAttributes.TTSInterruptReason]).toBe('speech-input')
  expect(spanNamed(IOSpanNames.SpeechTurn)?.attributes[IOAttributes.TurnId]).toBe('reply')
  expect(started).toHaveBeenCalledWith(clip)
  expect(ended).toHaveBeenCalledWith(clip, { status: 'stopped', reason: 'speech-input' })
})

it('marks synthesis that is still running when the turn ends as canceled', async () => {
  const pending = Promise.withResolvers<Blob | null>()
  const trace = traceSpeechOutput({ sessionId: 'alice', turnId: 'reply' }, {
    playback: new Playback({ nowMs: () => 0, play: vi.fn() }),
    synthesize: () => pending.promise,
  }, async () => ({ duration: 0 }))
  if (!('synthesize' in trace.output))
    throw new Error('Expected pipeline synthesis')

  const synthesizing = trace.output.synthesize(request('Late.'), new AbortController().signal)
  trace.end()
  pending.resolve(null)
  await synthesizing

  expect(spans.filter(span => span.name === IOSpanNames.TTSSynthesis)).toHaveLength(1)
  expect(spanNamed(IOSpanNames.TTSSynthesis)?.attributes[IOAttributes.TTSCanceled]).toBe(true)
})
