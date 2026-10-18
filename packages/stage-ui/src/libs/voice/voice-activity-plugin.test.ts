import type { SpeechInputAttempt, SpeechSubmission, TranscriptionEvent } from '@proj-airi/core-agent'
import type { PcmBlock } from '@proj-airi/pipelines-audio'

import { VoiceController } from '@proj-airi/core-agent'
import { AudioInput, createPushStream, Playback } from '@proj-airi/pipelines-audio'
import { expect, it, vi } from 'vitest'

import { listensContinuously, startsInputOnSpeech } from '../../stores/modules/hearing'
import { createVoiceActivityPlugin } from './voice-activity-plugin'

it('uses ordered VAD signals to capture, stream transcription, and submit to the accepted session', async () => {
  const source = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => source.stream }, { historyMs: 360 })
  const received: number[] = []
  const submit = vi.fn(async () => ({ status: 'committed' as const, messageId: 'accepted' }))
  let sessionId = 'alice'
  let attempt: SpeechInputAttempt | undefined
  const controller = new VoiceController({ audio, submit, transcriber: () => ({
    transcribe: (request) => {
      const media = request.audio
      return new ReadableStream<TranscriptionEvent>({ async start(output) {
        for await (const block of media)
          received.push(...block.channels[0])
        output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'words', revision: 1, text: 'Hello', final: true, tokens: [] }] })
        output.enqueue({ type: 'complete', revision: 1 })
        output.close()
      } })
    },
  }) })
  controller.use(createVoiceActivityPlugin({
    detect: async window => window.channels[0][0],
    target: () => ({ sessionId, interruptTurns: [] }),
    onInput: () => { attempt = controller.activeInput },
    minSpeechMs: 32,
    silenceMs: 64,
  }), { grants: ['input-control', 'cancel-input'] })
  await Promise.resolve()
  source.write({ range: { sourceId: 'mic', startFrame: 0, endFrame: 32 }, sampleRate: 1000, channels: [new Float32Array(32).fill(1)] })
  await expect.poll(() => attempt?.state.phase).toBe('capturing')
  sessionId = 'bob'
  source.write({ range: { sourceId: 'mic', startFrame: 32, endFrame: 96 }, sampleRate: 1000, channels: [new Float32Array(64)] })
  expect(await attempt!.done).toEqual({ status: 'committed', messageId: 'accepted' })
  expect(received).toHaveLength(96)
  expect(submit).toHaveBeenCalledWith(expect.objectContaining({ sessionId: 'alice', text: 'Hello' }), expect.any(AbortSignal))
  await controller.close()
})

