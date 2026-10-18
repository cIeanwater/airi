import type { CDPSession } from '@vitest/browser-playwright'

import type { MessageContextResult } from '../../../../libs/local-message-context'
import type { ChatHistoryItem } from '../../../../types/chat'

import en from '@proj-airi/i18n/locales/en'

import { createPinia, disposePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render } from 'vitest-browser-vue'
import { cdp, page, userEvent } from 'vitest/browser'
import { nextTick, ref } from 'vue'
import { createI18n } from 'vue-i18n'

import ChatHistory from './history.vue'
import MessageContextDialog from './message-context-dialog.vue'

import '@unocss/reset/tailwind.css'
import 'virtual:uno.css'

const load = vi.fn<(message: ChatHistoryItem) => Promise<MessageContextResult>>()
const selectionKey = ref('local-session')

// The saved-record loader is the model service boundary. Components and Pinia stay real.
vi.mock('../composables/use-message-context', () => ({ useMessageContext: () => ({ load, selectionKey }) }))

const stores: ReturnType<typeof createPinia>[] = []
let browserContextId: string | undefined

async function setClipboardPermission(allowed: boolean) {
  const session: CDPSession = cdp()
  await session.send('Browser.grantPermissions', {
    browserContextId,
    origin: location.origin,
    permissions: allowed ? ['clipboardReadWrite', 'clipboardSanitizedWrite'] : [],
  })
}

beforeEach(async () => {
  load.mockReset()
  selectionKey.value = 'local-session'
  browserContextId = (await cdp().send('Target.getTargetInfo')).targetInfo.browserContextId
  await setClipboardPermission(true)
  await page.viewport(1280, 720)
})
afterEach(async () => {
  await cdp().send('Browser.resetPermissions', { browserContextId })
  vi.restoreAllMocks()
  for (const store of stores.splice(0))
    disposePinia(store)
})

const first: ChatHistoryItem = { id: 'first', role: 'assistant', content: 'First answer', slices: [], tool_results: [] }
const second: ChatHistoryItem = { id: 'second', role: 'assistant', content: 'Second answer', slices: [], tool_results: [] }

function plugins() {
  const pinia = createPinia()
  stores.push(pinia)
  return [pinia, createI18n({ legacy: false, locale: 'en', messages: { en } })]
}

function savedRecord(prompt = 'Original saved prompt') {
  return {
    request: { messages: [{ role: 'system', content: prompt }, { role: 'user', content: 'Original question' }] },
    model_input: { prompt: '<system>Original saved prompt</system>' },
    model_output_raw_text: 'Raw answer with user marker',
    response: { choices: [{ message: { role: 'assistant', content: 'Final answer' } }] },
  }
}

async function readDownload(source: unknown) {
  if (!(source instanceof HTMLAnchorElement))
    throw new Error('Expected the download to use an anchor element')
  const response = await fetch(source.href)
  return { filename: source.download, content: await response.text() }
}

