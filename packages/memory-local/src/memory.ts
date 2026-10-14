import type { MemoryEmbedding, MemoryInput, MemoryOptions, MemoryRecord, MemorySearchOptions, MemorySearchResult, MemorySnapshot } from './types'

import MiniSearch from 'minisearch'

import { parseInput, parseScope, parseSnapshot } from './schema'
import { tokenize } from './text'

function cosine(a: number[], b: number[]): number {
  if (a.length !== b.length || !a.length)
    return 0
  let dot = 0
  let normA = 0
  let normB = 0
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i]
    normA += a[i] ** 2
    normB += b[i] ** 2
  }
  return normA && normB ? dot / Math.sqrt(normA * normB) : 0
}

function overlap(a: Set<string>, b: Set<string>): number {
  let common = 0
  for (const token of a) {
    if (b.has(token))
      common++
  }
  return common / Math.max(1, a.size + b.size - common)
}

function integer(value: number, min: number, max: number, name: string): number {
  if (!Number.isInteger(value) || value < min || value > max)
    throw new Error(`Invalid memory ${name}`)
  return value
}

/**
 * Owns evidence, an incremental lexical index, and bounded query-vector caches for one scope.
 * Writes are serialized and commit to storage before the live index changes.
 * The host coordinates other engines that share the same storage, then calls reload().
 */
export class MemoryEngine {
  private readonly options: MemoryOptions
  private readonly index = new MiniSearch<MemoryRecord>({
    fields: ['text'],
    tokenize,
    processTerm: term => term,
    // Exact token search protects names and identifiers from fuzzy false positives.
    searchOptions: { combineWith: 'OR', prefix: false, fuzzy: false },
    autoVacuum: false,
  })

  private records = new Map<string, MemoryRecord>()
  private recordTerms = new Map<string, Set<string>>()
  private initialized?: Promise<void>
  private pending: Promise<void> = Promise.resolve()
  private snapshotText = ''
  private queryVectors = new Map<string, number[]>()
  private embeddingState: 'disabled' | 'ready' | 'degraded'
  private embeddingFailures = 0

  constructor(options: MemoryOptions) {
    this.options = {
      ...options,
      scope: parseScope(options.scope),
      maxRecords: integer(options.maxRecords ?? 5000, 1, 100000, 'capacity'),
      embeddingTimeoutMs: integer(options.embeddingTimeoutMs ?? 1500, 1, 60000, 'embedding timeout'),
      now: options.now ?? Date.now,
    }
    this.embeddingState = options.embedding ? 'ready' : 'disabled'
  }

  /** Contains no evidence text, queries, or credentials. */
  get diagnostics() {
    return { embeddingStatus: this.embeddingState, embeddingFailures: this.embeddingFailures, records: this.records.size }
  }

  /** Loads storage once. A rejected load can be retried without replacing stored data. */
  async ready(): Promise<void> {
    if (!this.initialized) {
      this.initialized = this.load().catch((error) => {
        this.initialized = undefined
        throw error
      })
    }
    await this.initialized
  }

  private async load() {
    const value = await this.options.storage?.load()
    const snapshot: MemorySnapshot = value === undefined
      ? { version: 1, scope: this.options.scope, records: [] }
      : parseSnapshot(value, this.options.scope)
    this.install(snapshot)
  }

  private install(snapshot: MemorySnapshot) {
    const serialized = JSON.stringify(snapshot)
    if (serialized === this.snapshotText)
      return
    const next = new Map(snapshot.records.map(record => [record.id, record]))
    // Index only changed evidence. Reloads of an unchanged persisted snapshot are no-ops.
    for (const [id, previous] of this.records) {
      const replacement = next.get(id)
      if (!replacement || replacement.text !== previous.text) {
        this.index.remove(previous)
        this.recordTerms.delete(id)
      }
    }
    for (const [id, record] of next) {
      const previous = this.records.get(id)
      if (!previous || previous.text !== record.text) {
        this.index.add(record)
        this.recordTerms.set(id, new Set(tokenize(record.text)))
      }
    }
    this.records = next
    this.snapshotText = serialized
    this.queryVectors.clear()
  }

  private write<T>(operation: () => Promise<T>): Promise<T> {
    const task = this.pending.then(async () => {
      await this.ready()
      return operation()
    })
    // Failed operations do not poison the write queue; each caller still receives its error.
    this.pending = task.then(() => {}, () => {})
    return task
  }

  private async commit(records: MemoryRecord[]) {
    const snapshot = { version: 1 as const, scope: this.options.scope, records }
    await this.options.storage?.save(structuredClone(snapshot))
    this.install(snapshot)
  }

  private retain(records: MemoryRecord[], now: number) {
    const live = records.filter(record => record.expiresAt === undefined || record.expiresAt > now)
    const capacity = this.options.maxRecords!
    if (live.length <= capacity)
      return live
    const pinned = live.filter(record => record.pinned)
    if (pinned.length > capacity)
      throw new Error('Pinned memory exceeds the storage capacity')
    const recent = live.filter(record => !record.pinned).sort((a, b) => b.updatedAt - a.updatedAt || b.createdAt - a.createdAt)
    return [...pinned, ...recent.slice(0, capacity - pinned.length)]
  }