it('rejects playback echo and opens the matched character input only after playback fades', async () => {
  const frames = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => frames.stream })
  const fading = Promise.withResolvers<{ throughMs: number }>()
  const played = vi.fn()
  const fade = vi.fn(() => fading.promise)
  const playback = new Playback({ nowMs: () => 0, play: () => {
    played()
    return { done: new Promise<{ throughMs: number }>(() => {}), stop: fade }
  } })
  const received: number[] = []
  const submit = vi.fn(async () => ({ status: 'committed' as const, messageId: 'wake-message' }))
  const interrupted = vi.fn(async () => ({ status: 'queued' as const }))
  const controller = new VoiceController({ audio, submit, recordInterruption: interrupted, speech: () => ({ playback, synthesize: async () => new Blob(['Speaking']) }), transcriber: () => ({ transcribe(request) {
    const stream = request.audio
    return new ReadableStream<TranscriptionEvent>({ async start(output) {
      for await (const block of stream)
        received.push(...block.channels[0])
      output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'text', revision: 1, text: 'Question', final: true, tokens: [] }] })
      output.enqueue({ type: 'complete', revision: 1 })
      output.close()
    } })
  } }) })
  const turn = { sessionId: 'alice', turnId: 'answer' }
  const response = controller.openResponse(turn)
  const speech = response.openSpeech({ purpose: 'answer' })
  await speech.write('Speaking.')
  speech.end()
  await expect.poll(() => played).toHaveBeenCalledOnce()
  let accepted = false
  const detected = vi.fn(async () => ({ sessionId: 'alice', interruptTurns: [turn] }))
  const gate = vi.fn(async () => accepted)
  controller.use(createVoiceActivityPlugin({
    detect: async window => window.channels[0][0],
    detectWakeWord: async window => window.range.startFrame < 96 ? detected() : undefined,
    acceptSpeech: gate,
    target: () => ({ sessionId: 'other', interruptTurns: [] }),
    minSpeechMs: 32,
    silenceMs: 32,
  }), { grants: ['input-control', 'cancel-input'] })
  await Promise.resolve()
  function push(startFrame: number, value: number) {
    frames.write({ range: { sourceId: 'mic', startFrame, endFrame: startFrame + 32 }, sampleRate: 1000, channels: [new Float32Array(32).fill(value)] })
  }
  push(0, 1)
  await expect.poll(() => gate).toHaveBeenCalledOnce()
  expect(controller.activeInput).toBeUndefined()
  expect(fade).not.toHaveBeenCalled()
  accepted = true
  push(32, 1)
  await expect.poll(() => fade).toHaveBeenCalledOnce()
  const attempt = controller.activeInput!
  expect(attempt.state).toEqual({ phase: 'pending', waitingFor: 'silence' })
  push(64, 1)
  await expect.poll(() => detected).toHaveBeenCalledTimes(3)
  fading.resolve({ throughMs: 100 })
  // After silence, the input subscribes and the next source block admits it.
  await expect.poll(() => attempt.state).toEqual({ phase: 'pending', waitingFor: 'source' })
  push(96, 1)
  await expect.poll(() => attempt.state.phase).toBe('capturing')
  await expect.poll(() => received.length).toBe(32)
  push(128, 0)
  expect(await attempt.done).toEqual({ status: 'committed', messageId: 'wake-message' })
  expect(received).toHaveLength(64)
  expect(submit).toHaveBeenCalledWith(expect.objectContaining({ sessionId: 'alice', text: 'Question' }), expect.any(AbortSignal))
  await expect.poll(() => interrupted).toHaveBeenCalledOnce()
  await controller.close()
})

it('starts an input in wake word mode only after a wake word', async () => {
  const frames = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => frames.stream })
  const submit = vi.fn(async () => ({ status: 'committed' as const, messageId: 'wake-message' }))
  const controller = new VoiceController({ audio, submit, transcriber: () => ({ transcribe: request => new ReadableStream<TranscriptionEvent>({ async start(output) {
    // The test needs only the transcript, so the captured audio is drained and dropped.
    await request.audio.pipeTo(new WritableStream())
    output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'text', revision: 1, text: 'Hello', final: true, tokens: [] }] })
    output.enqueue({ type: 'complete', revision: 1 })
    output.close()
  } }) }) })
  let wake = false
  const target = vi.fn(() => startsInputOnSpeech('wake-word') ? { sessionId: 'active', interruptTurns: [] } : undefined)
  controller.use(createVoiceActivityPlugin({
    detect: async window => window.channels[0][0],
    detectWakeWord: async () => wake ? { sessionId: 'alice', interruptTurns: [] } : undefined,
    target,
    minSpeechMs: 32,
    silenceMs: 64,
  }), { grants: ['input-control', 'cancel-input'] })
  await Promise.resolve()
  function push(startFrame: number, value: number) {
    frames.write({ range: { sourceId: 'mic', startFrame, endFrame: startFrame + 32 }, sampleRate: 1000, channels: [new Float32Array(32).fill(value)] })
  }

  // Speech without a wake word asks for a target, gets none, and starts no input.
  push(0, 1)
  push(32, 1)
  await expect.poll(() => target).toHaveBeenCalledTimes(2)
  expect(controller.activeInput).toBeUndefined()

  wake = true
  push(64, 1)
  await expect.poll(() => controller.activeInput?.sessionId).toBe('alice')
  expect(startsInputOnSpeech('always-on')).toBe(true)
  expect(startsInputOnSpeech('push-to-talk')).toBe(false)
  expect(listensContinuously('wake-word')).toBe(true)
  await controller.close()
})

