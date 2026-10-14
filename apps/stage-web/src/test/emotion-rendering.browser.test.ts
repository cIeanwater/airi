import type { PixiLive2DInternalModel } from '@proj-airi/stage-ui-live2d/composables/live2d'

import { Live2DCanvas, Live2DModel } from '@proj-airi/stage-ui-live2d/components/scenes/live2d'
import { useExpressionStore, useLive2dParams } from '@proj-airi/stage-ui-live2d/stores'
import { useDisplayModelsStore } from '@proj-airi/stage-ui/stores/display-models'
import { createPinia } from 'pinia'
import { describe, expect, it } from 'vitest'
import { page } from 'vitest/browser'
import { createApp, defineComponent, h, shallowRef } from 'vue'

import '@proj-airi/stage-ui-live2d/utils/live2d-zip-loader'
import '@proj-airi/stage-ui-live2d/utils/live2d-opfs-registration'

type PixiApplication = NonNullable<InstanceType<typeof Live2DModel>['$props']['app']>

function isLive2DRig(value: object): value is { internalModel: PixiLive2DInternalModel } {
  return 'internalModel' in value
}

describe('chat emotions on the real Hiyori rig', () => {
  it.each([
    { id: 'preset-live2d-1', label: 'pro', cheekDirection: 1 },
    { id: 'preset-live2d-2', label: 'free', cheekDirection: -1 },
  ])('shows intensity and restores Hiyori $label without exp3 files', async ({ id, label, cheekDirection }) => {
    // ROOT CAUSE:
    //
    // Hiyori has no Happy or Angry motion groups and no exp3 expressions.
    // Selecting these missing groups left the rendered face unchanged.
    // Chat now applies supported Cubism face parameters independently of manual settings.
    const pinia = createPinia()
    const preset = await useDisplayModelsStore(pinia).getDisplayModel(id)
    if (preset?.type !== 'url')
      throw new Error(`The Hiyori ${label} test preset is unavailable.`)

    const parameters = useLive2dParams(pinia)
    parameters.resetState()
    const modelComponent = shallowRef<InstanceType<typeof Live2DModel>>()
    const renderErrors: Error[] = []
    let pixiApp: PixiApplication | undefined
    const container = document.createElement('div')
    container.style.width = '800px'
    container.style.height = '600px'
    container.style.background = '#f2e7df'
    document.body.appendChild(container)

    const TestHarness = defineComponent(() => () => h(Live2DCanvas, {
      width: 800,
      height: 600,
      resolution: 1,
      onError: error => renderErrors.push(error),
    }, {
      default: ({ app }: { app: PixiApplication }) => {
        pixiApp = app
        return h(Live2DModel, {
          ref: modelComponent,
          app,
          modelId: preset.id,
          modelSrc: preset.url,
          width: 800,
          height: 600,
          eyeTracking: false,
          live2dIdleAnimationEnabled: false,
          live2dForceIdleEyeAnimation: false,
          live2dAutoBlinkEnabled: false,
          live2dForceAutoBlinkEnabled: false,
          live2dExpressionEnabled: false,
          live2dShadowEnabled: false,
          onError: error => renderErrors.push(error),
        })
      },
    }))

    const app = createApp(TestHarness)
    app.use(pinia)
    app.mount(container)

    // Keep this isolated app mounted until Vitest closes its browser page.
    // Vue DevTools schedules inspector work after unmount and rejects during teardown.
    await expect.poll(() => parameters.availableMotions.length, { timeout: 30_000 }).toBeGreaterThan(0)
    await expect.poll(() => useExpressionStore(pinia).modelId, { timeout: 10_000 }).toBe(preset.id)
    expect(renderErrors).toEqual([])
    expect(useExpressionStore(pinia).expressionGroups.size).toBe(0)
    expect(parameters.availableMotions.some(motion => motion.motionName === 'Happy')).toBe(false)

    const rig = pixiApp?.stage.children.find(child => isLive2DRig(child))
    if (!rig || !isLive2DRig(rig))
      throw new Error('The canvas has no loaded Cubism model.')
    const core = rig.internalModel.coreModel
    const canvas = container.querySelector('canvas')
    if (!(canvas instanceof HTMLCanvasElement))
      throw new Error('The Live2D canvas is unavailable.')
    const api = modelComponent.value
    if (!api)
      throw new Error('The Live2D model component is unavailable.')

    const smileDefault = core.getParameterDefaultValue(core.getParameterIndex('ParamEyeLSmile'))
    const cheekDefault = core.getParameterDefaultValue(core.getParameterIndex('ParamCheek'))
    const mouthDefault = core.getParameterDefaultValue(core.getParameterIndex('ParamMouthForm'))
    const leftEyeDefault = core.getParameterDefaultValue(core.getParameterIndex('ParamEyeLOpen'))
    const rightEyeDefault = core.getParameterDefaultValue(core.getParameterIndex('ParamEyeROpen'))
    const parameter = (name: string) => core.getParameterValueById(name)
    const screenshot = (name: string) => page.screenshot({
      element: canvas,
      path: `../../../../.temp/emotion-rendering/live2d-${label}-${name}.png`,
    })

    await screenshot('neutral')
    await api.setEmotion('happy', 0.3)
    await expect.poll(() => parameter('ParamEyeLSmile')).toBeCloseTo(smileDefault + (1 - smileDefault) * 0.3)
    await expect.poll(() => parameter('ParamEyeLOpen')).toBeCloseTo(leftEyeDefault + (0.65 - leftEyeDefault) * 0.3)
    await expect.poll(() => parameter('ParamEyeROpen')).toBeCloseTo(rightEyeDefault + (0.65 - rightEyeDefault) * 0.3)
    await expect.poll(() => parameter('ParamCheek')).toBeCloseTo(cheekDirection * 0.12)
    await screenshot('happy-03')

    await api.setEmotion('happy', 1)
    await expect.poll(() => parameter('ParamEyeLSmile')).toBeCloseTo(1)
    await expect.poll(() => parameter('ParamEyeRSmile')).toBeCloseTo(1)
    await expect.poll(() => parameter('ParamEyeLOpen')).toBeCloseTo(0.65)
    await expect.poll(() => parameter('ParamEyeROpen')).toBeCloseTo(0.65)
    await expect.poll(() => parameter('ParamCheek')).toBeCloseTo(cheekDirection * 0.4)
    await screenshot('happy-10')

    await api.setEmotion('angry', 0.3)
    await expect.poll(() => parameter('ParamMouthForm')).toBeCloseTo(mouthDefault + (-1 - mouthDefault) * 0.3)
    await expect.poll(() => parameter('ParamEyeLSmile')).toBeCloseTo(smileDefault)
    await expect.poll(() => parameter('ParamEyeLOpen')).toBeCloseTo(leftEyeDefault)
    await expect.poll(() => parameter('ParamEyeROpen')).toBeCloseTo(rightEyeDefault)
    await expect.poll(() => parameter('ParamCheek')).toBeCloseTo(cheekDefault)
    await screenshot('angry-03')

    await api.setEmotion('neutral')
    await expect.poll(() => parameter('ParamMouthForm')).toBeCloseTo(mouthDefault)
    await expect.poll(() => parameter('ParamEyeLSmile')).toBeCloseTo(smileDefault)
    await expect.poll(() => parameter('ParamEyeLOpen')).toBeCloseTo(leftEyeDefault)
    await expect.poll(() => parameter('ParamEyeROpen')).toBeCloseTo(rightEyeDefault)
    await screenshot('restored')

    await api.setEmotion('happy', 0.8)
    await expect.poll(() => parameter('ParamEyeLSmile')).toBeCloseTo(smileDefault + (1 - smileDefault) * 0.8)
    await expect.poll(() => parameter('ParamEyeLSmile'), { timeout: 6_000 }).toBeCloseTo(smileDefault)
    await expect.poll(() => parameter('ParamEyeLOpen')).toBeCloseTo(leftEyeDefault)
    await expect.poll(() => parameter('ParamEyeROpen')).toBeCloseTo(rightEyeDefault)
    await expect.poll(() => parameter('ParamCheek')).toBeCloseTo(cheekDefault)
    expect(renderErrors).toEqual([])
  }, 60_000)
})
