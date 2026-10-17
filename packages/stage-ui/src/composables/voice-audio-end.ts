import { boolean, maxValue, minValue, number, object, parse, pipe } from 'valibot'

import { useHearingStore } from '../stores/modules/hearing'

const resultSchema = object({ complete: boolean(), probability: pipe(number(), minValue(0), maxValue(1)) })

/** Sends in-memory PCM to the chosen local endpoint. It stores no recording or transcript. */
export function useVoiceAudioEnd() {
  const hearing = useHearingStore()

  async function judge(audio: Float32Array, sampleRate: number, signal: AbortSignal) {
    const endpoint = new URL(hearing.endJudgeAudioEndpoint)
    if (!['http:', 'https:'].includes(endpoint.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname))
      throw new Error('Audio turn detection requires a local HTTP endpoint')
    endpoint.searchParams.set('sample_rate', String(sampleRate))
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: audio.slice().buffer,
      signal: AbortSignal.any([signal, AbortSignal.timeout(2000)]),
    })
    if (!response.ok)
      throw new Error(`Audio turn detection failed: HTTP ${response.status}`)
    return parse(resultSchema, await response.json()).complete
  }

  return { judge }
}
