import type { MemoryInput, MemoryRecord, MemoryScope, MemorySnapshot } from '@proj-airi/memory-local'

import { errorMessageFrom } from '@moeru/std'
import { chunkMemoryText, formatMemoryContext, MemoryEngine } from '@proj-airi/memory-local'
import { useLocalStorageManualReset } from '@proj-airi/stage-shared/composables'
import { defineStore } from 'pinia'
import { shallowRef, watch } from 'vue'

import { storage } from '../../database/storage'

/** A raw user turn bound to the scope and consent state at submission. */
export interface MemoryTurn {
  scope: MemoryScope
  text: string
  sessionId: string
  messageId: string
  createdAt: number
  consentGeneration: number
  /** Set only after a timely storage read. Clear operations rotate this durable generation. */
  storageGeneration?: string
}

/**
 * Owns local memory persistence and chat consent. The caller supplies the owning
 * user and character, so a queued turn never follows a later UI selection.
 * Web Locks serialize the complete read-modify-write operation across windows.
 * Only consent settings are shared through local storage. Engines and errors
 * belong to this renderer and never enter Pinia snapshot replication.
 */
export const useMemoryStore = defineStore('memory', () => {
  const enabledScopes = useLocalStorageManualReset<Record<string, boolean>>('settings/memory/enabled-scopes', {}, { flush: 'sync' })
  const lastError = shallowRef('')
  const engines = new Map<string, MemoryEngine>()
  let consentGeneration = 0

  watch(enabledScopes, () => {
    consentGeneration += 1
  }, { deep: true, flush: 'sync' })

  function isEnabled(scope: MemoryScope) {
    return enabledScopes.value[JSON.stringify([scope.userId, scope.characterId])] === true
  }

  /** Changes consent for one user and character without enabling other scopes. */
  function setEnabled(scope: MemoryScope, value: boolean) {
    enabledScopes.value = {
      ...enabledScopes.value,
      [JSON.stringify([scope.userId, scope.characterId])]: value,
    }
  }

  async function withEngine<T>(scope: MemoryScope, operation: (engine: MemoryEngine, key: string) => Promise<T>, signal?: AbortSignal): Promise<T> {
    // Use unstorage's normalized key form for both storage and Web Locks.
    const key = `local:memory:v1:${encodeURIComponent(scope.userId)}:${encodeURIComponent(scope.characterId)}`
    // A missing lock API is a storage error. An in-window mutex cannot protect
    // an IndexedDB snapshot from another tab or Electron renderer.
    if (typeof navigator === 'undefined' || !navigator.locks)
      throw new Error('Local memory needs a browser with Web Locks support')

    return navigator.locks.request(key, { signal }, async () => {
      let engine = engines.get(key)
      if (!engine) {
        engine = new MemoryEngine({
          scope,
          maxRecords: 5000,
          storage: {
            load: async () => await storage.getItemRaw<MemorySnapshot>(key) ?? undefined,
            save: snapshot => storage.setItemRaw(key, snapshot),
          },
        })
        engines.set(key, engine)
        await engine.ready()
      }
      else {
        // Another window can update storage while this renderer retains its index.
        await engine.reload()
      }

      const result = await operation(engine, key)
      lastError.value = ''
      return result
    })
  }

  function beginTurn(scope: MemoryScope, text: string, sessionId: string, messageId: string, createdAt: number): MemoryTurn | undefined {
    if (!isEnabled(scope) || !text.trim())
      return undefined

    return {
      scope: { ...scope },
      text: text.slice(0, 8000),
      sessionId,
      messageId,
      createdAt,
      consentGeneration,
    }
  }

  function canUseTurn(turn: MemoryTurn) {
    return isEnabled(turn.scope) && turn.consentGeneration === consentGeneration
  }

  /** Returns bounded evidence, or no evidence if storage is slow or unavailable. */
  async function recallTurn(turn: MemoryTurn): Promise<string> {
    if (!canUseTurn(turn))
      return ''

    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    try {
      const context = await Promise.race([
        withEngine(turn.scope, async (engine, key) => {
          const generationKey = key.replace('local:memory:', 'local:memory-control:')
          let generation = await storage.getItemRaw<string>(generationKey)
          if (!generation) {
            generation = crypto.randomUUID()
            await storage.setItemRaw(generationKey, generation)
          }
          const results = await engine.search(turn.text, {
            excludeSessionId: turn.sessionId,
            limit: 6,
            maxCharacters: 4000,
          })
          if (!controller.signal.aborted && canUseTurn(turn))
            turn.storageGeneration = generation
          return formatMemoryContext(results, { maxCharacters: 4000 })
        }, controller.signal),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => {
            const error = new Error('Memory retrieval exceeded 250 ms')
            controller.abort(error)
            reject(error)
          }, 250)
        }),
      ])
      // Disabling memory invalidates a request even if storage finishes later.
      return canUseTurn(turn) ? context : ''
    }
    catch (error) {
      lastError.value = errorMessageFrom(error) ?? 'Memory retrieval failed'
      // Memory is optional evidence. A storage failure must not stop chat.
      return ''
    }
    finally {
      clearTimeout(timer)
    }
  }

  /** Saves only the raw user text after the chat runtime reports a successful round. */
  async function rememberTurn(turn: MemoryTurn): Promise<void> {
    if (!canUseTurn(turn) || !turn.storageGeneration)
      return

    try {
      await withEngine(turn.scope, async (engine, key) => {
        if (!canUseTurn(turn))
          return

        const generationKey = key.replace('local:memory:', 'local:memory-control:')
        if (await storage.getItemRaw<string>(generationKey) !== turn.storageGeneration)
          return

        await engine.rememberMany(chunkMemoryText(turn.text).map((text, index) => ({
          id: `${turn.messageId}:${index}`,
          text,
          kind: 'turn',
          createdAt: turn.createdAt,
          source: { sessionId: turn.sessionId, messageId: turn.messageId, label: 'User message' },
        })))
      })
    }
    catch (error) {
      lastError.value = errorMessageFrom(error) ?? 'Memory storage failed'
    }
  }

  async function list(scope: MemoryScope): Promise<MemoryRecord[]> {
    return withEngine(scope, engine => engine.list())
  }

  async function search(scope: MemoryScope, query: string) {
    return withEngine(scope, engine => engine.search(query, { limit: 20, maxCharacters: 24000 }))
  }

  async function remember(scope: MemoryScope, input: MemoryInput) {
    return withEngine(scope, engine => engine.remember(input))
  }

  async function forget(scope: MemoryScope, id: string): Promise<void> {
    return withEngine(scope, engine => engine.forget(id))
  }

  async function clear(scope: MemoryScope): Promise<void> {
    consentGeneration += 1
    return withEngine(scope, async (engine, key) => {
      // Rotate before deletion. Even if deletion fails, earlier automatic
      // captures lose permission to restore data from another renderer.
      await storage.setItemRaw(key.replace('local:memory:', 'local:memory-control:'), crypto.randomUUID())
      await engine.clear()
    })
  }

  /** Revokes automatic capture and recall without deleting stored memories. */
  function resetState() {
    consentGeneration += 1
    enabledScopes.value = {}
    lastError.value = ''
  }

  /** Deletes local memory for every scope when the user clears all application data. */
  async function clearAll(): Promise<void> {
    resetState()
    const snapshots = await storage.getKeys('local:memory:v1:')
    const generations = await storage.getKeys('local:memory-control:v1:')
    const keys = new Set([
      ...snapshots,
      ...generations.map(key => key.replace('local:memory-control:', 'local:memory:')),
    ])
    for (const key of keys) {
      await navigator.locks.request(key, async () => {
        // Keep content-free tombstones. Deleting their generations would let a
        // captured turn in another window repopulate an empty memory scope.
        await storage.setItemRaw(key.replace('local:memory:', 'local:memory-control:'), crypto.randomUUID())
        await storage.removeItem(key)
        engines.delete(key)
      })
    }
  }

  async function exportMemories(scope: MemoryScope): Promise<string> {
    return withEngine(scope, engine => engine.export())
  }

  async function importMemories(scope: MemoryScope, json: string): Promise<number> {
    return withEngine(scope, engine => engine.import(json))
  }

  return {
    enabledScopes,
    isEnabled,
    setEnabled,
    lastError,
    beginTurn,
    recallTurn,
    rememberTurn,
    list,
    search,
    remember,
    forget,
    clear,
    clearAll,
    resetState,
    exportMemories,
    importMemories,
  }
})
