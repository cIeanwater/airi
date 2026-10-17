import type { VoicePlugin } from '@proj-airi/core-agent'

/** Audio-native endpoint detection runs during pauses, before batch transcription has any text. */
export interface VoiceAudioEndOptions {
  enabled: () => boolean
  judge: (audio: Float32Array, sampleRate: number, signal: AbortSignal) => Promise<boolean>
  /** @default 600. VAD silence required before one judgement per speech revision. */
  pauseMs?: number
  onError?: (error: unknown) => void
}

/**
 * Retains only the current turn's last eight seconds. New speech aborts an old judgement immediately.
 * Failed and incomplete judgements leave the input open for speech or the activity plugin's safety limit.
 */
export function createVoiceAudioEndPlugin(options: VoiceAudioEndOptions): VoicePlugin {
  return { name: 'voice-audio-end', setup(scope) {
    scope.onSpeechInput((input) => {
      if (!options.enabled())
        return undefined

      input.task({ name: 'audio-end', selection: { transcript: 'raw' } }, async (ctx) => {
        const control = ctx.controls.activeInput()
        if (control?.id !== input.inputId)
          return

        let chunks: Float32Array[] = []
        let samples = 0
        let speechRevision = 0
        let pauseStart = 0
        let current: { abort: AbortController, complete?: boolean } | undefined

        const installed = control.detectEnd({ windowMs: 100, hopMs: 100, activity: 'ordered' }, async (evidence, signal) => {
          const { audio } = evidence
          if (audio.discontinuity) {
            chunks = []
            samples = 0
            current?.abort.abort('Audio source changed')
            current = undefined
          }

          const mono = new Float32Array(audio.channels[0].length)
          for (const channel of audio.channels) {
            for (let index = 0; index < mono.length; index++)
              mono[index] += channel[index] / audio.channels.length
          }
          chunks.push(mono)
          samples += mono.length
          const limit = audio.sampleRate * 8
          while (chunks.length > 0 && samples - chunks[0].length >= limit)
            samples -= chunks.shift()!.length

          if (evidence.speechRevision !== speechRevision) {
            speechRevision = evidence.speechRevision
            pauseStart = audio.range.endFrame
            current?.abort.abort('Speaker continued')
            current = undefined
          }

          const pausedMs = (audio.range.endFrame - pauseStart) * 1000 / audio.sampleRate
          // Semantic completion must not bypass the activity plugin's minimum utterance duration.
          if (evidence.speechFrames * 1000 / audio.sampleRate < 300 || pausedMs < (options.pauseMs ?? 600))
            return 'continue'

          if (!current) {
            const recording = new Float32Array(Math.min(samples, limit))
            let remaining = recording.length
            for (let index = chunks.length - 1; index >= 0 && remaining > 0; index--) {
              const part = chunks[index].subarray(Math.max(0, chunks[index].length - remaining))
              remaining -= part.length
              recording.set(part, remaining)
            }
            const judgement = { abort: new AbortController(), complete: false }
            current = judgement
            void options.judge(recording, audio.sampleRate, AbortSignal.any([signal, judgement.abort.signal]))
              .then(complete => judgement.complete = complete)
              .catch((error: unknown) => {
                if (!judgement.abort.signal.aborted && !signal.aborted)
                  options.onError?.(error)
              })
          }
          return current.complete ? 'end' : 'continue'
        })
        if (installed.status !== 'installed')
          return
        await installed.observer.done
        current?.abort.abort('Speech input ended')
      })
      return undefined
    })
    return undefined
  } }
}
