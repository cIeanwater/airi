import type { PcmBlock } from '@proj-airi/pipelines-audio'

import type { VoiceAudioEndOptions } from './voice-audio-end-plugin'

import { VoiceController } from '@proj-airi/core-agent'
import { AudioInput, createPushStream } from '@proj-airi/pipelines-audio'
import { expect, it, vi } from 'vitest'

import { createVoiceActivityPlugin } from './voice-activity-plugin'
import { createVoiceAudioEndPlugin } from './voice-audio-end-plugin'

function createHarness(judge: VoiceAudioEndOptions['judge']) {
  const source = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => source.stream })
  const controller = new VoiceController({
    audio,
    submit: async () => ({ status: 'drafted', draftId: 'draft' }),
    transcriber: () => ({ transcribe: request => new ReadableStream({ async start(output) {
      for await (const _block of request.audio) {
        // A file transcription provider waits until capture ends before it returns text.
      }
      output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'speech', revision: 1, text: 'synthetic speech', final: true, tokens: [] }] })
      output.enqueue({ type: 'complete', revision: 1 })
      output.close()
    } }) }),
  })
  const onError = vi.fn()
  controller.use(createVoiceAudioEndPlugin({ enabled: () => true, judge, onError }), { grants: ['input-control'] })
  const attempt = controller.beginInput({ sessionId: 'alice', interruptTurns: [], start: { kind: 'after-silence' } })
  let frame = 0
  async function hear(speech: boolean, duration = 100, amplitude = speech ? 0.5 : 0) {
    for (let elapsed = 0; elapsed < duration && ['pending', 'capturing'].includes(attempt.state.phase); elapsed += 100) {
      const range = { sourceId: 'mic', startFrame: frame, endFrame: frame + 100 }
      source.write({ range, sampleRate: 1000, channels: [new Float32Array(100).fill(amplitude)] })
      frame += 100
      await expect.poll(() => audio.position?.frame).toBe(range.endFrame)
      attempt.noteActivity({ range, speech })
    }
  }
  return { controller, attempt, hear, onError }
}

it('judges raw speech before batch transcription has produced any text', async () => {
  const judge = vi.fn(async (_audio: Float32Array, _rate: number, _signal: AbortSignal) => true)
  const harness = createHarness(judge)
  await harness.hear(false)
  await expect.poll(() => harness.attempt.state.phase).toBe('capturing')
  await harness.hear(true, 400)
  await harness.hear(false, 900)
  await expect.poll(() => harness.attempt.state.phase).not.toBe('capturing')
  expect(judge).toHaveBeenCalledOnce()
  expect(judge.mock.calls[0]?.[0]).toContain(0.5)
  expect(judge.mock.calls[0]?.[1]).toBe(1000)
  await harness.controller.close()
})

it('keeps an incomplete turn open and aborts its old judgement when speech resumes', async () => {
  const pending = Promise.withResolvers<boolean>()
  const signals: AbortSignal[] = []
  const judge = vi.fn(async (_audio: Float32Array, _rate: number, signal: AbortSignal) => {
    signals.push(signal)
    return signals.length === 1 ? pending.promise : false
  })
  const harness = createHarness(judge)
  await harness.hear(false)
  await harness.hear(true, 400)
  await harness.hear(false, 900)
  await vi.waitFor(() => expect(judge).toHaveBeenCalledOnce())
  await harness.hear(true, 200)
  await harness.hear(false, 100)
  await expect.poll(() => signals[0]?.aborted).toBe(true)
  pending.resolve(true)
  await harness.hear(false, 700)
  expect(harness.attempt.state.phase).toBe('capturing')
  expect(judge).toHaveBeenCalledTimes(2)
  await harness.controller.close()
})

it('never judges silence before speech and retains no more than eight seconds', async () => {
  const judge = vi.fn(async (_audio: Float32Array, _rate: number, _signal: AbortSignal) => false)
  const harness = createHarness(judge)
  await harness.hear(false, 1000, 0.05)
  expect(judge).not.toHaveBeenCalled()
  await harness.hear(true, 9000)
  await harness.hear(false, 900)
  await vi.waitFor(() => expect(judge).toHaveBeenCalledOnce())
  expect(judge.mock.calls[0]?.[0].length).toBe(8000)
  expect(harness.attempt.state.phase).toBe('capturing')
  await harness.controller.close()
})

