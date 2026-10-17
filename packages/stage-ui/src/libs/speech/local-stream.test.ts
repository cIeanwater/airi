import type { ServerResponse } from 'node:http'

import { Buffer } from 'node:buffer'
import { createServer } from 'node:http'

import { afterEach, describe, expect, it } from 'vitest'

import { streamLocalSpeech } from './local-stream'

const cleanups: (() => Promise<void>)[] = []

afterEach(async () => {
  await Promise.all(cleanups.splice(0).map(cleanup => cleanup()))
})

async function fixture(handle: (response: ServerResponse, body: string) => void) {
  const server = createServer((request, response) => {
    let body = ''
    request.setEncoding('utf8')
    request.on('data', chunk => body += chunk)
    request.on('end', () => handle(response, body))
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  cleanups.push(() => new Promise<void>((resolve) => {
    server.close(() => resolve())
    server.closeAllConnections()
  }))
  const address = server.address()
  if (!address || typeof address === 'string')
    throw new Error('Missing local fixture address')
  return `http://127.0.0.1:${address.port}/v1/`
}

function input(text = '你好，欢迎回来。') {
  return new ReadableStream<string>({ start(controller) {
    controller.enqueue(text)
    controller.close()
  } })
}

function pcmHeaders(response: ServerResponse) {
  response.writeHead(200, { 'Content-Type': 'audio/pcm', 'X-Audio-Sample-Rate': '48000', 'X-Audio-Channels': '2' })
}

describe('local streaming speech transport', () => {
  it('plays stereo PCM before HTTP EOF and preserves frames across split bytes', async () => {
    const finish = Promise.withResolvers<void>()
    const baseUrl = await fixture((response, body) => {
      expect(JSON.parse(body)).toMatchObject({ stream: true, response_format: 'pcm', model: 'moss-tts-nano-100m', voice: 'Xiaoyu' })
      pcmHeaders(response)
      response.write(Buffer.from([0, 64, 0]))
      response.write(Buffer.from([192, 0, 128, 255, 127]))
      void finish.promise.then(() => response.end(Buffer.from([0, 0, 0, 0])))
    })
    const output = streamLocalSpeech({ baseUrl, model: 'moss-tts-nano-100m', voice: 'Xiaoyu' }, input(), new AbortController().signal).getReader()
    const part = await output.read()
    if (part.done || part.value.audio instanceof Blob)
      throw new Error('Missing playable PCM stream')
    const pcm = part.value.audio.getReader()
    const first = await pcm.read()
    expect(first.done).toBe(false)
    expect(first.value?.sampleRate).toBe(48000)
    expect(Array.from(first.value?.channels[0] ?? [])).toEqual([0.5, -1])
    expect(Array.from(first.value?.channels[1] ?? [])).toEqual([-0.5, 32767 / 32768])
    expect(first.value?.range).toMatchObject({ startFrame: 0, endFrame: 2 })
    finish.resolve()
    const last = await pcm.read()
    expect(last.value?.range).toMatchObject({ startFrame: 2, endFrame: 3, sourceId: first.value?.range.sourceId })
    expect((await pcm.read()).done).toBe(true)
    expect((await output.read()).done).toBe(true)
  })

  it('aborts an unfinished HTTP body and never starts the next phrase', async () => {
    const disconnected = Promise.withResolvers<void>()
    let requests = 0
    const baseUrl = await fixture((response) => {
      requests++
      pcmHeaders(response)
      response.write(Buffer.alloc(8))
      response.on('close', () => disconnected.resolve())
    })
    const abort = new AbortController()
    const output = streamLocalSpeech({ baseUrl, model: 'moss-tts-nano-100m', voice: 'Xiaoyu' }, input('第一句话。第二句话。'), abort.signal).getReader()
    const part = await output.read()
    if (part.done || part.value.audio instanceof Blob)
      throw new Error('Missing playable PCM stream')
    const pcm = part.value.audio.getReader()
    await pcm.read()
    abort.abort(new Error('Stopped this response'))
    await expect(pcm.read()).rejects.toThrow()
    await expect(output.read()).rejects.toThrow()
    await disconnected.promise
    expect(requests).toBe(1)
  })

  it('rejects truncated PCM rather than playing a corrupt last frame', async () => {
    const baseUrl = await fixture((response) => {
      pcmHeaders(response)
      response.write(Buffer.alloc(4))
      setImmediate(() => response.end(Buffer.from([1])))
    })
    const output = streamLocalSpeech({ baseUrl, model: 'moss-tts-nano-100m', voice: 'Xiaoyu' }, input(), new AbortController().signal).getReader()
    const part = await output.read()
    if (part.done || part.value.audio instanceof Blob)
      throw new Error('Missing playable PCM stream')
    const pcm = part.value.audio.getReader()
    // The producer may already have detected the partial frame before this read.
    await expect((async () => {
      while (true) {
        if ((await pcm.read()).done)
          return
      }
    })()).rejects.toThrow('inside a PCM frame')
    await expect(output.read()).rejects.toThrow('inside a PCM frame')
  })

  it('rejects an incompatible WAV response instead of interpreting it as PCM', async () => {
    const baseUrl = await fixture(response => response.writeHead(200, { 'Content-Type': 'audio/wav' }).end('RIFF'))
    const output = streamLocalSpeech({ baseUrl, model: 'moss-tts-nano-100m', voice: 'Xiaoyu' }, input(), new AbortController().signal).getReader()
    await expect(output.read()).rejects.toThrow('invalid PCM metadata')
  })

  it('rejects remote endpoints before consuming any text', () => {
    expect(() => streamLocalSpeech({ baseUrl: 'https://example.com/v1/', model: 'moss-tts-nano-100m', voice: 'Xiaoyu' }, input(), new AbortController().signal)).toThrow('loopback')
  })
})
