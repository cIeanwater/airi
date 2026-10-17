import type { Ref } from 'vue'

import type { ExpressionBlendMode, ExpressionEntry, ExpressionGroupDefinition } from '../../stores/expression-store'
import type { PixiLive2DInternalModel } from './motion-manager'

import { ref } from 'vue'

import { Emotion, EMOTION_EmotionMotionName_value, EMOTION_VALUES } from '../../constants/emotions'
import { useExpressionStore } from '../../stores/expression-store'
import { emotionFaceTargets, normalizeEmotionIntensity } from './emotion'

// ---------------------------------------------------------------------------
// Types for model3.json / exp3.json data
// ---------------------------------------------------------------------------

/** A single expression reference inside model3.json FileReferences.Expressions[]. */
interface Model3ExpressionRef {
  Name: string
  File: string
}

/** Parameter entry inside an exp3.json file. */
interface Exp3Parameter {
  Id: string
  Value: number
  Blend: 'Add' | 'Multiply' | 'Overwrite'
}

/** Root structure of an exp3.json file. */
interface Exp3Json {
  Type: string
  Parameters: Exp3Parameter[]
  // FadeInTime / FadeOutTime are intentionally ignored (we do direct application).
}

// ---------------------------------------------------------------------------
// Controller
// ---------------------------------------------------------------------------

export interface ExpressionControllerOptions {
  /**
   * The loaded Live2D internal model reference (reactive so it can be null
   * while a model is being swapped).
   */
  internalModel: Ref<PixiLive2DInternalModel | undefined>
  /**
   * Applies manual expression values from the store. Chat emotions remain active independently.
   * @default true
   */
  manualEnabled?: Ref<boolean>
}

/**
 * Create an expression controller that:
 * 1. Parses exp3 data from the model settings
 * 2. Registers entries into the Pinia expression store
 * 3. Provides an `applyExpressions()` function to be called every frame
 */
