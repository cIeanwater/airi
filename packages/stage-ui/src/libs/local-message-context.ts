import type { StreamOptions } from '@proj-airi/core-agent'
import type { GenerationRequest } from '@proj-airi/provider-inference'

import type { ChatHistoryItem } from '../types/chat'

import { array, literal, object, parse, picklist, record, safeParse, string, unknown, variant } from 'valibot'

/** Saved model records are read only from an explicitly selected local model endpoint. */
export type MessageContextResult
  = | { status: 'found', match: 'id' | 'content', records: Record<string, unknown>[] }
    | { status: 'unavailable', reason: 'not-recorded' | 'unsupported-provider' | 'recording-disabled' | 'ambiguous' }

interface MessageContextQuery {
  message_id?: string
  session_id?: string
  content?: string
  created_at?: number
}

const resultSchema = variant('status', [
  object({ status: literal('found'), match: picklist(['id', 'content']), records: array(record(string(), unknown())) }),
  object({ status: literal('unavailable'), reason: picklist(['not-recorded', 'unsupported-provider', 'recording-disabled', 'ambiguous']) }),
])

const messagesSchema = object({ messages: array(record(string(), unknown())) })
const responseSchema = object({ choices: array(object({ message: record(string(), unknown()) })) })

/**
 * Projects saved exchanges without rebuilding messages or replaying stream chunks.
 * Actual model input takes precedence over the received request. Missing fields remain null.
 * Each saved request remains a separate round, including tool calls and tool results.
 */
export function projectMessageContext(records: Record<string, unknown>[]) {
  return records.map((saved) => {
    const modelInput = safeParse(messagesSchema, saved.model_input)
    const request = safeParse(messagesSchema, saved.request)
    const response = safeParse(responseSchema, saved.response)
    let messages: Record<string, unknown>[] | null = null
    if (modelInput.success)
      messages = modelInput.output.messages
    else if (request.success)
      messages = request.output.messages

    return {
      messages,
      // Both local services generate one choice and record its aggregated assistant message.
      response: response.success ? response.output.choices[0]?.message ?? null : null,
    }
  })
}

function localContextEndpoint(request: GenerationRequest): URL | undefined {
  if (request.protocol !== 'chat-completions' || !['qwen3.5-2b-bf16', 'gemma4-e2b-int4'].includes(request.config.model ?? ''))
    return
  let address: URL
  try {
    address = new URL(request.config.baseURL)
  }
  catch {
    // An incomplete local provider configuration has no endpoint to query.
    return
  }
  if (address.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(address.hostname)
    || address.username || address.password || address.search || address.hash) {
    return
  }
  return new URL('debug/message-context', `${address.href.replace(/\/$/, '')}/`)
}

/** Internal correlation is sent only to the local recorder, never to a remote provider. */
export function createLocalMessageContextHeaders(request: GenerationRequest, correlation: StreamOptions['requestCorrelation']): Record<string, string> {
  if (!correlation?.messageId || !localContextEndpoint(request))
    return {}
  return {
    'X-Humanning-Session-ID': correlation.conversationId,
    'X-Humanning-Turn-ID': correlation.turnId,
    'X-Humanning-Message-ID': correlation.messageId,
  }
}

/** A user message owns only its next assistant response, before another user turn starts. */
export function resolveMessageContextTarget(messages: ChatHistoryItem[], selected: ChatHistoryItem): ChatHistoryItem | undefined {
  const index = messages.findIndex(message => selected.id ? message.id === selected.id : message === selected)
  if (index < 0)
    return
  if (selected.role === 'assistant')
    return messages[index]
  if (selected.role !== 'user')
    return
  for (const message of messages.slice(index + 1)) {
    if (message.role === 'user')
      return
    if (message.role === 'assistant')
      return message
  }
}

/** Reads saved JSON without sending provider credentials or reconstructing the prompt from current settings. */
export async function fetchLocalMessageContext(request: GenerationRequest, query: MessageContextQuery): Promise<MessageContextResult> {
  const endpoint = localContextEndpoint(request)
  if (!endpoint)
    return { status: 'unavailable', reason: 'unsupported-provider' }
  const response = await fetch(endpoint, {
    method: 'POST',
    credentials: 'omit',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(query),
    signal: AbortSignal.timeout(15_000),
  })
  if (response.status === 404)
    return { status: 'unavailable', reason: 'unsupported-provider' }
  if (!response.ok)
    throw new Error(`Local message context request failed (${response.status})`)
  return parse(resultSchema, await response.json())
}
