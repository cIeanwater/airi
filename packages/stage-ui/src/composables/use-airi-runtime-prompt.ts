import { useLive2dParams } from '@proj-airi/stage-ui-live2d/stores'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { EMOTION_EmotionMotionName_value, EMOTION_VALUES } from '../constants/emotions'
import { useSettingsStageModel } from '../stores/settings/stage-model'

const RUNTIME_PROMPT_KEYS = [
  'base.prompt.emotion',
  'base.prompt.emoji',
  'base.prompt.suffix',
]

/** Returns the localized emotion and emoji prompt for each model request. */
export function useAiriRuntimePrompt() {
  const { locale, t, te } = useI18n()
  const stage = useSettingsStageModel()
  const live2d = useLive2dParams()

  return computed(() => {
    if (!RUNTIME_PROMPT_KEYS.every(key => te(key, locale.value)))
      return ''

    return [
      t('base.prompt.emotion'),
      EMOTION_VALUES
        .map(emotion => `- ${emotion} (Emotion for feeling ${EMOTION_EmotionMotionName_value[emotion]})`)
        .join('\n'),
      t('base.prompt.suffix'),
      t('base.prompt.emoji'),
      // Motion names address authored model assets. Free-form cues cannot create animations.
      `ACT motion must match one of these loaded stage motion groups exactly: ${JSON.stringify(stage.stageModelRenderer === 'live2d' ? [...new Set(live2d.availableMotions.map(motion => motion.motionName))] : [])}. Omit motion when no group fits. Never invent a motion name. Emotion remains available independently.`,
    ].join('\n\n')
  })
}