describe('saved message context', () => {
  it('shows model messages and the aggregated assistant response without stream envelopes by default', async () => {
    const record = {
      ...savedRecord(),
      model_input: { messages: [{ role: 'user', content: 'Actual forwarded question' }] },
      stream_events: [{ object: 'chat.completion.chunk', choices: [{ delta: { content: 'Final answer' } }] }],
    }
    load.mockResolvedValue({ status: 'found', match: 'id', records: [record] })
    render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await expect.poll(() => page.getByLabelText('Model messages JSON').element().textContent).toBe(JSON.stringify([{
      messages: record.model_input.messages,
      response: record.response.choices[0].message,
    }], null, 2))
  })

  it('opens the selected message, defaults to saved messages, and keeps the full record available', async () => {
    const records = [savedRecord()]
    load.mockResolvedValue({ status: 'found', match: 'id', records })
    const view = render(ChatHistory, {
      props: { messages: [first] },
      attrs: { style: 'height: 400px; width: 600px' },
      global: { plugins: plugins() },
    })
    await view.getByText('First answer', { exact: true }).click({ button: 'right' })
    await page.getByRole('menuitem', { name: 'View full context' }).click()
    await expect.element(page.getByRole('dialog', { name: 'Full message context' })).toBeVisible()
    await expect.element(page.getByLabelText('Model messages JSON')).toHaveTextContent('Original saved prompt')
    await expect.element(page.getByRole('button', { name: 'Messages', exact: true })).toHaveAttribute('aria-pressed', 'true')
    expect(load).toHaveBeenCalledWith(first)
    await expect.element(page.getByRole('button', { name: 'Close', exact: true })).toHaveFocus()
    await userEvent.tab()
    expect(page.getByRole('dialog').element().contains(document.activeElement)).toBe(true)
    await userEvent.tab()
    await expect.element(page.getByRole('button', { name: 'Complete record', exact: true })).toHaveFocus()
    await userEvent.tab()
    expect(page.getByRole('dialog').element().contains(document.activeElement)).toBe(true)
    await userEvent.tab()
    await expect.element(page.getByLabelText('Model messages JSON')).toHaveFocus()
    await page.getByRole('button', { name: 'Copy messages JSON' }).click()
    await expect.element(page.getByRole('button', { name: 'JSON copied' })).toBeVisible()
    await page.getByRole('button', { name: 'Complete record', exact: true }).click()
    await expect.poll(() => page.getByLabelText('Complete message record JSON').element().textContent).toBe(JSON.stringify(records, null, 2))
    await expect.element(page.getByRole('button', { name: 'Copy complete record JSON' })).toBeVisible()
    await expect.element(page.getByRole('button', { name: 'Download complete record JSON' })).toBeEnabled()
    await userEvent.keyboard('{Escape}')
    await expect.element(page.getByRole('dialog', { name: 'Full message context' })).not.toBeInTheDocument()
  })

  it('keeps a late record from replacing the newly selected message', async () => {
    let resolveFirst: ((result: MessageContextResult) => void) | undefined
    load.mockImplementationOnce(() => new Promise(resolve => resolveFirst = resolve))
    load.mockResolvedValueOnce({ status: 'found', match: 'id', records: [savedRecord('Second saved prompt')] })
    const view = render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await expect.element(page.getByRole('status')).toHaveTextContent('Loading saved context')
    await view.rerender({ message: second })
    await expect.element(page.getByLabelText('Model messages JSON')).toHaveTextContent('Second saved prompt')
    resolveFirst?.({ status: 'found', match: 'id', records: [savedRecord('Stale first prompt')] })
    await Promise.resolve()
    await nextTick()
    await expect.poll(() => load.mock.calls.length).toBe(2)
    await expect.element(page.getByLabelText('Model messages JSON')).not.toHaveTextContent('Stale first prompt')
  })

  it('copies and downloads exactly the selected view without changing the saved record', async () => {
    const records = [{ ...savedRecord(), stream_events: [{ object: 'chat.completion.chunk' }] }]
    const messages = [{ messages: records[0].request.messages, response: records[0].response.choices[0].message }]
    const original = JSON.stringify(records)
    load.mockResolvedValue({ status: 'found', match: 'id', records })
    const copy = vi.spyOn(navigator.clipboard, 'writeText')
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click')
    render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await page.getByRole('button', { name: 'Copy messages JSON' }).click()
    expect(copy).toHaveBeenLastCalledWith(JSON.stringify(messages, null, 2))
    await page.getByRole('button', { name: 'Download messages JSON' }).click()
    const messagesDownload = await readDownload(download.mock.contexts[0])
    expect(messagesDownload.filename).toBe('humanning-message-messages.json')
    expect(messagesDownload.content).toBe(JSON.stringify(messages, null, 2))
    expect(JSON.parse(await navigator.clipboard.readText())).toEqual(messages)

    await page.getByRole('button', { name: 'Complete record', exact: true }).click()
    await page.getByRole('button', { name: 'Copy complete record JSON' }).click()
    expect(copy).toHaveBeenLastCalledWith(JSON.stringify(records, null, 2))
    await page.getByRole('button', { name: 'Download complete record JSON' }).click()
    const recordDownload = await readDownload(download.mock.contexts[1])
    expect(recordDownload.filename).toBe('humanning-message-record.json')
    expect(recordDownload.content).toBe(JSON.stringify(records, null, 2))
    expect(JSON.parse(await navigator.clipboard.readText())).toEqual(records)
    expect(JSON.stringify(records)).toBe(original)
  })

  it('shows a real clipboard permission failure and clears it after a successful retry', async () => {
    load.mockResolvedValue({ status: 'found', match: 'id', records: [savedRecord()] })
    await setClipboardPermission(false)
    render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await page.getByRole('button', { name: 'Copy messages JSON' }).click()
    await expect.element(page.getByRole('alert')).toHaveTextContent('Failed to copy the JSON.')
    await expect.element(page.getByRole('button', { name: 'JSON copied' })).not.toBeInTheDocument()

    await setClipboardPermission(true)
    await page.getByRole('button', { name: 'Copy messages JSON' }).click()
    await expect.element(page.getByRole('button', { name: 'JSON copied' })).toBeVisible()
    await expect.element(page.getByRole('alert')).not.toBeInTheDocument()
    expect(await navigator.clipboard.readText()).toContain('Original saved prompt')
  })

  it.each(['success', 'failure'] as const)('discards a late clipboard %s after switching views', async (outcome) => {
    load.mockResolvedValue({ status: 'found', match: 'id', records: [savedRecord()] })
    const release = Promise.withResolvers<void>()
    const nativeWrite = navigator.clipboard.writeText.bind(navigator.clipboard)
    // Delay the real clipboard operation so its completion reaches a different view.
    const copy = vi.spyOn(navigator.clipboard, 'writeText').mockImplementation(async (text) => {
      await release.promise
      await nativeWrite(text)
    })
    render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await page.getByRole('button', { name: 'Copy messages JSON' }).click()
    await page.getByRole('button', { name: 'Complete record', exact: true }).click()
    if (outcome === 'failure')
      await setClipboardPermission(false)
    release.resolve()
    await copy.mock.results[0].value.catch(() => {})
    await nextTick()
    await expect.element(page.getByRole('button', { name: 'Copy complete record JSON' })).toBeVisible()
    await expect.element(page.getByRole('button', { name: 'JSON copied' })).not.toBeInTheDocument()
    await expect.element(page.getByRole('alert')).not.toBeInTheDocument()
  })

  it('resets to messages when selecting another message or reopening the dialog', async () => {
    load.mockResolvedValue({ status: 'found', match: 'id', records: [savedRecord()] })
    const view = render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await page.getByRole('button', { name: 'Complete record', exact: true }).click()
    await expect.element(page.getByLabelText('Complete message record JSON')).toBeVisible()
    await view.rerender({ message: second })
    await expect.element(page.getByLabelText('Model messages JSON')).toBeVisible()
    await page.getByRole('button', { name: 'Complete record', exact: true }).click()
    await view.rerender({ open: false })
    await view.rerender({ open: true })
    await expect.element(page.getByLabelText('Model messages JSON')).toBeVisible()
    await expect.element(page.getByRole('button', { name: 'Copy messages JSON' })).toBeVisible()
  })

  it.each([
    { id: 'user', role: 'user', content: 'User question' },
    { id: 'error', role: 'error', content: 'Model request failed' },
  ] satisfies ChatHistoryItem[])('opens the $role message from the dropdown menu', async (message) => {
    load.mockResolvedValue({ status: 'unavailable', reason: 'not-recorded' })
    const view = render(ChatHistory, {
      props: { messages: [message] },
      attrs: { style: 'height: 400px; width: 600px' },
      global: { plugins: plugins() },
    })
    await view.getByRole('button', { name: 'Message actions', exact: true }).click()
    await page.getByRole('menuitem', { name: 'View full context', exact: true }).click()
    await expect.element(page.getByRole('dialog', { name: 'Full message context' })).toBeVisible()
    await expect.element(page.getByRole('status')).toHaveTextContent('No saved record was found')
    expect(load).toHaveBeenCalledWith(message)
  })

  it('shows a network failure and reloads the same message after retry', async () => {
    load.mockRejectedValueOnce(new Error('Local service unavailable'))
    load.mockResolvedValueOnce({ status: 'found', match: 'id', records: [savedRecord()] })
    render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await expect.element(page.getByRole('alert')).toHaveTextContent('Local service unavailable')
    await page.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect.element(page.getByLabelText('Model messages JSON')).toHaveTextContent('Original saved prompt')
    expect(load).toHaveBeenNthCalledWith(2, first)
  })

  it.each([
    ['not-recorded', 'No saved record was found'],
    ['unsupported-provider', 'does not support saved message context'],
    ['recording-disabled', 'Message recording is disabled'],
    ['ambiguous', 'More than one saved record matches'],
  ] as const)('shows the %s state without invented JSON', async (reason, text) => {
    load.mockResolvedValue({ status: 'unavailable', reason })
    render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await expect.element(page.getByRole('status')).toHaveTextContent(text)
    await expect.element(page.getByRole('button', { name: 'Copy messages JSON' })).not.toBeInTheDocument()
  })

  it('keeps long JSON inside the dialog at a narrow viewport and marks content matches', async () => {
    load.mockResolvedValue({ status: 'found', match: 'content', records: [savedRecord('Long prompt '.repeat(2000))] })
    await page.viewport(320, 568)
    render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await expect.element(page.getByLabelText('Model messages JSON')).toBeVisible()
    await expect.element(page.getByRole('status')).toHaveTextContent('matches a saved record by content')
    const dialog = page.getByRole('dialog').element().getBoundingClientRect()
    expect(dialog.left).toBeGreaterThanOrEqual(0)
    expect(dialog.right).toBeLessThanOrEqual(320)
    expect(dialog.bottom).toBeLessThanOrEqual(568)
    await page.getByRole('button', { name: 'Close', exact: true }).click()
    await expect.element(page.getByRole('dialog')).not.toBeInTheDocument()
    await page.viewport(1280, 720)
  })
})
