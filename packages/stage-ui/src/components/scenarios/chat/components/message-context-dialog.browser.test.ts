import type { MessageContextResult } from '../../../../libs/local-message-context'
import type { ChatHistoryItem } from '../../../../types/chat'

import en from '@proj-airi/i18n/locales/en'

import { createPinia, disposePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render } from 'vitest-browser-vue'
import { page, userEvent } from 'vitest/browser'
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
beforeEach(async () => {
  load.mockReset()
  await page.viewport(1280, 720)
})
afterEach(() => {
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

describe('saved message context', () => {
  it('opens the selected message from its context menu and shows the saved complete JSON', async () => {
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
    await expect.poll(() => page.getByLabelText('Complete message record JSON').element().textContent).toBe(JSON.stringify(records, null, 2))
    expect(load).toHaveBeenCalledWith(first)
    await expect.element(page.getByRole('button', { name: 'Close', exact: true })).toHaveFocus()
    await userEvent.tab()
    expect(page.getByRole('dialog').element().contains(document.activeElement)).toBe(true)
    await userEvent.tab()
    await expect.element(page.getByLabelText('Complete message record JSON')).toHaveFocus()
    await page.getByRole('button', { name: 'Copy full JSON' }).click()
    await expect.element(page.getByRole('button', { name: 'JSON copied' })).toBeVisible()
    await expect.element(page.getByRole('button', { name: 'Download JSON' })).toBeEnabled()
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
    await expect.element(page.getByLabelText('Complete message record JSON')).toHaveTextContent('Second saved prompt')
    resolveFirst?.({ status: 'found', match: 'id', records: [savedRecord('Stale first prompt')] })
    await Promise.resolve()
    await nextTick()
    await expect.poll(() => load.mock.calls.length).toBe(2)
    await expect.element(page.getByLabelText('Complete message record JSON')).not.toHaveTextContent('Stale first prompt')
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
    await expect.element(page.getByLabelText('Complete message record JSON')).toHaveTextContent('Original saved prompt')
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
    await expect.element(page.getByRole('button', { name: 'Copy full JSON' })).not.toBeInTheDocument()
  })

  it('keeps long JSON inside the dialog at a narrow viewport and marks content matches', async () => {
    load.mockResolvedValue({ status: 'found', match: 'content', records: [savedRecord('Long prompt '.repeat(2000))] })
    await page.viewport(320, 568)
    render(MessageContextDialog, { props: { open: true, message: first }, global: { plugins: plugins() } })
    await expect.element(page.getByLabelText('Complete message record JSON')).toBeVisible()
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