it('keeps speech across short pauses and captures the next message while batch transcription finishes', async () => {
  const source = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => source.stream }, { historyMs: 360 })
  const pending = [Promise.withResolvers<string>(), Promise.withResolvers<string>()]
  const captured: number[][] = []
  const attempts: SpeechInputAttempt[] = []
  const submit = vi.fn(async (_submission: SpeechSubmission) => ({ status: 'committed' as const, messageId: 'accepted' }))
  const controller = new VoiceController({ audio, submit, transcriber: () => ({
    transcribe(request) {
      const index = captured.length
      const samples: number[] = []
      captured.push(samples)
      return new ReadableStream<TranscriptionEvent>({ async start(output) {
        for await (const block of request.audio)
          samples.push(...block.channels[0])
        const text = await pending[index].promise
        output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'words', revision: 1, text, final: true, tokens: [] }] })
        output.enqueue({ type: 'complete', revision: 1 })
        output.close()
      } })
    },
  }) })
  controller.onInput(attempt => attempts.push(attempt))
  const detect = vi.fn(async window => window.channels[0][0])
  controller.use(createVoiceActivityPlugin({
    detect,
    target: () => ({ sessionId: 'alice', interruptTurns: [] }),
    silenceMs: 2000,
  }), { grants: ['input-control', 'cancel-input'] })
  let frame = 0
  async function push(duration: number, speech: boolean) {
    source.write({ range: { sourceId: 'mic', startFrame: frame, endFrame: frame + duration }, sampleRate: 1000, channels: [new Float32Array(duration).fill(speech ? 1 : 0)] })
    frame += duration
    await expect.poll(() => detect.mock.calls.length).toBe(frame / 32)
  }

  await push(320, true)
  await expect.poll(() => attempts[0]?.state.phase).toBe('capturing')
  await push(992, false)
  expect(attempts[0].state.phase).toBe('capturing')
  await push(320, true)
  await push(1984, false)
  expect(attempts[0].state.phase).toBe('capturing')
  await push(32, false)
  await expect.poll(() => attempts[0].state.phase).toBe('finalizing')
  expect(submit).not.toHaveBeenCalled()

  await push(320, true)
  await expect.poll(() => attempts[1]?.state.phase).toBe('capturing')
  await push(2016, false)
  await expect.poll(() => attempts[1].state.phase).toBe('finalizing')
  pending[0].resolve('continued question')
  pending[1].resolve('next question')

  await expect(attempts[0].done).resolves.toEqual({ status: 'committed', messageId: 'accepted' })
  await expect(attempts[1].done).resolves.toEqual({ status: 'committed', messageId: 'accepted' })
  expect(submit).toHaveBeenCalledTimes(2)
  expect(submit.mock.calls[0][0].text).toBe('continued question')
  expect(submit.mock.calls[1][0].text).toBe('next question')
  expect(captured[0].filter(value => value === 1)).toHaveLength(640)
  expect(captured[1].filter(value => value === 1)).toHaveLength(320)
  await controller.close()
})

