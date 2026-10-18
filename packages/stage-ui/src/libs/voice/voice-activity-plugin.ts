import type { TurnRef, VoicePlugin } from '@proj-airi/core-agent'
import type { AudioWindow } from '@proj-airi/pipelines-audio'

/** Application policy chooses the destination. The model only supplies scores for ordered windows. */
export interface VoiceActivityOptions {
  readonly detect: (window: AudioWindow, signal: AbortSignal) => Promise<number>
  readonly target: () => { sessionId: string, interruptTurns: readonly TurnRef[] } | undefined
  /**
   * External KWS adapters resolve pronunciation ownership and the character session before returning a wake signal.
   * `activity` is the VAD result of the same window, before an active input lowers the threshold. An adapter that
   * reads another input uses it to find pauses.
   */
  readonly detectWakeWord?: (window: AudioWindow, signal: AbortSignal, activity: { speech: boolean, durationMs: number }) => Promise<{ sessionId: string, interruptTurns: readonly TurnRef[] } | undefined>
  /** Echo rejection runs after detection. Detectors remain active while the assistant speaks. */
  readonly acceptSpeech?: (window: AudioWindow, signal: AbortSignal) => Promise<boolean>
  readonly enabled?: () => boolean
  readonly onInput?: () => void
  /** @default 0.6. A live input uses 30 percent of this score to keep speech active. */
  readonly threshold?: number
  /** @default 300. Shorter speech is discarded when silence ends an input. */
  readonly minSpeechMs?: number
  /** @default 96. A response stops only after this much continuous speech. Wake words already confirm intent. */
  readonly bargeInSpeechMs?: number
  /**
   * This much silence ends an accepted input. A function is read on each window, so a setting change applies at once.
   *
   * @default 1200
   */
  readonly silenceMs?: number | (() => number)
  /** @default 360. Speech onset retains this much audio before the detected window. */
  readonly preRollMs?: number
}

/** Converts VAD scores to ordinary input commands. It owns no recorder, source, transcription request, or playback nodes. */
export function createVoiceActivityPlugin(options: VoiceActivityOptions): VoicePlugin {
  const preRollMs = options.preRollMs ?? 360
  const bargeInSpeechMs = options.bargeInSpeechMs ?? 96
  return { name: 'voice-activity', setup(scope) {
    let inputId: string | undefined
    let speechMs = 0
    let silenceMs = 0
    let onset: { target: { sessionId: string, interruptTurns: readonly TurnRef[] }, range: AudioWindow['range'], speechMs: number } | undefined
    let captureFloor: { sourceId: string, frame: number } | undefined

    // NOTICE:
    // SileroVad requires 512 samples at 16 kHz, so each window lasts 32 ms.
    // The constraint is in workers/vad/silero-vad.ts.
    // Remove this fixed size when detectors supply their own window requirements.
    // Confirmation consumes additional windows. Retain their duration too, so capture can still replay the original onset's full pre-roll.
    scope.observeAudio({ windowMs: 32, hopMs: 32, scheduling: 'ordered', preRollMs: preRollMs + bargeInSpeechMs }, async (ctx) => {
      const score = await options.detect(ctx.window, ctx.signal)
      const duration = (ctx.window.range.endFrame - ctx.window.range.startFrame) * 1000 / ctx.window.sampleRate
      const wake = await options.detectWakeWord?.(ctx.window, ctx.signal, { speech: score >= (options.threshold ?? 0.6), durationMs: duration })
      ctx.signal.throwIfAborted()

      if (ctx.window.discontinuity) {
        inputId = undefined
        speechMs = 0
        silenceMs = 0
        onset = undefined
        captureFloor = { sourceId: ctx.window.range.sourceId, frame: ctx.window.range.startFrame }
      }

      let input = ctx.controls.activeInput()

      // A manual control owns its interval until release. VAD continues inference without ending that input.
      if (input && input.id !== inputId) {
        onset = undefined
        return
      }

      if (options.enabled && !options.enabled()) {
        input?.cancel('Automatic speech input is paused')
        inputId = undefined
        onset = undefined
        return
      }

      const speech = score >= (input ? (options.threshold ?? 0.6) * 0.3 : options.threshold ?? 0.6)
      if (!speech)
        onset = undefined

      let activityRange = ctx.window.range
      let activityDuration = duration
      if ((wake && wake.sessionId !== input?.sessionId) || (!input && speech)) {
        if (options.acceptSpeech && !await options.acceptSpeech(ctx.window, ctx.signal)) {
          onset = undefined
          return
        }

        ctx.signal.throwIfAborted()
        const target = wake ?? options.target()
        if (!target) {
          onset = undefined
          return
        }

        if (!wake && target.interruptTurns.length) {
          const sameTarget = onset?.target.sessionId === target.sessionId
            && onset.target.interruptTurns.length === target.interruptTurns.length
            && onset.target.interruptTurns.every((turn, index) => turn.turnId === target.interruptTurns[index].turnId && turn.sessionId === target.interruptTurns[index].sessionId)
          const contiguous = onset?.range.sourceId === ctx.window.range.sourceId && onset.range.endFrame === ctx.window.range.startFrame
          if (onset && sameTarget && contiguous)
            onset = { target, range: { ...onset.range, endFrame: ctx.window.range.endFrame }, speechMs: onset.speechMs + duration }
          else
            onset = { target, range: ctx.window.range, speechMs: duration }

          if (onset.speechMs < bargeInSpeechMs)
            return

          // Confirmation delays admission. The original onset keeps the first phoneme in capture and end detection.
          activityRange = onset.range
          activityDuration = onset.speechMs
        }

        // A source gap discards retained history. Pre-roll cannot request samples before the new continuous interval.
        const retainedPreRollMs = captureFloor?.sourceId === activityRange.sourceId
          ? Math.min(preRollMs, (activityRange.startFrame - captureFloor.frame) * 1000 / ctx.window.sampleRate)
          : preRollMs
        const result = ctx.controls.beginInput({ ...target, start: wake
          ? { kind: 'after-silence' }
          : { kind: 'speech-onset', at: { sourceId: activityRange.sourceId, frame: activityRange.startFrame }, preRollMs: retainedPreRollMs } })
        onset = undefined
        if (result.status !== 'started')
          return

        input = result.input
        inputId = input.id
        speechMs = 0
        silenceMs = 0
        options.onInput?.()
      }

      if (!input)
        return

      if (!input.noteActivity({ range: activityRange, speech }))
        return

      speechMs += speech ? activityDuration : 0
      silenceMs = speech ? 0 : silenceMs + duration
      const silenceLimit = typeof options.silenceMs === 'function' ? options.silenceMs() : options.silenceMs ?? 1200
      if (silenceMs >= silenceLimit) {
        if (speechMs < (options.minSpeechMs ?? 300))
          input.cancel('Speech segment was too short')
        else
          input.end()

        inputId = undefined
      }
    })

    return undefined
  } }
}
