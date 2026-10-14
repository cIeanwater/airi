/** A memory belongs to one local user and one character across chat sessions. */
export interface MemoryScope {
  userId: string
  characterId: string
}

/** Source references remain attached to evidence during retrieval and export. */
export interface MemorySource {
  sessionId?: string
  messageId?: string
  label?: string
}

/** User-authored evidence. A key replaces a known fact without semantic guessing. */
export interface MemoryInput {
  id?: string
  text: string
  /** @default 'note' */
  kind?: 'note' | 'turn'
  source?: MemorySource
  key?: string
  /** Pinned records do not expire through capacity eviction or recency ranking. @default false */
  pinned?: boolean
  /** Expiration is always enforced, including for pinned records. Milliseconds since Unix epoch. */
  expiresAt?: number
  createdAt?: number
}

/** The embedding model identity prevents comparisons between incompatible vector spaces. */
export interface MemoryEmbedding {
  model: string
  vector: number[]
}

/** Stored evidence. Edits preserve the creation time and replace the update time. */
export interface MemoryRecord extends MemoryInput {
  id: string
  kind: 'note' | 'turn'
  pinned: boolean
  createdAt: number
  updatedAt: number
  embedding?: MemoryEmbedding
}

/** Versioned export and storage format. The scope must match the receiving engine. */
export interface MemorySnapshot {
  version: 1
  scope: MemoryScope
  records: MemoryRecord[]
}

/**
 * The adapter must save a complete snapshot atomically or reject without changes.
 * One engine serializes writes. Hosts must coordinate writers across windows or processes.
 */
export interface MemoryStorage {
  load: () => Promise<MemorySnapshot | undefined>
  save: (snapshot: MemorySnapshot) => Promise<void>
}

/** Optional semantic retrieval. The host owns model loading, transport, and credentials. */
export interface EmbeddingProvider {
  /** Include the model revision and vector configuration in this identity. */
  id: string
  embed: (texts: string[], signal: AbortSignal) => Promise<number[][]>
}

/** Engine settings are independent of UI, storage drivers, and network clients. */
export interface MemoryOptions {
  scope: MemoryScope
  storage?: MemoryStorage
  embedding?: EmbeddingProvider
  /** Oldest unpinned evidence is evicted first. @default 5000 */
  maxRecords?: number
  now?: () => number
  /** Embedding errors and deadlines leave lexical retrieval available. @default 1500 */
  embeddingTimeoutMs?: number
}

/** Search budgets apply before any evidence enters the provider prompt. */
export interface MemorySearchOptions {
  /** @default 6 */
  limit?: number
  /** Sum of raw evidence characters, excluding prompt framing. @default 6000 */
  maxCharacters?: number
  excludeSessionId?: string
  now?: number
  /** Zero disables decay. Evergreen notes and pinned records never decay. @default 0 */
  halfLifeDays?: number
  /** MMR redundancy penalty, from zero to one. @default 0 */
  diversity?: number
  /** Minimum fused ranking score. This is not a calibrated confidence. @default 0 */
  minScore?: number
}

/** Scores rank evidence; they do not measure whether a statement is true. */
export interface MemorySearchResult {
  record: MemoryRecord
  score: number
  lexicalScore: number
  semanticScore: number
}