it.each([2000, 6000])('uses the configured %i ms activity safety limit for an incomplete turn', async (silenceMs) => {
  const source = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => source.stream }, { historyMs: 360 })
  const controller = new VoiceController({ audio, transcriber: () => ({ transcribe: request => new ReadableStream({ async start(output) {
    for await (const _block of request.audio) {
      // Batch providers wait for the capture to end.
    }
    output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'speech', revision: 1, text: 'synthetic speech', final: true, tokens: [] }] })
    output.enqueue({ type: 'complete', revision: 1 })
    output.close()
  } }) }) })
  const judge = vi.fn(async () => false)
  controller.use(createVoiceAudioEndPlugin({ enabled: () => true, judge }), { grants: ['input-control'] })
  controller.use(createVoiceActivityPlugin({
    detect: async window => window.channels[0][0],
    target: () => ({ sessionId: 'synthetic', interruptTurns: [] }),
    silenceMs,
  }), { grants: ['input-control', 'cancel-input'] })
  let frame = 0
  async function hear(speech: boolean, duration: number) {
    for (let elapsed = 0; elapsed < duration; elapsed += 32) {
      const range = { sourceId: 'mic', startFrame: frame, endFrame: frame + 32 }
      source.write({ range, sampleRate: 1000, channels: [new Float32Array(32).fill(speech ? 1 : 0)] })
      frame += 32
      await expect.poll(() => audio.position?.frame).toBe(range.endFrame)
    }
  }
  await hear(true, 320)
  await expect.poll(() => controller.activeInput?.state.phase).toBe('capturing')
  const attempt = controller.activeInput!
  await hear(false, silenceMs - 200)
  expect(attempt.state.phase).toBe('capturing')
  expect(judge).toHaveBeenCalledOnce()
  if (silenceMs === 2000) {
    await hear(true, 320)
    await hear(false, 1800)
    expect(attempt.state.phase).toBe('capturing')
    expect(judge).toHaveBeenCalledTimes(2)
  }
  await hear(false, 320)
  await expect.poll(() => attempt.state.phase).not.toBe('capturing')
  await controller.close()
})

it('reports one endpoint failure per pause and waits for the activity safety limit', async () => {
  const harness = createHarness(async () => {
    throw new Error('Endpoint unavailable')
  })
  await harness.hear(false)
  await harness.hear(true, 400)
  await harness.hear(false, 1800)
  await vi.waitFor(() => expect(harness.onError).toHaveBeenCalledOnce())
  expect(harness.attempt.state.phase).toBe('capturing')
  await harness.controller.close()
})

it.each([{ speechMs: 128, accepted: false }, { speechMs: 320, accepted: true }])('requires 300 ms of VAD speech before semantic completion ($speechMs ms)', async ({ speechMs, accepted }) => {
  const source = createPushStream<PcmBlock>()
  const audio = new AudioInput({ live: true, open: () => source.stream }, { historyMs: 360 })
  const submit = vi.fn(async () => ({ status: 'drafted' as const, draftId: 'synthetic' }))
  const controller = new VoiceController({ audio, submit, transcriber: () => ({ transcribe: request => new ReadableStream({ async start(output) {
    for await (const _block of request.audio) {
      // The batch transcript remains pending until a valid utterance ends.
    }
    output.enqueue({ type: 'update', revision: 1, segments: [{ id: 'speech', revision: 1, text: 'synthetic speech', final: true, tokens: [] }] })
    output.enqueue({ type: 'complete', revision: 1 })
    output.close()
  } }) }) })
  const judge = vi.fn(async () => true)
  controller.use(createVoiceAudioEndPlugin({ enabled: () => true, judge }), { grants: ['input-control'] })
  controller.use(createVoiceActivityPlugin({ detect: async window => window.channels[0][0], target: () => ({ sessionId: 'synthetic', interruptTurns: [] }), minSpeechMs: 300, silenceMs: 6000 }), { grants: ['input-control', 'cancel-input'] })
  try {
    let frame = 0
    for (const speech of [true, false]) {
      const duration = speech ? speechMs : 1200
      for (let elapsed = 0; elapsed < duration; elapsed += 32) {
        if (submit.mock.calls.length > 0)
          break
        const range = { sourceId: 'mic', startFrame: frame, endFrame: frame + 32 }
        source.write({ range, sampleRate: 1000, channels: [new Float32Array(32).fill(speech ? 1 : 0)] })
        frame += 32
        await expect.poll(() => audio.position?.frame).toBe(range.endFrame)
      }
    }
    if (accepted) {
      await vi.waitFor(() => expect(submit).toHaveBeenCalledOnce())
      expect(judge).toHaveBeenCalledOnce()
    }
    else {
      expect(judge).not.toHaveBeenCalled()
      expect(submit).not.toHaveBeenCalled()
      expect(controller.activeInput?.state.phase).toBe('capturing')
    }
  }
  finally {
    await controller.close()
  }
})
