import { describe, expect, it } from '../../src'
import { configureModuleHearing, configureOnboarding } from '../shared/configurations'
import { configureStorage } from '../shared/configurations/storage'
import { openaiAsr } from '../shared/providers'

describe('local audio reception', () => {
  // This synthetic recording supplies warm-up speech, a greeting, and trailing silence.
  // Chromium replaces the microphone, while AIRI's VAD and the local ASR run unchanged.
  it('captures a file microphone, recognizes speech locally, and creates a voice draft', {
    input: new URL('../single-utterance-pipeline/input.test.wav', import.meta.url),
    preflight: [
      configureOnboarding(() => ({ completed: true })),
      configureModuleHearing(async (context) => {
        const baseUrl = context.env.TESTING_AUDIO_LOCAL_ASR_BASE_URL
        context.skip(!baseUrl, 'Set TESTING_AUDIO_LOCAL_ASR_BASE_URL to run the local ASR case.')
        if (!baseUrl)
          return undefined
        if (!['127.0.0.1', 'localhost', '[::1]'].includes(new URL(baseUrl).hostname))
          throw new Error('The local ASR case requires a loopback endpoint.')

        const provider = openaiAsr({
          apiKey: context.env.TESTING_AUDIO_LOCAL_ASR_API_KEY ?? 'local-test',
          baseUrl,
          model: context.env.TESTING_AUDIO_LOCAL_ASR_MODEL ?? 'Systran/faster-whisper-small',
          provider: 'openai-compatible-audio-transcription',
        })
        provider.config.language = 'en'
        return { provider, captureFormat: 'wav', autoSend: false, microphoneEnabled: true }
      }),
      async ({ runtime }) => configureStorage(runtime, {
        // Isolate reception from optional LLM rewriting, turn judging, and TTS.
        'settings/hearing/rephrase-enabled': 'false',
        'settings/hearing/end-judge-enabled': 'false',
        'settings/hearing/input-mode': 'always-on',
        'settings/language': 'en',
      }),
    ],
  }, async ({ audio }) => {
    await audio.waitForVoiceInput({ timeout: 90_000 })
    await expect(audio).toHaveCapturedTranscriptionAudio({ count: 1, minimumBytes: 8000 })
    await expect(audio).toHaveTranscriptions([
      ['Please say hello.', 'Say hello.'],
    ], { match: 'contains' })
    const draft = audio.runtimePage.getByRole('region', { name: 'Voice draft', includeHidden: true })
    await draft.waitFor({ state: 'attached', timeout: 30_000 })
    await expect.poll(() => draft.getByRole('textbox').inputValue(), { timeout: 30_000 }).toMatch(/say hello/i)
  })
})
