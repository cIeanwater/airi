import { Emotion, EMOTION_EmotionMotionName_value } from '../../constants/emotions'

/** A motion discovered from the loaded model, with its original group and index. */
export interface EmotionMotion {
  motionName: string
  motionIndex: number
  fileName: string
}

/** Standard Cubism face targets for models without named emotion expressions. */
export const emotionFaceTargets: Record<Emotion, Readonly<Record<string, number>>> = {
  // Hiyori's smile shape is visible when its eyelids narrow. Keep some eye
  // opening at full intensity so gaze and procedural blinking remain visible.
  [Emotion.Happy]: { ParamMouthForm: 1, ParamEyeLSmile: 1, ParamEyeRSmile: 1, ParamEyeLOpen: 0.65, ParamEyeROpen: 0.65, ParamCheek: 0.4 },
  [Emotion.Sad]: { ParamMouthForm: -0.7, ParamBrowLY: -0.4, ParamBrowRY: -0.4, ParamBrowLForm: 0.8, ParamBrowRForm: 0.8, ParamEyeLOpen: 0.65, ParamEyeROpen: 0.65 },
  [Emotion.Angry]: { ParamMouthForm: -1, ParamBrowLAngle: -0.7, ParamBrowRAngle: -0.7, ParamBrowLForm: -1, ParamBrowRForm: -1 },
  [Emotion.Think]: { ParamMouthForm: -0.15, ParamBrowLY: 0.2, ParamBrowRY: -0.2, ParamEyeLOpen: 0.75, ParamEyeROpen: 0.75 },
  [Emotion.Surprise]: { ParamMouthForm: -0.2, ParamBrowLY: 0.6, ParamBrowRY: 0.6, ParamEyeLOpen: 1, ParamEyeROpen: 1 },
  [Emotion.Awkward]: { ParamMouthForm: 0.2, ParamCheek: 0.6, ParamBrowLY: 0.3, ParamBrowRY: -0.3 },
  [Emotion.Question]: { ParamMouthForm: -0.2, ParamBrowLY: 0.6, ParamBrowRY: -0.2 },
  [Emotion.Curious]: { ParamMouthForm: 0.3, ParamBrowLY: 0.4, ParamBrowRY: 0.4 },
  [Emotion.Neutral]: {},
}

/**
 * Normalizes face and motion intensity to the same range. Non-finite values use full intensity.
 * @example
 * normalizeEmotionIntensity(2)
 * // => 1
 */
export function normalizeEmotionIntensity(intensity: number): number {
  return Number.isFinite(intensity) ? Math.max(0, Math.min(1, intensity)) : 1
}

/**
 * Resolves an emotion to a motion that exists in the loaded model.
 * Explicit file mappings take priority over matching group names. Missing motions stay absent.
 */
export function resolveEmotionMotion(
  emotion: Emotion,
  motions: EmotionMotion[],
  motionMap: Record<string, string>,
  intensity = 1,
): { group: string, index: number } | undefined {
  // Neutral can restore Idle even at zero intensity. Other zero-weight emotions
  // release their face overlay without starting a new body motion.
  if (emotion !== Emotion.Neutral && normalizeEmotionIntensity(intensity) === 0)
    return undefined
  const group = EMOTION_EmotionMotionName_value[emotion].toLowerCase()
  const matches = (value: string) => value.toLowerCase() === emotion || value.toLowerCase() === group
  const mapped = motions.find(motion => motionMap[motion.fileName] && matches(motionMap[motion.fileName]))
  const motion = mapped ?? motions.find(motion => matches(motion.motionName))
  if (!motion)
    return undefined
  return { group: motion.motionName, index: motion.motionIndex }
}