export function useExpressionController(options: ExpressionControllerOptions) {
  const store = useExpressionStore()

  // Track which parameter IDs were written in the previous frame so we can
  // detect active→inactive transitions and explicitly reset them.
  const activeLastFrame = new Set<string>()
  // Chat emotions are transient overlays. Manual settings and saved defaults
  // stay in the store. A new emotion, neutral, reload, or disposal releases this overlay.
  const emotionParameters = new Map<string, ExpressionEntry>()
  const emotionActive = ref(false)
  let emotionResetTimer: ReturnType<typeof setTimeout> | undefined

  /**
   * Applies a named model expression, or standard face parameters when no expression matches.
   * The overlay yields after the duration and never changes saved expression defaults.
   */
  function setEmotion(name: string, intensity = 1, duration = 3000): boolean {
    const emotion = EMOTION_VALUES.find(value => value === name.toLowerCase())
    const coreModel = options.internalModel.value?.coreModel
    if (!emotion || !coreModel)
      return false

    if (emotionResetTimer !== undefined)
      clearTimeout(emotionResetTimer)
    emotionResetTimer = undefined
    emotionParameters.clear()
    emotionActive.value = false
    const weight = normalizeEmotionIntensity(intensity)
    if (emotion === Emotion.Neutral || weight === 0)
      return true

    const groupName = EMOTION_EmotionMotionName_value[emotion].toLowerCase()
    const group = [...store.expressionGroups.values()].find(group =>
      group.name.toLowerCase() === emotion || group.name.toLowerCase() === groupName,
    )
    // Authored expressions carry the model's intended appearance. Standard
    // Cubism parameters cover models such as Hiyori, which supplies no exp3 files.
    const parameters = group?.parameters ?? Object.entries(emotionFaceTargets[emotion]).map(([parameterId, value]) => ({
      parameterId,
      value,
      blend: 'Overwrite' as const,
    }))
    for (const parameter of parameters) {
      const index = coreModel.getParameterIndex(parameter.parameterId)
      // Cubism creates virtual indices for unknown IDs. They cannot alter the rig.
      if (index < 0 || index >= coreModel.getParameterCount())
        continue

      const modelDefault = getModelParameterDefault(parameter.parameterId)
      let identity = modelDefault
      if (parameter.blend === 'Add')
        identity = 0
      else if (parameter.blend === 'Multiply')
        identity = 1
      let targetValue = parameter.value
      if (!group && parameter.parameterId === 'ParamCheek') {
        // Hiyori Free uses negative blush values, while Pro uses positive values.
        // Choose the available direction from the rig default.
        const maximum = coreModel.getParameterMaximumValue(index)
        const endpoint = maximum > modelDefault ? maximum : coreModel.getParameterMinimumValue(index)
        targetValue = modelDefault + (endpoint - modelDefault) * parameter.value
      }
      let value = identity + (targetValue - identity) * weight
      if (parameter.blend === 'Overwrite') {
        value = Math.max(coreModel.getParameterMinimumValue(index), Math.min(coreModel.getParameterMaximumValue(index), value))
      }
      emotionParameters.set(parameter.parameterId, {
        name: parameter.parameterId,
        parameterId: parameter.parameterId,
        blend: parameter.blend,
        modelDefault,
        defaultValue: identity,
        currentValue: value,
        targetValue,
      })
    }

    if (emotionParameters.size === 0)
      return false
    emotionActive.value = true
    emotionResetTimer = setTimeout(() => {
      emotionParameters.clear()
      emotionActive.value = false
      emotionResetTimer = undefined
    }, duration)
    return true
  }

  // ---- Initialisation (called once after model load) ----------------------

  /**
   * Parse model3.json expression references and the corresponding exp3 data,
   * then register everything in the store.
   *
   * @param modelId        - The identifier captured for this model load. It
   *                         scopes expression persistence to the loaded model.
   * @param expressionRefs - `FileReferences.Expressions` from model3.json
   * @param readExpFile    - An async function that reads the content of an
   *                         exp3.json file given its path (relative to the
   *                         model root inside the ZIP / OPFS).
   */
  async function initialise(
    modelId: string | undefined,
    expressionRefs: Model3ExpressionRef[],
    readExpFile: (path: string) => Promise<string>,
  ) {
    dispose()
    const groups: ExpressionGroupDefinition[] = []
    const entryMap = new Map<string, ExpressionEntry>()

    for (const expRef of expressionRefs) {
      try {
        const raw = await readExpFile(expRef.File)
        const exp3: Exp3Json = JSON.parse(raw)

        const groupParams: ExpressionGroupDefinition['parameters'] = []

        for (const param of exp3.Parameters) {
          const blend = normaliseBlend(param.Blend)

          groupParams.push({
            parameterId: param.Id,
            blend,
            value: param.Value,
          })

          // Only create the entry once per parameterId (first-come basis for
          // modelDefault; the store handles last-write-wins at runtime).
          if (!entryMap.has(param.Id)) {
            const modelDefault = getModelParameterDefault(param.Id)
            entryMap.set(param.Id, {
              name: param.Id,
              parameterId: param.Id,
              blend,
              currentValue: modelDefault,
              defaultValue: modelDefault,
              modelDefault,
              targetValue: param.Value,
            })
          }
          else if (param.Value !== 0) {
            // Update targetValue if this group has a non-zero value
            // (prefer non-zero over zero as the "intended activation value")
            const existing = entryMap.get(param.Id)!
            if (existing.targetValue === 0) {
              existing.targetValue = param.Value
            }
          }
        }

        groups.push({ name: expRef.Name, parameters: groupParams })
      }
      catch (err) {
        console.warn(`[expression-controller] Failed to parse exp3 for "${expRef.Name}" (${expRef.File}):`, err)
      }
    }

    store.registerExpressions(
      modelId ?? 'unknown',
      groups,
      Array.from(entryMap.values()),
    )
  }

  // ---- Per-frame application -----------------------------------------------

  /**
   * Apply all expression entries from the store onto the Live2D model.
   *
   * Two key mechanisms:
   *
   * 1. **Noop detection**: entries whose currentValue is the blend-mode
   *    identity (Add:0, Multiply:1, Overwrite:modelDefault) are skipped.
   *
   * 2. **Transition reset**: when an entry was active last frame but is
   *    noop this frame, we explicitly write modelDefault to clear the
   *    stale value (expression-only params are not reset by motion).
   *
   * Multiply blend reads the current frame parameter value (post-blink)
   * so auto-blink modulation is naturally preserved.
   *
   * @param coreModel - The Cubism core model (coreModel from internalModel).
   */
  function applyExpressions(coreModel: PixiLive2DInternalModel['coreModel']) {
    const activeThisFrame = new Set<string>()

    for (const entry of store.expressions.values()) {
      if (options.manualEnabled?.value === false)
        break
      if (isNoopValue(entry))
        continue

      const blendedValue = computeTargetValue(entry, coreModel)

      coreModel.setParameterValueById(entry.parameterId, blendedValue)
      activeThisFrame.add(entry.parameterId)
    }

    for (const entry of emotionParameters.values()) {
      coreModel.setParameterValueById(entry.parameterId, computeTargetValue(entry, coreModel))
      activeThisFrame.add(entry.parameterId)
    }

    // Reset parameters that were active last frame but not this frame.
    // This handles the active→inactive transition (e.g. toggle OFF).
    for (const paramId of activeLastFrame) {
      if (!activeThisFrame.has(paramId)) {
        const entry = findEntryByParameterId(paramId)
        coreModel.setParameterValueById(paramId, entry?.modelDefault ?? getModelParameterDefault(paramId))
      }
    }

    activeLastFrame.clear()
    for (const id of activeThisFrame)
      activeLastFrame.add(id)
  }

  /**
   * Does this entry's currentValue produce no visual effect when blended?
   * - Add: adding 0 changes nothing.
   * - Multiply: multiplying by 1 changes nothing.
   * - Overwrite: writing modelDefault changes nothing.
   */
  function isNoopValue(entry: ExpressionEntry): boolean {
    switch (entry.blend) {
      case 'Add':
        return entry.currentValue === 0
      case 'Multiply':
        return entry.currentValue === 1
      default:
        return entry.currentValue === entry.modelDefault
    }
  }

  /**
   * Compute the final blended value for a parameter.
   *
   * - **Add**: modelDefault + currentValue (stable base prevents accumulation
   *   on expression-only parameters not reset by motion each frame).
   * - **Multiply**: reads the CURRENT frame parameter value (post-blink/motion)
   *   and scales it.  This preserves auto-blink modulation because blink/motion
   *   always write a fresh value before the expression plugin runs.
   * - **Overwrite**: direct replacement.
   */
  function computeTargetValue(entry: ExpressionEntry, coreModel: PixiLive2DInternalModel['coreModel']): number {
    switch (entry.blend) {
      case 'Add':
        return entry.modelDefault + entry.currentValue
      case 'Multiply': {
        const currentFrameValue = coreModel.getParameterValueById(entry.parameterId) as number
        return currentFrameValue * entry.currentValue
      }
      default:
        return entry.currentValue
    }
  }

  /** Look up an entry by its Live2D parameter ID. */
  function findEntryByParameterId(paramId: string): ExpressionEntry | undefined {
    for (const entry of store.expressions.values()) {
      if (entry.parameterId === paramId)
        return entry
    }
    return undefined
  }

  // ---- Cleanup -------------------------------------------------------------

  function dispose() {
    if (emotionResetTimer !== undefined)
      clearTimeout(emotionResetTimer)
    emotionResetTimer = undefined
    emotionParameters.clear()
    emotionActive.value = false
    activeLastFrame.clear()
    store.dispose()
  }

  // ---- Private helpers -----------------------------------------------------

  function normaliseBlend(raw: string): ExpressionBlendMode {
    switch (raw) {
      case 'Add':
        return 'Add'
      case 'Multiply':
        return 'Multiply'
      default:
        return 'Overwrite'
    }
  }

  /**
   * Reads the rig default rather than the current animated value.
   * Unknown parameters use zero because they cannot affect the loaded rig.
   */
  function getModelParameterDefault(parameterId: string): number {
    const coreModel = options.internalModel.value?.coreModel
    if (!coreModel)
      return 0
    const index = coreModel.getParameterIndex(parameterId)
    if (index < 0 || index >= coreModel.getParameterCount())
      return 0
    return coreModel.getParameterDefaultValue(index)
  }

  return {
    initialise,
    setEmotion,
    emotionActive,
    applyExpressions,
    dispose,
  }
}
