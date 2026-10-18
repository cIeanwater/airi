import type { SpeechAudio } from '@proj-airi/core-agent'
import type { PcmBlock } from '@proj-airi/pipelines-audio'

import { chunkTtsInput, createPushStream } from '@proj-airi/pipelines-audio'

interface LocalSpeechOptions {
  baseUrl: string
  model: string
  voice: string
}

/** One response owns its text reader, HTTP bodies, and cancellation. PCM is playable before HTTP EOF. */
class LocalSpeechStream {
  private readonly abort = new AbortController()
  private readonly text: ReadableStreamDefaultReader<Uint8Array>
  private readonly output = createPushStream<SpeechAudio>(reason => this.abort.abort(reason))
  private readonly endpoint: URL

  constructor(private readonly options: LocalSpeechOptions, text: ReadableStream<string>, private readonly signal: AbortSignal) {
    signal.throwIfAborted()
    const base = new URL(options.baseUrl)
    if (!['http:', 'https:'].includes(base.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(base.hostname) || base.username || base.password || base.search || base.hash)
      throw new Error('Local streaming speech requires a loopback HTTP base URL')
    this.endpoint = new URL('audio/speech', `${base.href.replace(/\/$/, '')}/`)
    this.text = text.pipeThrough(new TextEncoderStream()).getReader()
    signal.addEventListener('abort', this.cancel, { once: true })
    this.abort.signal.addEventListener('abort', () => {
      this.output.error(this.abort.signal.reason)
      void this.text.cancel(this.abort.signal.reason).catch(() => {})
    }, { once: true })
    void this.run()
  }

  get stream() {
    return this.output.stream
  }

  private readonly cancel = () => this.abort.abort(this.signal.reason)

  private async run() {
    try {
      // GPT-SoVITS needs a whole sentence for connected prosody. Its PCM streams before synthesis finishes.
      const preserveSentence = this.options.model === 'gpt-sovits-v2proplus'
      const boost = preserveSentence ? 0 : 2
      const maximumWords = preserveSentence ? 24 : 16
      for await (const phrase of chunkTtsInput(this.text, { boost, minimumWords: 4, maximumWords })) {
        this.abort.signal.throwIfAborted()
        if (!phrase.text.trim())
          continue
        // The Python service accepts 240 Unicode characters. The word chunker checks length only at punctuation.
        const characters = Array.from(phrase.text)
        const requestLength = preserveSentence ? 240 : characters.length
        for (let offset = 0; offset < characters.length; offset += requestLength) {
          this.abort.signal.throwIfAborted()
          const text = characters.slice(offset, offset + requestLength).join('')
          const response = await fetch(this.endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: this.options.model, voice: this.options.voice, input: text, response_format: 'pcm', stream: true }),
            signal: this.abort.signal,
          })
          if (!response.ok)
            throw new Error(`Local speech returned HTTP ${response.status}`)
          await this.playableBody(response, text)
        }
      }
      this.abort.signal.throwIfAborted()
      this.output.close()
    }
    catch (error) {
      this.output.error(error)
      this.abort.abort(error)
    }
    finally {
      this.signal.removeEventListener('abort', this.cancel)
      await this.text.cancel().catch(() => {})
      this.text.releaseLock()
    }
  }

  private async playableBody(response: Response, text: string) {
    const sampleRate = Number(response.headers.get('X-Audio-Sample-Rate'))
    const channels = Number(response.headers.get('X-Audio-Channels'))
    if (!response.body || !response.headers.get('Content-Type')?.startsWith('audio/pcm') || !Number.isInteger(sampleRate) || sampleRate < 8000 || sampleRate > 96000 || ![1, 2].includes(channels)) {
      await response.body?.cancel()
      throw new Error('Local speech returned invalid PCM metadata')
    }
    const pcm = createPushStream<PcmBlock>(reason => this.abort.abort(reason))
    this.output.write({ audio: pcm.stream, text })
    const reader = response.body.getReader()
    const sourceId = crypto.randomUUID()
    const frameBytes = channels * 2
    let carry = new Uint8Array(0)
    let frame = 0
    try {
      while (true) {
        this.abort.signal.throwIfAborted()
        const item = await reader.read()
        if (item.done)
          break
        const bytes = new Uint8Array(carry.length + item.value.length)
        bytes.set(carry)
        bytes.set(item.value, carry.length)
        const count = Math.floor(bytes.length / frameBytes)
        const length = count * frameBytes
        carry = bytes.slice(length)
        if (!count)
          continue
        const view = new DataView(bytes.buffer, bytes.byteOffset, length)
        const planar = Array.from({ length: channels }, () => new Float32Array(count))
        for (let index = 0; index < count; index++) {
          for (let channel = 0; channel < channels; channel++)
            planar[channel][index] = view.getInt16((index * channels + channel) * 2, true) / 32768
        }
        pcm.write({ sampleRate, channels: planar, range: { sourceId, startFrame: frame, endFrame: frame + count } })
        frame += count
      }
      if (carry.length)
        throw new Error('Local speech ended inside a PCM frame')
      if (!frame)
        throw new Error('Local speech returned no audio')
      pcm.close()
    }
    catch (error) {
      pcm.error(error)
      throw error
    }
    finally {
      await reader.cancel().catch(() => {})
      reader.releaseLock()
    }
  }
}

export function streamLocalSpeech(options: LocalSpeechOptions, text: ReadableStream<string>, signal: AbortSignal): ReadableStream<SpeechAudio> {
  return new LocalSpeechStream(options, text, signal).stream
}