// ROOT CAUSE:
// A single positive VAD frame stopped a response before the input's minimum speech check could reject noise.
// Continuous speech now confirms interruption before capture opens, while pre-roll preserves the first phoneme.
it.each(['generating', 'playing'] as const)('confirms sustained speech before interrupting a %s response and preserves its onset', async (stage) => {
  const frames = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => frames.stream }, { historyMs: 360 })
  const synthesis = Promise.withResolvers<Blob>()
  const stopped = vi.fn(async () => ({ throughMs: 20 }))
  const played = vi.fn(() => ({ done: new Promise<{ throughMs: number }>(() => {}), stop: stopped }))
  const playback = new Playback({ nowMs: () => 0, play: played })
  const synthesized = vi.fn(async () => stage === 'generating' ? synthesis.promise : new Blob(['Synthetic speech']))
  const received: number[] = []
  const submitted = vi.fn(async (_submission: SpeechSubmission) => ({ status: 'committed' as const, messageId: 'interruption' }))
  const controller = new VoiceController({ audio, speech: () => ({ playback, synthesize: synthesized }), recordInterruption: async () => ({ status: 'queued' }), submit: submitted, transcriber: () => ({ transcribe: request => new ReadableStream<TranscriptionEvent>({ async start(output) {
    for await (const block of request.audio)
      received.push(...block.channels[0])
    output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'words', revision: 1, text: 'Wait, I have a question.', final: true, tokens: [] }] })
    output.enqueue({ type: 'complete', revision: 1 })
    output.close()
  } }) }) })
  const turn = { sessionId: 'alice', turnId: `barge-${stage}` }
  const response = controller.openResponse(turn)
  const speech = response.openSpeech({ purpose: 'answer' })
  speech.write('The assistant is still answering.')
  speech.end()
  await expect.poll(() => synthesized).toHaveBeenCalledOnce()
  if (stage === 'playing')
    await expect.poll(() => played).toHaveBeenCalledOnce()

  const detect = vi.fn(async window => window.channels[0][0])
  controller.use(createVoiceActivityPlugin({ detect, target: () => ({ sessionId: 'alice', interruptTurns: [turn] }), acceptSpeech: async () => true, minSpeechMs: 32, silenceMs: 32 }), { grants: ['input-control', 'cancel-input'] })
  let frame = 0
  async function push(value: number) {
    frames.write({ range: { sourceId: 'mic', startFrame: frame, endFrame: frame + 32 }, sampleRate: 1000, channels: [new Float32Array(32).fill(value)] })
    frame += 32
    await expect.poll(() => detect.mock.calls.length).toBe(frame / 32)
  }

  try {
    // An established microphone has trimmed its initial history before the user interrupts.
    frames.write({ range: { sourceId: 'mic', startFrame: 0, endFrame: 512 }, sampleRate: 1000, channels: [new Float32Array(512)] })
    frame = 512
    await expect.poll(() => detect.mock.calls.length).toBe(16)
    await push(1)
    expect(response.signal.aborted).toBe(false)
    await push(0)
    expect(response.signal.aborted).toBe(false)
    await push(1)
    await push(1)
    expect(response.signal.aborted).toBe(false)
    await push(1)
    await expect.poll(() => response.signal.aborted).toBe(true)
    await expect.poll(() => controller.activeInput?.state.phase).toBe('capturing')
    const attempt = controller.activeInput!
    await push(0)
    expect(await attempt.done).toEqual({ status: 'committed', messageId: 'interruption' })
    expect(received.filter(value => value === 1)).toHaveLength(128)
    expect(submitted).toHaveBeenCalledWith(expect.objectContaining({ text: 'Wait, I have a question.', sessionId: 'alice' }), expect.any(AbortSignal))
    expect(await response.finish()).toBe('interrupted')
    expect(stopped).toHaveBeenCalledTimes(stage === 'playing' ? 1 : 0)
    expect(speech.write('Late tokens after interruption.')).toEqual({ status: 'closed' })
  }
  finally {
    synthesis.resolve(new Blob(['Synthetic speech']))
    await controller.close()
  }
})

it.each(['session', 'gap'] as const)('requires fresh speech confirmation after a %s change', async (change) => {
  const frames = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => frames.stream }, { historyMs: 360 })
  const playback = new Playback({ nowMs: () => 0, play: () => {
    throw new Error('This response has no synthesized audio')
  } })
  const controller = new VoiceController({
    audio,
    speech: () => ({ playback, synthesize: async () => null }),
    transcriber: () => ({ transcribe: () => new ReadableStream<TranscriptionEvent>() }),
    recordInterruption: async () => ({ status: 'queued' }),
  })
  const alice = { sessionId: 'alice', turnId: 'alice-answer' }
  const bob = { sessionId: 'bob', turnId: 'bob-answer' }
  const first = controller.openResponse(alice)
  const second = controller.openResponse(bob)
  let target = alice
  const detect = vi.fn(async () => 1)
  controller.use(createVoiceActivityPlugin({ detect, target: () => ({ sessionId: target.sessionId, interruptTurns: [target] }) }), { grants: ['input-control', 'cancel-input'] })
  let frame = 0
  let observations = 0
  async function push() {
    frames.write({ range: { sourceId: 'mic', startFrame: frame, endFrame: frame + 32 }, sampleRate: 1000, channels: [new Float32Array(32).fill(1)] })
    frame += 32
    observations += 1
    await expect.poll(() => detect.mock.calls.length).toBe(observations)
  }

  try {
    await push()
    await push()
    if (change === 'session')
      target = bob
    else
      frame += 32
    await push()
    await push()
    expect(first.signal.aborted).toBe(false)
    expect(second.signal.aborted).toBe(false)
    await push()
    await expect.poll(() => (change === 'session' ? second : first).signal.aborted).toBe(true)
    expect((change === 'session' ? first : second).signal.aborted).toBe(false)
    expect(controller.activeInput?.sessionId).toBe(target.sessionId)
  }
  finally {
    await controller.close()
    await audio.close()
  }
})
