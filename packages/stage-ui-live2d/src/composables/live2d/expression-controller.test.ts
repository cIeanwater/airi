import type { PixiLive2DInternalModel } from './motion-manager'

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ref, shallowRef } from 'vue'

import { useExpressionStore } from '../../stores/expression-store'
import { useExpressionController } from './expression-controller'

describe('useExpressionController', () => {
  afterEach(() => vi.useRealTimers())

  it('shows an emotion on Hiyori without motion groups or exp3 files, then releases it', async () => {
    // ROOT CAUSE:
    //
    // The stage only requested a Happy motion group. Hiyori has no such group
    // and no exp3 expressions, so a parsed emotion produced no visible change.
    // We apply existing face parameters after motion updates, then release them.
    setActivePinia(createPinia())
    vi.useFakeTimers()
    const ids = ['ParamMouthForm', 'ParamEyeLSmile', 'ParamEyeRSmile', 'ParamCheek', 'ParamEyeLOpen', 'ParamEyeROpen']
    const values = new Map(ids.map(id => [id, id.endsWith('Open') ? 1 : 0]))
    const coreModel = {
      getParameterIndex: (id: string) => ids.includes(id) ? ids.indexOf(id) : ids.length,
      getParameterCount: () => ids.length,
      getParameterDefaultValue: (index: number) => ids[index].endsWith('Open') ? 1 : 0,
      getParameterMinimumValue: (index: number) => ids[index].endsWith('Open') ? 0 : -1,
      getParameterMaximumValue: (index: number) => ids[index] === 'ParamCheek' ? 0 : 1,
      getParameterValueById: (id: string) => values.get(id) ?? 0,
      setParameterValueById: vi.fn((id: string, value: number) => values.set(id, value)),
    }
    const internalModel = shallowRef({ coreModel } as unknown as PixiLive2DInternalModel)
    const controller = useExpressionController({ internalModel, manualEnabled: ref(false) })
    await controller.initialise('hiyori', [], async () => '')

    expect(controller.setEmotion('happy', 0.5)).toBe(true)
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(0.5)
    expect(values.get('ParamEyeLSmile')).toBe(0.5)
    expect(values.get('ParamEyeLOpen')).toBe(0.825)
    expect(values.get('ParamEyeROpen')).toBe(0.825)
    expect(values.get('ParamCheek')).toBe(-0.2)

    // Idle motion changes are overwritten throughout the emotion hold.
    values.set('ParamMouthForm', 0)
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(0.5)

    vi.advanceTimersByTime(3000)
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(0)
    expect(values.get('ParamCheek')).toBe(0)
    expect(values.get('ParamEyeLOpen')).toBe(1)
    expect(coreModel.setParameterValueById).not.toHaveBeenCalledWith('ParamBrowLY', expect.anything())

    controller.setEmotion('happy')
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(1)
    expect(values.get('ParamEyeLOpen')).toBe(0.65)
    controller.setEmotion('neutral')
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(0)
    expect(values.get('ParamEyeLOpen')).toBe(1)
    expect(controller.emotionActive.value).toBe(false)

    controller.setEmotion('happy')
    controller.applyExpressions(internalModel.value.coreModel)
    controller.setEmotion('happy', 0)
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(0)

    // The SDK exposes defaults by index. Reading the current happy value as
    // a default kept later emotions smiling and prevented neutral from resetting.
    controller.setEmotion('happy')
    controller.applyExpressions(internalModel.value.coreModel)
    controller.setEmotion('angry', 0.3)
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(-0.3)
    expect(values.get('ParamEyeLSmile')).toBe(0)
    controller.setEmotion('neutral')
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(0)

    // Replacing an emotion must cancel its old release timer.
    controller.setEmotion('happy')
    vi.advanceTimersByTime(2000)
    controller.setEmotion('happy', 0.8)
    vi.advanceTimersByTime(1000)
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(0.8)
    controller.dispose()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('uses the authored expression targets and blend modes without changing manual values', async () => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    const ids = ['ParamMouthForm', 'ParamCheek', 'ParamEyeLOpen']
    const values = new Map<string, number>([['ParamMouthForm', 0], ['ParamCheek', 0], ['ParamEyeLOpen', 1]])
    const coreModel = {
      getParameterIndex: (id: string) => ids.indexOf(id),
      getParameterCount: () => ids.length,
      getParameterDefaultValue: (index: number) => ids[index] === 'ParamEyeLOpen' ? 1 : 0,
      getParameterMinimumValue: () => -1,
      getParameterMaximumValue: () => 1,
      getParameterValueById: (id: string) => values.get(id) ?? 0,
      setParameterValueById: (id: string, value: number) => values.set(id, value),
    }
    const internalModel = shallowRef({ coreModel } as unknown as PixiLive2DInternalModel)
    const controller = useExpressionController({ internalModel, manualEnabled: ref(false) })
    await controller.initialise('custom', [{ Name: 'HAPPY', File: 'happy.exp3.json' }], async () => JSON.stringify({
      Type: 'Live2D Expression',
      Parameters: [
        { Id: 'ParamMouthForm', Value: -0.8, Blend: 'Overwrite' },
        { Id: 'ParamCheek', Value: 0.6, Blend: 'Add' },
        { Id: 'ParamEyeLOpen', Value: 0, Blend: 'Multiply' },
      ],
    }))
    const store = useExpressionStore()
    store.set('ParamCheek', 0.1)

    controller.setEmotion('happy', 0.5)
    controller.applyExpressions(internalModel.value.coreModel)
    expect(values.get('ParamMouthForm')).toBe(-0.4)
    expect(values.get('ParamCheek')).toBe(0.3)
    expect(values.get('ParamEyeLOpen')).toBe(0.5)
    expect(store.expressions.get('ParamCheek')?.currentValue).toBe(0.1)
    controller.dispose()
  })

  it('registers expressions with the identity of each loaded model', async () => {
    // ROOT CAUSE:
    //
    // Model.vue creates one expression controller, then reuses it when the
    // component loads another model. The controller captured the first model
    // ID at setup time, so model B expressions were registered as model A.
    //
    // We fixed this by passing the identity of each completed model load into
    // the matching expression initialization.
    setActivePinia(createPinia())

    const internalModel = shallowRef({
      coreModel: {
        getParameterIndex: () => 0,
        getParameterCount: () => 1,
        getParameterDefaultValue: () => 0,
      },
    } as unknown as PixiLive2DInternalModel)
    const controller = useExpressionController({ internalModel })
    const expressionStore = useExpressionStore()
    const expressionRefs = [{ Name: 'Happy', File: 'happy.exp3.json' }]
    const readExpFile = async () => JSON.stringify({
      Type: 'Live2D Expression',
      Parameters: [{ Id: 'ParamMouthForm', Value: 1, Blend: 'Add' }],
    })

    await controller.initialise('model-a', expressionRefs, readExpFile)
    expect(expressionStore.modelId).toBe('model-a')

    await controller.initialise('model-b', expressionRefs, readExpFile)
    expect(expressionStore.modelId).toBe('model-b')
  })
})