  /** Refreshes from storage. The host must hold its shared writer lock during mutations. */
  async reload(): Promise<void> {
    await this.write(() => this.load())
  }

  private async embed(texts: string[]): Promise<number[][] | undefined> {
    const provider = this.options.embedding
    if (!provider)
      return undefined
    const controller = new AbortController()
    let timeout: ReturnType<typeof setTimeout> | undefined
    try {
      const vectors = await Promise.race([
        provider.embed(texts, controller.signal),
        new Promise<never>((_, reject) => {
          timeout = setTimeout(() => {
            controller.abort()
            reject(new Error('Memory embedding deadline exceeded'))
          }, this.options.embeddingTimeoutMs)
        }),
      ])
      const dimensions = vectors[0]?.length
      if (vectors.length !== texts.length || !dimensions || dimensions > 8192
        || vectors.some(vector => vector.length !== dimensions || vector.some(value => !Number.isFinite(value)) || !vector.some(value => value !== 0))) {
        throw new Error('Memory embedding response contains invalid vectors')
      }
      this.embeddingState = 'ready'
      return vectors
    }
    catch {
      // Evidence remains usable through the local index. Diagnostics expose degraded retrieval.
      this.embeddingState = 'degraded'
      this.embeddingFailures++
      return undefined
    }
    finally {
      clearTimeout(timeout)
    }
  }

  /** Adds raw evidence or replaces the record with the same explicit ID or fact key. */
  async remember(input: MemoryInput): Promise<MemoryRecord> {
    const records = await this.rememberMany([input])
    return records[0]
  }

  /** Batches ingestion into one storage commit. Equal texts with different sources remain distinct. */
  async rememberMany(inputs: MemoryInput[]): Promise<MemoryRecord[]> {
    const parsed = inputs.map(input => parseInput(input))
    return this.write(async () => {
      const next = new Map(this.records)
      const changed: MemoryRecord[] = []
      const now = this.options.now!()
      for (const input of parsed) {
        const keyed = input.key === undefined ? undefined : [...next.values()].find(record => record.key === input.key)
        if (keyed && input.id !== undefined && input.id !== keyed.id)
          throw new Error('Memory ID conflicts with an existing fact key')
        const id = input.id ?? keyed?.id ?? globalThis.crypto.randomUUID()
        const previous = next.get(id)
        const record: MemoryRecord = {
          ...previous,
          ...input,
          id,
          text: input.text,
          kind: input.kind ?? previous?.kind ?? 'note',
          pinned: input.pinned ?? previous?.pinned ?? false,
          createdAt: previous?.createdAt ?? input.createdAt ?? now,
          updatedAt: now,
        }
        if (previous?.text !== record.text)
          delete record.embedding
        next.set(id, record)
        changed.push(record)
      }
      // Embed only new or changed content. Persisted model identities make reuse explicit.
      const needsEmbedding = [...new Map(changed.map(record => [record.id, record])).values()]
        .filter(record => this.options.embedding && record.embedding?.model !== this.options.embedding.id)
      if (needsEmbedding.length) {
        const vectors = await this.embed(needsEmbedding.map(record => record.text))
        if (vectors) {
          needsEmbedding.forEach((record, index) => {
            record.embedding = { model: this.options.embedding!.id, vector: [...vectors[index]] }
          })
        }
      }
      await this.commit(this.retain([...next.values()], now))
      return structuredClone(changed)
    })
  }

  /** Deletes evidence and derived indexes. It does not change the source chat history. */
  async forget(id: string): Promise<void> {
    await this.write(async () => this.commit([...this.records.values()].filter(record => record.id !== id)))
  }

  /** Deletes every record in this engine's scope. */
  async clear(): Promise<void> {
    await this.write(async () => this.commit([]))
  }

