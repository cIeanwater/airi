import type { AIRIStreamTranscriptionDelta, AIRIStreamTranscriptionResult } from '../providers/stream-transcription'

import { literal, object, parse, string, variant } from 'valibot'

const eventSchema = variant('type', [
  object({ type: literal('partial'), text: string() }),
  object({ type: literal('final'), text: string() }),
  object({ type: literal('error'), message: string() }),
])

/** One socket owns one utterance. It closes on final output, cancellation, input failure, or transport failure. */
class LocalStreamingTranscription {
  readonly result: AIRIStreamTranscriptionResult
  private readonly completion = Promise.withResolvers<string>()
  private readonly socket: WebSocket
  private readonly reader: ReadableStreamDefaultReader<Uint8Array>
  private fullOutput!: ReadableStreamDefaultController<AIRIStreamTranscriptionDelta>
  private textOutput!: ReadableStreamDefaultController<string>
  private settled = false
  private text = ''
  private timeout: ReturnType<typeof setTimeout> | undefined

  constructor(endpoint: string, model: string, audio: ReadableStream<Uint8Array>, private readonly signal: AbortSignal) {
    signal.throwIfAborted()
    const url = new URL(endpoint)
    if (!['ws:', 'wss:'].includes(url.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || url.username || url.password)
      throw new Error('Streaming voice input requires a loopback WebSocket URL')
    this.reader = audio.getReader()
    this.socket = new WebSocket(url)
    this.result = {
      text: this.completion.promise,
      fullStream: new ReadableStream({ start: output => this.fullOutput = output, cancel: reason => this.fail(reason) }),
      textStream: new ReadableStream({ start: output => this.textOutput = output }),
    }
    this.socket.addEventListener('open', () => {
      if (this.settled)
        return
      clearTimeout(this.timeout)
      this.socket.send(JSON.stringify({ type: 'start', model, sample_rate: 16000 }))
      void this.sendAudio().catch(error => this.fail(error))
    }, { once: true })
    this.socket.addEventListener('message', (event: MessageEvent<unknown>) => {
      if (this.settled)
        return
      try {
        if (typeof event.data !== 'string')
          throw new Error('Streaming voice input returned a non-JSON event')
        const update = parse(eventSchema, JSON.parse(event.data))
        if (update.type === 'error')
          throw new Error(update.message)
        this.text = update.text
        this.fullOutput.enqueue({ type: 'transcript.text.snapshot', text: this.text, isFinal: update.type === 'final', locale: 'und', startMilliseconds: 0, durationMilliseconds: 0 })
        if (update.type === 'final') {
          this.settled = true
          this.textOutput.enqueue(this.text)
          this.textOutput.close()
          this.fullOutput.close()
          this.completion.resolve(this.text)
          this.close()
        }
      }
      catch (error) {
        this.fail(error)
      }
    })
    this.socket.addEventListener('error', () => this.fail(new Error('Local streaming voice input connection failed')), { once: true })
    this.socket.addEventListener('close', () => {
      if (!this.settled)
        this.fail(new Error('Local streaming voice input closed before its final transcript'))
    }, { once: true })
    signal.addEventListener('abort', this.abort, { once: true })
    this.timeout = setTimeout(() => this.fail(new Error('Local streaming voice input connection timed out')), 3000)
    if (signal.aborted)
      this.abort()
  }

  private readonly abort = () => this.fail(this.signal.reason)

  private async sendAudio() {
    while (!this.settled) {
      this.signal.throwIfAborted()
      const item = await this.reader.read()
      if (this.settled)
        return
      if (item.done) {
        this.socket.send(JSON.stringify({ type: 'finish' }))
        this.timeout = setTimeout(() => this.fail(new Error('Local streaming voice input did not finalize')), 5000)
        return
      }
      while (this.socket.bufferedAmount > 262144) {
        this.signal.throwIfAborted()
        if (this.settled)
          return
        await new Promise(resolve => setTimeout(resolve, 5))
      }
      this.socket.send(item.value.slice())
    }
  }

  private fail(error: unknown) {
    if (this.settled)
      return
    this.settled = true
    this.fullOutput.error(error)
    this.textOutput.error(error)
    this.completion.reject(error)
    this.close()
  }

  private close() {
    clearTimeout(this.timeout)
    this.signal.removeEventListener('abort', this.abort)
    void this.reader.cancel('Local transcription settled').catch(() => {})
    if (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)
      this.socket.close()
  }
}

/** Streams PCM16 at 16 kHz to a persistent local recognizer, including replaceable transcripts before recording ends. */
export function streamLocalTranscription(endpoint: string, model: string, audio: ReadableStream<Uint8Array>, signal: AbortSignal): AIRIStreamTranscriptionResult {
  return new LocalStreamingTranscription(endpoint, model, audio, signal).result
}
