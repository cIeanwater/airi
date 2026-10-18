import { boolean, maxLength, maxValue, minValue, number, object, parse, pipe, safeParse, string } from 'valibot'

import { useHearingStore } from '../stores/modules/hearing'

const resultSchema = object({ complete: boolean(), probability: pipe(number(), minValue(0), maxValue(1)) })
const errorSchema = object({ error: pipe(string(), maxLength(512)) })

/** Sends in-memory PCM to the chosen local endpoint. It stores no recording or transcript. */
export function useVoiceAudioEnd() {
  const hearing = useHearingStore()

  async function judge(audio: Float32Array, sampleRate: number, signal: AbortSignal) {
    const endpoint = new URL(hearing.endJudgeAudioEndpoint)
    if (!['http:', 'https:'].includes(endpoint.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname))
      throw new Error('Audio turn detection requires a local HTTP endpoint')
    endpoint.searchParams.set('sample_rate', String(sampleRate))
    const samples = audio.slice()
    let peak = 0
    for (const sample of samples) {
      if (!Number.isFinite(sample))
        throw new Error('Audio turn detection requires finite PCM samples')
      peak = Math.max(peak, Math.abs(sample))
    }
    // Web Audio permits full-scale float peaks after gain or mixing. Preserve their ratios for normalized PCM.
    if (peak > 1) {
      for (let index = 0; index < samples.length; index++)
        samples[index] /= peak
    }
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      credentials: 'omit',
      body: samples.buffer,
      signal: AbortSignal.any([signal, AbortSignal.timeout(2000)]),
    })
    if (!response.ok) {
      const detail = safeParse(errorSchema, await response.json().catch(() => undefined))
      throw new Error(`Audio turn detection failed: HTTP ${response.status}${detail.success ? `: ${detail.output.error}` : ''}`)
    }
    return parse(resultSchema, await response.json()).complete
  }

  return { judge }
}
