import { IOAttributes, IOSpanNames } from '@proj-airi/stage-shared/perf/io-trace'

import { describe, expect, it } from '../../src'
import { configureModuleConsciousness, configureModuleHearing, configureModuleSpeech, configureOnboarding } from '../shared/configurations'
import { configureStorage } from '../shared/configurations/storage'
import { openaiAsr, openaiLlm, openaiTts } from '../shared/providers'

function requireLoopbackUrl(value: string | undefined, name: string, protocols = ['http:', 'https:']) {
  if (!value)
    throw new Error(`Set ${name} for the local conversation case.`)
  const url = new URL(value)
  if (!protocols.includes(url.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname))
    throw new Error(`${name} requires a loopback endpoint.`)
  return value
}

describe('local automatic voice conversation', () => {
  // Only the microphone is replaced. The application runs its real VAD, ASR, chat model, TTS, and playback.
  it('sends speech after a pause and plays the local model reply without a send click', {
    input: new URL('../single-utterance-pipeline/input.test.wav', import.meta.url),
    preflight: [
      async (context) => {
        context.skip(context.env.TESTING_AUDIO_LOCAL_CONVERSATION !== '1', 'Set TESTING_AUDIO_LOCAL_CONVERSATION=1 to run the local conversation case.')
      },
      configureOnboarding(() => ({ completed: true })),
      configureModuleHearing(async ({ env }) => {
        const provider = openaiAsr({
          apiKey: env.TESTING_AUDIO_LOCAL_ASR_API_KEY ?? 'local-test',
          baseUrl: requireLoopbackUrl(env.TESTING_AUDIO_LOCAL_ASR_BASE_URL, 'TESTING_AUDIO_LOCAL_ASR_BASE_URL'),
          model: env.TESTING_AUDIO_LOCAL_ASR_MODEL ?? 'Systran/faster-whisper-small',
          provider: 'openai-compatible-audio-transcription',
        })
        provider.config.language = 'en'
        if (env.TESTING_AUDIO_LOCAL_ASR_STREAM_URL)
          provider.config.localStreamingUrl = requireLoopbackUrl(env.TESTING_AUDIO_LOCAL_ASR_STREAM_URL, 'TESTING_AUDIO_LOCAL_ASR_STREAM_URL', ['ws:', 'wss:'])
        return { provider, captureFormat: env.TESTING_AUDIO_LOCAL_ASR_STREAM_URL ? 'pcm' : 'wav', autoSend: true, microphoneEnabled: true }
      }),
      configureModuleConsciousness(({ env }) => ({
        provider: openaiLlm({
          apiKey: env.TESTING_AUDIO_LOCAL_LLM_API_KEY ?? 'local',
          baseUrl: requireLoopbackUrl(env.TESTING_AUDIO_LOCAL_LLM_BASE_URL, 'TESTING_AUDIO_LOCAL_LLM_BASE_URL'),
          model: env.TESTING_AUDIO_LOCAL_LLM_MODEL ?? 'qwen3.5-2b-bf16',
          provider: 'openai-compatible',
        }),
      })),
      configureModuleSpeech(({ env }) => openaiTts({
        apiKey: env.TESTING_AUDIO_LOCAL_TTS_API_KEY ?? 'local',
        baseUrl: requireLoopbackUrl(env.TESTING_AUDIO_LOCAL_TTS_BASE_URL, 'TESTING_AUDIO_LOCAL_TTS_BASE_URL'),
        model: env.TESTING_AUDIO_LOCAL_TTS_MODEL ?? 'tts-1',
        provider: 'openai-compatible-audio-speech',
        voice: env.TESTING_AUDIO_LOCAL_TTS_VOICE ?? 'zf_xiaoxiao',
      })),
      async ({ runtime, env }) => configureStorage(runtime, {
        'settings/hearing/auto-send-delay': '2000',
        'settings/hearing/rephrase-enabled': env.TESTING_AUDIO_LOCAL_REPHRASE === '1' ? 'true' : 'false',
        'settings/hearing/end-judge-enabled': 'true',
        'settings/hearing/end-judge-max-silence-ms': '2000',
        'settings/hearing/end-judge-audio-endpoint': env.TESTING_AUDIO_LOCAL_TURN_ENDPOINT
          ? requireLoopbackUrl(env.TESTING_AUDIO_LOCAL_TURN_ENDPOINT, 'TESTING_AUDIO_LOCAL_TURN_ENDPOINT')
          : '',
        'settings/hearing/input-mode': 'always-on',
        'settings/language': 'en',
      }),
    ],
  }, async ({ audio }) => {
    await audio.waitForVoiceInput({ timeout: 90_000 })
    await expect(audio).toHaveCapturedTranscriptionAudio({ count: 1, minimumBytes: 8000 })
    await expect(audio).toHaveTranscriptions([['Please say hello.', 'Say hello.', 'PLEASE SAY HELLO']], { match: 'contains' })
    if (audio.transcriptionCaptureFormat === 'pcm')
      expect((await audio.streamingTranscriptionUpdates()).length).toBeGreaterThan(0)

    const turn = await audio.waitForTurn({ timeout: 90_000 })
    expect(turn.chat.messages.map(message => message.role)).toEqual(['user', 'assistant'])
    expect(turn.chat.messages[0].text).toMatch(/say hello/i)
    expect(turn.chat.messages[1].text.length).toBeGreaterThan(0)
    expect(turn.llm.inputMessages.some(message => message.role === 'user')).toBe(true)
    expect(turn.llm.outputChunks.length).toBeGreaterThan(0)
    expect(turn.llm.outputCharacters).toBeGreaterThan(0)
    expect(turn.tts.audioSegments.length).toBeGreaterThan(0)
    for (const segment of turn.tts.audioSegments) {
      expect(segment.text.length).toBeGreaterThan(0)
      expect(segment.durationMs).toBeGreaterThan(100)
    }

    const playback = (await audio.completedSpans(IOSpanNames.AudioPlayback))
      .filter(span => span.attributes[IOAttributes.TurnId] === turn.id)
    expect(playback.length).toBeGreaterThan(0)
    for (const span of playback) {
      expect(span.ended).toBe(true)
      expect(span.status.code).toBe(0)
      expect(span.attributes[IOAttributes.TTSInterrupted]).not.toBe(true)
      expect(span.attributes[IOAttributes.TTSCanceled]).not.toBe(true)
      expect(Number(span.endTimeNano) - Number(span.startTimeNano)).toBeGreaterThan(100_000_000)
    }
    const spans = await audio.completedSpans()
    const llm = spans.find(span => span.name === IOSpanNames.LLMInference && span.attributes[IOAttributes.TurnId] === turn.id)
    const recognition = spans.findLast(span => span.name === IOSpanNames.SpeechRecognition)
    const firstPlayback = playback.toSorted((left, right) => Number(left.startTimeNano) - Number(right.startTimeNano))[0]
    console.info('local_voice_metrics', JSON.stringify({
      turnId: turn.id,
      llmMs: llm ? (Number(llm.endTimeNano) - Number(llm.startTimeNano)) / 1e6 : undefined,
      finalTranscriptToFirstPlaybackMs: recognition && firstPlayback ? (Number(firstPlayback.startTimeNano) - Number(recognition.endTimeNano)) / 1e6 : undefined,
      llmRequestToFirstPlaybackMs: llm && firstPlayback ? (Number(firstPlayback.startTimeNano) - Number(llm.startTimeNano)) / 1e6 : undefined,
      audioMs: turn.tts.audioSegments.reduce((total, segment) => total + segment.durationMs, 0),
      playedClips: playback.length,
    }))
  })
})
