import type { SpeechProviderWithExtraOptions } from '@xsai-ext/providers/utils'

import { generateSpeech } from '@xsai/generate-speech'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { providerOpenAIAudioSpeech, providerOpenAICompatibleAudioSpeech } from '.'

describe('openAI compatible speech requests', () => {
  afterEach(() => vi.unstubAllGlobals())

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

  it('discovers configured local voice profiles without forwarding credentials', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json({
      model: 'gpt-sovits-v2proplus',
      voices: [{ id: 'anime-playful', name: '动漫 · 轻快俏皮', description: 'Synthetic reference variant', gender: 'female' }],
    }))
    vi.stubGlobal('fetch', fetch)
    const provider = await providerOpenAICompatibleAudioSpeech.createProvider({ apiKey: 'private-local-key', baseUrl: 'http://127.0.0.1:18084/v1/' })
    const voices = await providerOpenAICompatibleAudioSpeech.extraMethods!.listVoices!({ apiKey: 'private-local-key', baseUrl: 'http://127.0.0.1:18084/v1/' }, provider, 'gpt-sovits-v2proplus')
    expect(String(fetch.mock.calls[0][0])).toBe('http://127.0.0.1:18084/v1/audio/voices')
    expect(fetch.mock.calls[0][1]?.credentials).toBe('omit')
    expect(fetch.mock.calls[0][1]?.headers).toBeUndefined()
    expect(voices[0]).toMatchObject({ id: 'anime-playful', name: '动漫 · 轻快俏皮', languages: [{ code: 'zh', title: 'Chinese' }] })
    expect(providerOpenAICompatibleAudioSpeech.extraMethods!.voiceCatalogConfig!({ apiKey: 'private-local-key', baseUrl: 'http://localhost:18084/v1' })).toEqual({ baseUrl: 'http://localhost:18084/v1/' })
  })

  it('retains manual voice entry for other models and non-loopback endpoints', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
    vi.stubGlobal('fetch', fetch)
    for (const [baseUrl, model] of [['https://example.com/v1/', 'gpt-sovits-v2proplus'], ['http://user:password@localhost:18084/v1/', 'gpt-sovits-v2proplus'], ['http://localhost:18084/v1/?token=private', 'gpt-sovits-v2proplus'], ['http://127.0.0.1:18084/v1/', 'tts-1']]) {
      const config = { apiKey: 'key', baseUrl }
      const provider = await providerOpenAICompatibleAudioSpeech.createProvider(config)
      expect(await providerOpenAICompatibleAudioSpeech.extraMethods!.listVoices!(config, provider, model)).toEqual([])
    }
    expect(fetch).not.toHaveBeenCalled()
  })

  it('rejects catalogs from another model and HTTP failures', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json({ model: 'different-model', voices: [] }))
    vi.stubGlobal('fetch', fetch)
    const config = { apiKey: 'key', baseUrl: 'http://127.0.0.1:18084/v1/' }
    const provider = await providerOpenAICompatibleAudioSpeech.createProvider(config)
    await expect(providerOpenAICompatibleAudioSpeech.extraMethods!.listVoices!(config, provider, 'gpt-sovits-v2proplus')).rejects.toThrow()
    fetch.mockResolvedValueOnce(new Response('', { status: 503 }))
    await expect(providerOpenAICompatibleAudioSpeech.extraMethods!.listVoices!(config, provider, 'gpt-sovits-v2proplus')).rejects.toThrow('HTTP 503')
  })
})
