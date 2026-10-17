import type { SpeechProviderWithExtraOptions } from '@xsai-ext/providers/utils'

import { generateSpeech } from '@xsai/generate-speech'
import { describe, expect, it } from 'vitest'

import { providerOpenAIAudioSpeech, providerOpenAICompatibleAudioSpeech } from '.'

describe('openAI compatible speech requests', () => {
  it('preserves per-request instructions and output format without leaking them to another request', async () => {
    const provider = await providerOpenAICompatibleAudioSpeech.createProvider({ apiKey: 'local', baseUrl: 'http://127.0.0.1:18084/v1/' }) as SpeechProviderWithExtraOptions<string, { instructions?: string, responseFormat?: string }>
    const first = provider.speech('qwen3-tts-1.7b-customvoice', { instructions: 'Speak with curiosity', responseFormat: 'wav' })
    const second = provider.speech('qwen3-tts-1.7b-customvoice')
    expect(first.instructions).toBe('Speak with curiosity')
    expect(first.responseFormat).toBe('wav')
    expect(first.model).toBe('qwen3-tts-1.7b-customvoice')
    expect(second).not.toHaveProperty('instructions')
    expect(second).not.toHaveProperty('responseFormat')
  })

  it('keeps connection settings out of the OpenAI body and preserves the explicitly selected model', async () => {
    const provider = await providerOpenAIAudioSpeech.createProvider({ apiKey: 'configured-key', baseUrl: 'https://api.openai.com/v1/' }) as SpeechProviderWithExtraOptions<string, Record<string, unknown>>
    let body: Record<string, unknown> | undefined
    let url = ''
    let authorization: string | null = null
    const options = provider.speech('gpt-4o-mini-tts', {
      baseUrl: 'https://wrong.invalid/v1/',
      baseURL: 'https://wrong.invalid/v1/',
      apiKey: 'wrong-key',
      model: 'wrong-model',
      instructions: 'Speak naturally',
      responseFormat: 'wav',
    })
    await generateSpeech({ ...options, model: String(options.model), input: 'Hello', voice: 'coral', fetch: async (input: RequestInfo | URL, init?: RequestInit) => {
      url = String(input)
      body = JSON.parse(String(init?.body))
      authorization = new Headers(init?.headers).get('Authorization')
      return new Response(new Uint8Array([1, 2, 3]))
    } })
    expect(url).toBe('https://api.openai.com/v1/audio/speech')
    expect(authorization).toBe('Bearer configured-key')
    expect(body?.model).toBe('gpt-4o-mini-tts')
    expect(body?.instructions).toBe('Speak naturally')
    expect(body?.response_format).toBe('wav')
    expect(body).not.toHaveProperty('base_url')
    expect(body).not.toHaveProperty('api_key')
  })
})
