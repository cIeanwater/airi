import type { GenerationRequest } from '@proj-airi/provider-inference'

import { createServer } from 'node:http'

import { afterEach, describe, expect, it } from 'vitest'

import { createLocalMessageContextHeaders, fetchLocalMessageContext, resolveMessageContextTarget } from './local-message-context'

const server = createServer()
afterEach(async () => {
  if (server.listening)
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  server.removeAllListeners('request')
})

function request(baseURL = 'http://127.0.0.1:18083/v1/', model = 'qwen3.5-2b-bf16'): GenerationRequest {
  return { protocol: 'chat-completions', config: { model, baseURL } }
}

describe('local message context correlation', () => {
  it.each(['qwen3.5-2b-bf16', 'gemma4-e2b-int4'])('adds stable message and session IDs only to local %s', (model) => {
    const correlation = { conversationId: 'session', turnId: 'user-id', messageId: 'assistant-id' }
    expect(createLocalMessageContextHeaders(request('http://127.0.0.1:18083/v1/', model), correlation)).toEqual({
      'X-Humanning-Session-ID': 'session',
      'X-Humanning-Turn-ID': 'user-id',
      'X-Humanning-Message-ID': 'assistant-id',
    })
    expect(createLocalMessageContextHeaders(request('https://remote.example/v1/'), correlation)).toEqual({})
    expect(createLocalMessageContextHeaders(request('http://127.0.0.1:18083/v1/', 'another-model'), correlation)).toEqual({})
    expect(createLocalMessageContextHeaders(request(), undefined)).toEqual({})
  })

  it('finds only the selected user turn response and never a later turn', () => {
    const user = { role: 'user', id: 'u1', content: 'first' } as const
    const nextUser = { role: 'user', id: 'u2', content: 'second' } as const
    const assistant = { role: 'assistant', id: 'a2', content: 'second answer', slices: [], tool_results: [] } as const
    expect(resolveMessageContextTarget([user, nextUser, { ...assistant, slices: [], tool_results: [] }], user)).toBeUndefined()
    expect(resolveMessageContextTarget([nextUser, { ...assistant, slices: [], tool_results: [] }], nextUser)?.id).toBe('a2')
  })

  it('reads the saved record through real HTTP without forwarding credentials', async () => {
    const records = [{ request: { messages: [{ role: 'system', content: 'saved prompt' }] }, model_output_raw_text: 'answer' }]
    const requests: { body: string, authorization: string | undefined, cookie: string | undefined, path: string | undefined }[] = []
    server.on('request', async (incoming, response) => {
      let body = ''
      for await (const chunk of incoming)
        body += chunk
      requests.push({ body, authorization: incoming.headers.authorization, cookie: incoming.headers.cookie, path: incoming.url })
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify({ status: 'found', match: 'id', records }))
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const address = server.address()
    if (!address || typeof address === 'string')
      throw new Error('Expected an HTTP port')
    const target = request(`http://127.0.0.1:${address.port}/v1/`)
    const query = { message_id: 'assistant-id', session_id: 'session', content: 'answer', created_at: 1000 }
    expect(await fetchLocalMessageContext(target, query)).toEqual({ status: 'found', match: 'id', records })
    expect(requests).toEqual([{ body: JSON.stringify(query), authorization: undefined, cookie: undefined, path: '/v1/debug/message-context' }])
  })

  it('refuses remote endpoints before sending any message text', async () => {
    expect(await fetchLocalMessageContext(request('https://remote.example/v1/'), { content: 'private answer' })).toEqual({ status: 'unavailable', reason: 'unsupported-provider' })
    expect(await fetchLocalMessageContext(request('http://user:secret@localhost:18083/v1/'), { content: 'private answer' })).toEqual({ status: 'unavailable', reason: 'unsupported-provider' })
  })
})
