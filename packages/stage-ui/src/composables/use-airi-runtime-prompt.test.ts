import { useLive2dParams } from '@proj-airi/stage-ui-live2d/stores'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useSettingsStageModel } from '../stores/settings/stage-model'
import { useAiriRuntimePrompt } from './use-airi-runtime-prompt'

const i18nMock = vi.hoisted(() => ({
  hasTranslation: vi.fn<(key: string, locale: string) => boolean>(),
  locale: { value: 'en' },
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({
    locale: i18nMock.locale,
    t: (key: string) => key,
    te: (key: string, currentLocale: string) => i18nMock.hasTranslation(key, currentLocale),
  }),
}))

describe('useAiriRuntimePrompt', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('returns no prompt for a locale that still uses the combined prompt', () => {
    i18nMock.hasTranslation.mockReturnValue(false)

    expect(useAiriRuntimePrompt().value).toBe('')
  })

  it('assembles the emotion and emoji prompt for a split locale', () => {
    i18nMock.hasTranslation.mockReturnValue(true)

    const prompt = useAiriRuntimePrompt().value

    expect(prompt).toContain('base.prompt.emotion')
    expect(prompt).toContain('base.prompt.suffix')
    expect(prompt).toContain('base.prompt.emoji')
  })

  it('restricts action cues to the loaded Live2D motion groups and reacts to a renderer change', () => {
    i18nMock.hasTranslation.mockReturnValue(true)
    const stage = useSettingsStageModel()
    stage.stageModelRenderer = 'live2d'
    useLive2dParams().availableMotions = [
      { motionName: 'Idle', motionIndex: 0, fileName: 'idle-01.motion3.json' },
      { motionName: 'Idle', motionIndex: 1, fileName: 'idle-02.motion3.json' },
      { motionName: 'Tap', motionIndex: 0, fileName: 'tap.motion3.json' },
    ]
    const prompt = useAiriRuntimePrompt()

    expect(prompt.value).toContain('["Idle","Tap"]')
    expect(prompt.value).toContain('Never invent a motion name')
    stage.stageModelRenderer = 'vrm'
    expect(prompt.value).toContain('motion groups exactly: []')
    expect(prompt.value).not.toContain('["Idle","Tap"]')
  })
})