  /** Returns detached, live records. Call reload() first when another host can write storage. */
  async list(): Promise<MemoryRecord[]> {
    await this.ready()
    await this.pending
    const now = this.options.now!()
    return structuredClone([...this.records.values()]
      .filter(record => record.expiresAt === undefined || record.expiresAt > now)
      .sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.updatedAt - a.updatedAt))
  }

  /** Exports live evidence only. The receiving engine must have the same user and character scope. */
  async export(): Promise<string> {
    return JSON.stringify({ version: 1, scope: this.options.scope, records: await this.list() }, null, 2)
  }

  /** Validates and merges a scoped export in one commit. Does not call an embedding provider. */
  async import(json: string): Promise<number> {
    if (json.length > 64 * 1024 * 1024)
      throw new Error('Memory import exceeds 64 MiB')
    const snapshot = parseSnapshot(JSON.parse(json), this.options.scope)
    return this.write(async () => {
      const merged = new Map(this.records)
      for (const record of snapshot.records)
        merged.set(record.id, record)
      const candidate = parseSnapshot({ ...snapshot, records: [...merged.values()] }, this.options.scope)
      await this.commit(this.retain(candidate.records, this.options.now!()))
      return snapshot.records.length
    })
  }

  /**
   * Retrieves lexical and optional vector candidates, fuses ranks, then removes redundant evidence.
   * No query expansion or LLM generation runs on this path. Empty lexical matches abstain.
   */
  async search(query: string, options: MemorySearchOptions = {}): Promise<MemorySearchResult[]> {
    const limit = integer(options.limit ?? 6, 0, 100, 'result limit')
    const budget = integer(options.maxCharacters ?? 6000, 0, 1000000, 'search budget')
    const diversity = options.diversity ?? 0
    const halfLifeDays = options.halfLifeDays ?? 0
    const minScore = options.minScore ?? 0
    if (!Number.isFinite(diversity) || diversity < 0 || diversity > 1 || !Number.isFinite(halfLifeDays) || halfLifeDays < 0 || !Number.isFinite(minScore) || minScore < 0)
      throw new Error('Invalid memory ranking settings')
    await this.ready()
    await this.pending
    if (!limit || !budget || !query.trim())
      return []
    const revision = this.snapshotText
    const now = options.now ?? this.options.now!()
    const live = (record: MemoryRecord) => (record.expiresAt === undefined || record.expiresAt > now)
      && (options.excludeSessionId === undefined || record.source?.sessionId !== options.excludeSessionId)
    const candidateLimit = Math.max(48, limit * 8)
    const lexical = this.index.search(query.slice(0, 16000), { filter: (result) => {
      const record = this.records.get(String(result.id))
      return record !== undefined && live(record)
    } }).slice(0, candidateLimit)
    let queryVector = this.queryVectors.get(query)
    if (this.options.embedding && !queryVector) {
      queryVector = (await this.embed([query.slice(0, 16000)]))?.[0]
      // A concurrent edit or deletion invalidates candidates created before the provider call.
      // Do not return old evidence or cache a query from a cleared scope.
      if (revision !== this.snapshotText)
        return []
      if (queryVector) {
        if (this.queryVectors.size >= 64)
          this.queryVectors.delete(this.queryVectors.keys().next().value!)
        this.queryVectors.set(query, queryVector)
      }
    }
    const semantic: { id: string, score: number }[] = []
    if (queryVector) {
      for (const record of this.records.values()) {
        const embedding: MemoryEmbedding | undefined = record.embedding
        if (!live(record) || embedding?.model !== this.options.embedding?.id || !embedding)
          continue
        const score = cosine(queryVector, embedding.vector)
        if (score >= 0.3)
          semantic.push({ id: record.id, score })
      }
      semantic.sort((a, b) => b.score - a.score)
    }
    const candidates = new Map<string, MemorySearchResult>()
    const merge = (id: string, rank: number, rawScore: number, kind: 'lexicalScore' | 'semanticScore') => {
      const record = this.records.get(id)!
      const hit = candidates.get(id) ?? { record, score: 0, lexicalScore: 0, semanticScore: 0 }
      // Reciprocal rank fusion avoids mixing uncalibrated BM25 and cosine score scales.
      hit.score += 1 / (60 + rank + 1)
      hit[kind] = rawScore
      candidates.set(id, hit)
    }
    lexical.forEach((result, rank) => merge(String(result.id), rank, result.score, 'lexicalScore'))
    semantic.slice(0, candidateLimit).forEach((result, rank) => merge(result.id, rank, result.score, 'semanticScore'))
    for (const hit of candidates.values()) {
      if (halfLifeDays && hit.record.kind === 'turn' && !hit.record.pinned)
        hit.score *= 2 ** (-Math.max(0, now - hit.record.createdAt) / (halfLifeDays * 86400000))
    }
    const pool = [...candidates.values()].filter(hit => hit.score >= minScore).sort((a, b) => b.score - a.score || b.record.updatedAt - a.record.updatedAt || a.record.id.localeCompare(b.record.id))
    // Ranked retrieval without diversity needs no pairwise similarity calculations.
    if (diversity === 0) {
      const selected: MemorySearchResult[] = []
      let characters = 0
      for (const hit of pool) {
        if (characters + hit.record.text.length > budget)
          continue
        selected.push(hit)
        characters += hit.record.text.length
        if (selected.length === limit)
          break
      }
      return structuredClone(selected)
    }
    const maximumScore = pool[0]?.score ?? 1
    const selected: MemorySearchResult[] = []
    let characters = 0
    while (selected.length < limit && pool.length) {
      let best = -1
      let bestScore = -Infinity
      pool.forEach((hit, index) => {
        if (characters + hit.record.text.length > budget)
          return
        let redundancy = 0
        for (const chosen of selected)
          redundancy = Math.max(redundancy, overlap(this.recordTerms.get(hit.record.id)!, this.recordTerms.get(chosen.record.id)!))
        const score = (1 - diversity) * hit.score / maximumScore - diversity * redundancy
        if (score > bestScore) {
          bestScore = score
          best = index
        }
      })
      if (best < 0)
        break
      const [hit] = pool.splice(best, 1)
      selected.push(hit)
      characters += hit.record.text.length
    }
    return structuredClone(selected)
  }
}
