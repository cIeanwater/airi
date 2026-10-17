import type { MemoryScope } from '@proj-airi/memory-local'

import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { storage } from '../../database/storage'
import { useMemoryStore } from './memory'

let store: ReturnType<typeof useMemoryStore>
let scope: MemoryScope
const clients: ReturnType<typeof useMemoryStore>[] = []

function newClient() {
  const client = useMemoryStore(createPinia())
  clients.push(client)
  return client
}

beforeEach(() => {
  store = newClient()
  scope = { userId: crypto.randomUUID(), characterId: 'memory-test' }
  store.setEnabled(scope, false)
})

afterEach(async () => {
  vi.restoreAllMocks()
  await store.clear(scope)
  await storage.removeItem('outbox:memory-test-sentinel')
  for (const client of clients)
    client.$dispose()
  clients.length = 0
})

describe('local memory browser persistence', () => {
  it('requires consent and invalidates pending turns after memory is disabled', async () => {
    expect(store.beginTurn(scope, 'My home city is Kyoto.', 'old-session', 'turn-1', 1)).toBeUndefined()
    store.setEnabled(scope, true)
    const turn = store.beginTurn(scope, 'My home city is Kyoto.', 'old-session', 'turn-1', 1)
    expect(turn).toBeDefined()
    if (!turn)
      throw new Error('Expected a captured turn')

    await store.recallTurn(turn)
    store.setEnabled(scope, false)
    store.setEnabled(scope, true)
    await store.rememberTurn(turn)

    expect(await store.list(scope)).toEqual([])
    expect(await store.recallTurn(turn)).toBe('')
  })

  it('persists raw user evidence across clients and excludes the current session', async () => {
    store.setEnabled(scope, true)
    const turn = store.beginTurn(scope, 'My home city is Kyoto.', 'old-session', 'turn-1', 1)
    if (!turn)
      throw new Error('Expected a captured turn')
    await store.recallTurn(turn)
    await store.rememberTurn(turn)

    const reopened = newClient()
    reopened.setEnabled(scope, true)
    const query = reopened.beginTurn(scope, 'What is my home city?', 'new-session', 'turn-2', 2)
    if (!query)
      throw new Error('Expected a recall request')

    expect(await reopened.recallTurn(query)).toContain('Kyoto')
    expect(await reopened.recallTurn({ ...query, sessionId: 'old-session' })).toBe('')
    expect(await reopened.list({ ...scope, characterId: 'another-character' })).toEqual([])
    expect(await reopened.list({ ...scope, userId: 'another-user' })).toEqual([])
  })

  it('serializes independent writers and reloads stale indexes before each operation', async () => {
    // ROOT CAUSE:
    // Independent renderers retain different in-memory snapshots. A write from
    // a stale snapshot can erase another renderer's completed write. The Web
    // Lock covers reload and save together, rather than only the final save.
    const second = newClient()
    await Promise.all([
      store.remember(scope, { id: 'a', text: 'I live in Kyoto.' }),
      second.remember(scope, { id: 'b', text: 'I own a cat named Miso.' }),
    ])

    expect((await store.list(scope)).map(record => record.id).sort()).toEqual(['a', 'b'])
    await second.forget(scope, 'a')
    await store.remember(scope, { id: 'c', text: 'I enjoy hiking.' })
    expect((await second.list(scope)).map(record => record.id).sort()).toEqual(['b', 'c'])
  })

  it('supports fact replacement, edit, delete, export and validated import', async () => {
    await store.remember(scope, { text: 'My home city is Kyoto.', key: 'home-city' })
    const replacement = await store.remember(scope, { text: 'My home city is Osaka.', key: 'home-city', pinned: true })
    expect(await store.list(scope)).toHaveLength(1)
    await store.remember(scope, { id: replacement.id, text: 'My home city is Tokyo.', key: 'home-city', pinned: true })
    const exported = await store.exportMemories(scope)
    await store.forget(scope, replacement.id)
    expect(await store.list(scope)).toEqual([])
    expect(await store.importMemories(scope, exported)).toBe(1)
    expect((await store.list(scope))[0]?.text).toBe('My home city is Tokyo.')
    await expect(store.importMemories({ ...scope, userId: 'wrong-user' }, exported)).rejects.toThrow()
    await expect(store.importMemories(scope, '{"version": 1}')).rejects.toThrow()
    expect(await store.list(scope)).toHaveLength(1)
  })

  it('returns no evidence on storage failure and does not throw during automatic capture', async () => {
    store.setEnabled(scope, true)
    const turn = store.beginTurn(scope, 'Where is Kyoto?', 'session', 'turn', 1)
    if (!turn)
      throw new Error('Expected a captured turn')
    vi.spyOn(storage, 'getItemRaw').mockRejectedValue(new Error('IndexedDB unavailable'))

    expect(await store.recallTurn(turn)).toBe('')
    await expect(store.rememberTurn(turn)).resolves.toBeUndefined()
    expect(store.lastError).toContain('IndexedDB unavailable')
  })

  it('bounds chat recall time when another writer holds the scope lock', async () => {
    store.setEnabled(scope, true)
    const turn = store.beginTurn(scope, 'Where is Kyoto?', 'session', 'turn', 1)
    if (!turn)
      throw new Error('Expected a captured turn')
    let release: (() => void) | undefined
    let acquired: (() => void) | undefined
    const entered = new Promise<void>((resolve) => {
      acquired = resolve
    })
    const held = navigator.locks.request(`local:memory:v1:${encodeURIComponent(scope.userId)}:${encodeURIComponent(scope.characterId)}`, async () => {
      acquired?.()
      await new Promise<void>((resolve) => {
        release = resolve
      })
    })
    await entered
    try {
      expect(await store.recallTurn(turn)).toBe('')
      expect(store.lastError).toContain('250 ms')
    }
    finally {
      release?.()
      await held
    }
  })

  it('normalizes IndexedDB mount keys so clearing all memory preserves unrelated outbox data', async () => {
    // ROOT CAUSE:
    // unstorage 1.17.5 returns base-prefixed keys from its IndexedDB driver.
    // Mount filtering then sees no matching local keys, so clearAll silently
    // leaves records behind. The local driver now filters and strips its base.
    const otherScope = { ...scope, characterId: 'another-character' }
    store.setEnabled(scope, true)
    store.setEnabled(otherScope, true)
    await store.remember(scope, { text: 'Kyoto' })
    await store.remember(otherScope, { text: 'Osaka' })
    await storage.setItemRaw('outbox:memory-test-sentinel', 'Keep unrelated queued data')

    const localKeys = await storage.getKeys('local:memory:v1:')
    expect(localKeys).toContain(`local:memory:v1:${scope.userId}:${scope.characterId}`)
    expect(localKeys.some(key => key.includes('memory-test-sentinel'))).toBe(false)

    await store.clearAll()

    expect(store.isEnabled(scope)).toBe(false)
    expect(store.isEnabled(otherScope)).toBe(false)
    expect(await store.list(scope)).toEqual([])
    expect(await store.list(otherScope)).toEqual([])
    expect(await storage.getItemRaw('outbox:memory-test-sentinel')).toBe('Keep unrelated queued data')
  })

  it('does not restore a captured turn after another renderer clears the scope', async () => {
    store.setEnabled(scope, true)
    const second = newClient()
    const turn = second.beginTurn(scope, 'My home city is Kyoto.', 'session', 'pending-turn', 1)
    if (!turn)
      throw new Error('Expected a captured turn')
    await second.recallTurn(turn)

    await store.clear(scope)
    await second.rememberTurn(turn)

    expect(await store.list(scope)).toEqual([])
  })

  it('does not save a turn when its pre-send storage read did not complete', async () => {
    store.setEnabled(scope, true)
    const turn = store.beginTurn(scope, 'Kyoto', 'session', 'turn', 1)
    if (!turn)
      throw new Error('Expected a captured turn')

    await store.rememberTurn(turn)

    expect(await store.list(scope)).toEqual([])
  })
})
