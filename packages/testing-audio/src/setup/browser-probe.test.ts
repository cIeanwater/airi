import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import { createServer } from 'node:http'

import { chromium } from 'playwright'
import { expect, it } from 'vitest'

import { browserProbeOptions, stubForBrowser } from './browser-probe'

it.runIf(Boolean(process.env.TESTING_AUDIO_LOCAL_ASR_PROBE_URL))('captures complete native PCM and the final transcript through a real browser socket', async () => {
  const endpoint = process.env.TESTING_AUDIO_LOCAL_ASR_PROBE_URL!
  const fixture = await readFile(new URL('../../cases/single-utterance-pipeline/input.test.wav', import.meta.url))
  const server = createServer((_request, response) => {
    response.setHeader('Content-Type', 'text/html')
    response.end('<!doctype html><title>Transcription probe regression</title>')
  })
  await new Promise<void>((resolve) => {
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  if (!address || typeof address === 'string')
    throw new Error('The local probe page requires a TCP port.')

  const browser = await chromium.launch({ channel: 'msedge' })
  try {
    const page = await browser.newPage()
    await page.addInitScript(stubForBrowser, browserProbeOptions)
    await page.goto(`http://127.0.0.1:${address.port}`)

    const ignoredState = await page.evaluate(async (url) => {
      const target = new URL(url)
      target.pathname = '/unrelated-stream'
      await new Promise<void>((resolve, reject) => {
        const socket = new WebSocket(target)
        socket.addEventListener('open', () => socket.send(new Uint8Array(10_000)))
        socket.addEventListener('close', () => resolve())
        socket.addEventListener('error', reject)
      })
      return window.__airiAudioInputE2E
    }, endpoint)
    expect(ignoredState?.transcriptionAudio).toEqual([])
    expect(ignoredState?.transcriptionResults).toEqual([])
    expect(ignoredState?.streamingTranscriptionReady).toBe(false)

    const observation = await page.evaluate(async ({ endpoint, audioBase64 }) => {
      const context = new AudioContext()
      const bytes = Uint8Array.from(atob(audioBase64), value => value.charCodeAt(0))
      const decoded = await context.decodeAudioData(bytes.buffer)
      await context.close()
      const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * 16000), 16000)
      const source = offline.createBufferSource()
      source.buffer = decoded
      source.connect(offline.destination)
      source.start()
      const mono = (await offline.startRendering()).getChannelData(0)
      const pcm = new Uint8Array(mono.length * 2)
      const view = new DataView(pcm.buffer)
      for (let index = 0; index < mono.length; index++)
        view.setInt16(index * 2, Math.max(-32768, Math.min(32767, Math.round(mono[index] * 32768))), true)

      const finalText = await new Promise<string>((resolve, reject) => {
        const socket = new WebSocket(endpoint)
        socket.addEventListener('error', reject)
        socket.addEventListener('open', () => {
          socket.send(JSON.stringify({ type: 'start', model: 'zipformer-small-zh-en-int8', sample_rate: 16000 }))
          for (let offset = 0; offset < pcm.length; offset += 3200)
            socket.send(pcm.subarray(offset, Math.min(pcm.length, offset + 3200)))
          socket.send(JSON.stringify({ type: 'finish' }))
        })
        socket.addEventListener('message', (event) => {
          const payload = JSON.parse(event.data) as { type: string, text?: string }
          if (payload.type === 'final') {
            socket.close()
            resolve(payload.text ?? '')
          }
          if (payload.type === 'error')
            reject(new Error('The real ASR service rejected the fixture.'))
        })
      })
      return { finalText, expectedPcm: pcm.toBase64(), state: window.__airiAudioInputE2E }
    }, { endpoint, audioBase64: fixture.toString('base64') })

    expect(observation.finalText).toMatch(/PLEASE SAY HELLO/)
    expect(observation.state?.streamingTranscriptionReady).toBe(true)
    expect(observation.state?.streamingTranscriptionUpdates.length).toBeGreaterThan(0)
    expect(observation.state?.transcriptionResults).toEqual([observation.finalText])
    expect(observation.state?.transcriptionAudio).toHaveLength(1)
    expect(observation.state?.transcriptionAudio[0].format).toBe('pcm')
    const captured = Buffer.from(observation.state!.transcriptionAudio[0].base64, 'base64')
    const expected = Buffer.from(observation.expectedPcm, 'base64')
    expect(captured.byteLength).toBeGreaterThan(8000)
    expect(captured.equals(expected)).toBe(true)
  }
  finally {
    await browser.close()
    await new Promise<void>((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve())
    })
  }
}, 30_000)
