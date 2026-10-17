import { env } from '@huggingface/transformers'
import { expect, it } from 'vitest'
import { commands } from 'vitest/browser'

import { SileroVad } from './silero-vad'

declare module 'vitest/browser' {
  interface BrowserCommands {
    voiceExternalRequests: (block: boolean) => Promise<string[]>
  }
}

it('scores audio with a cold model cache and all external requests blocked', async () => {
  // ROOT CAUSE:
  // The first microphone window loaded VAD from Hugging Face and ONNX WASM from a CDN.
  // Network failure prevented transcription before a request reached the local ASR.
  // Both files now come from build assets, without a remote fallback.
  await commands.voiceExternalRequests(true)
  const browserCache = env.useBrowserCache
  env.useBrowserCache = false
  const vad = new SileroVad()
  try {
    const probability = await vad.score({
      range: { sourceId: 'offline-vad', startFrame: 0, endFrame: 512 },
      sampleRate: 16000,
      channels: [new Float32Array(512)],
      discontinuity: false,
    }, new AbortController().signal)
    expect(probability).toBeGreaterThanOrEqual(0)
    expect(probability).toBeLessThan(0.6)
  }
  finally {
    await vad.close()
    env.useBrowserCache = browserCache
    expect(await commands.voiceExternalRequests(false)).toEqual([])
  }
}, 30_000)
