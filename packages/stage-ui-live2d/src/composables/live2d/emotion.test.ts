import { describe, expect, it } from 'vitest'

import { Emotion } from '../../constants/emotions'
import { resolveEmotionMotion } from './emotion'

describe('resolveEmotionMotion', () => {
  it('uses an explicit file mapping instead of requesting a missing Happy group', () => {
    const motions = [
      { motionName: 'Tap', motionIndex: 1, fileName: 'motions/tap_02.motion3.json' },
      { motionName: 'Happy', motionIndex: 0, fileName: 'motions/happy.motion3.json' },
    ]
    expect(resolveEmotionMotion(Emotion.Happy, motions, { 'motions/tap_02.motion3.json': 'Happy' })).toEqual({ group: 'Tap', index: 1 })
  })

  it('matches real group names without case sensitivity and ignores absent groups', () => {
    const motions = [{ motionName: 'idle', motionIndex: 0, fileName: 'idle.motion3.json' }]
    expect(resolveEmotionMotion(Emotion.Neutral, motions, {})).toEqual({ group: 'idle', index: 0 })
    expect(resolveEmotionMotion(Emotion.Happy, motions, {})).toBeUndefined()
  })

  it('releases zero-weight emotions without playing a motion and can restore neutral Idle', () => {
    const motions = [
      { motionName: 'Happy', motionIndex: 0, fileName: 'happy.motion3.json' },
      { motionName: 'Idle', motionIndex: 0, fileName: 'idle.motion3.json' },
    ]
    expect(resolveEmotionMotion(Emotion.Happy, motions, {}, 0)).toBeUndefined()
    expect(resolveEmotionMotion(Emotion.Happy, motions, {}, -1)).toBeUndefined()
    expect(resolveEmotionMotion(Emotion.Neutral, motions, {}, 0)).toEqual({ group: 'Idle', index: 0 })
    expect(resolveEmotionMotion(Emotion.Happy, motions, {}, Number.POSITIVE_INFINITY)).toEqual({ group: 'Happy', index: 0 })
    expect(resolveEmotionMotion(Emotion.Happy, motions, {}, Number.NaN)).toEqual({ group: 'Happy', index: 0 })
  })
})
