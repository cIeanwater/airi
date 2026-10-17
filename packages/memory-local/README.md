# Local memory

`@proj-airi/memory-local` stores and retrieves evidence across conversations for one user and character. The core has no Vue, Pinia, Electron, server, or network dependency. The AIRI adapter persists scoped snapshots in IndexedDB.

Use this package for explicit notes, preferences, and relevant excerpts from earlier conversations. It supports editing, deletion, import, export, expiration, and optional embeddings. It does not infer facts, summarize conversations, resolve contradictions automatically, or replace chat history.

## Use in AIRI

Open **Settings → Modules → Long-Term Memory**, at `/settings/modules/memory-long-term`. Enable memory to allow automatic capture and recall for the displayed user and character. Each scope is disabled by default. Enabling one scope does not enable another account or character.

The page supports search, manual notes, fact keys, editing, pinning, deletion, and JSON import/export. The displayed user and character identify the active scope. Changing the account or character clears the previous scope from the page.

Automatic capture stores only user text after a successful chat round. Failed and canceled rounds do not enter memory. Each turn is limited to its first 8,000 UTF-16 code units, then divided into overlapping chunks. Assistant answers and earlier chat history are not scanned automatically.

Before a response, AIRI retrieves evidence from other sessions in the same scope. The active session is excluded because it already contributes to the chat context. Retrieval has a 250 ms deadline and a final 4,000-character prompt budget. If retrieval fails or exceeds the deadline, chat continues without memory evidence.

The target session supplies the user and character scope. Selecting another character does not change a background request's scope. Missing or foreign session metadata prevents memory access. An account change during recall also discards the previous account's evidence and automatic capture.

A timely storage read also records the deletion generation for that turn. If recall fails or times out, automatic capture skips the turn. This rule prevents a delayed response from restoring evidence after a clear operation.

## Architecture

```text
successful user turn / manual note
  -> scoped AIRI adapter and Web Lock
  -> MemoryEngine
     -> atomic snapshot save
     -> incremental MiniSearch index
     -> optional stored embedding vectors

new user message
  -> scope and session filters
  -> exact lexical search + optional vector search
  -> reciprocal rank fusion
  -> optional recency decay and diversity ordering
  -> escaped JSON evidence within a character budget
  -> active chat provider
```

MiniSearch provides an in-process BM25 index. Queries use exact tokens without fuzzy or prefix expansion. Tokenization preserves Latin identifiers and supports Chinese, Japanese, and Korean character pairs. English names within Chinese text remain searchable. This support does not establish retrieval accuracy for every language.

With an embedding provider, the engine combines lexical and semantic ranks through reciprocal rank fusion. It does not mix raw BM25 and cosine scores. Optional MMR ordering reduces repeated evidence through token overlap. The default `diversity: 0` skips both the penalty and its overlap calculations. The benchmark guide documents the measured tradeoff behind this default.

Age decay is disabled by default. With a positive `halfLifeDays`, old conversation turns lose ranking weight. Manual notes and pinned records do not decay. Expiration still applies to every record.

The index changes only for added, edited, or removed text. Unchanged storage reloads keep the existing index. The engine batches ingestion into one save, reuses stored vectors, and caches up to 64 query vectors. Mutations invalidate query caches. A concurrent mutation also invalidates a search that is waiting for embeddings.

Persistence still writes complete snapshots. Optional vector search scans stored vectors. The default capacity is 5,000 records, so this package targets bounded personal memory. It does not provide an approximate vector index for large document collections.

### Relationship to OpenClaw

OpenClaw combines keyword and vector retrieval with deterministic ranking. This design adopts that separation of evidence, indexes, and retrieval policy. It is an independent AIRI implementation. See the [OpenClaw memory search documentation](https://docs.openclaw.ai/concepts/memory-search).

| Area | OpenClaw reference | AIRI adaptation |
| --- | --- | --- |
| Evidence storage | Markdown memory files | Versioned JSON records with user and character scope |
| Search index | SQLite BM25 and vector support | In-process MiniSearch and optional vectors |
| Hybrid merge | Weighted retrieval scores | Reciprocal rank fusion |
| Retrieval policy | Recency and MMR ranking | Configurable diversity, no default age decay |
| Host integration | Agent workspace and memory tools | IndexedDB adapter, chat hook, and settings page |

OpenClaw documents Markdown as its memory source and SQLite as its builtin index. AIRI does not require either format. See [Memory overview](https://docs.openclaw.ai/concepts/memory) and [Builtin memory engine](https://docs.openclaw.ai/concepts/memory-builtin).

## Public API

```ts
import { formatMemoryContext, MemoryEngine } from '@proj-airi/memory-local'

const memory = new MemoryEngine({
  scope: { userId: 'local-user', characterId: 'airi' },
})

await memory.remember({
  key: 'home-city',
  text: 'I live in Berlin.',
  source: { sessionId: 'session-1', messageId: 'message-1' },
})

const results = await memory.search('Berlin', {
  excludeSessionId: 'session-2',
  limit: 6,
  maxCharacters: 4000,
})
const evidence = formatMemoryContext(results, { maxCharacters: 4000 })

await memory.remember({ key: 'home-city', text: 'I now live in Madrid.' })
const backup = await memory.export()
```

This example uses transient storage. For persistence, supply a `MemoryStorage` adapter to the constructor. The AIRI adapter already connects this boundary to IndexedDB.

| API | Contract |
| --- | --- |
| `ready()` | Loads and validates storage once. A failed load can be retried. |
| `reload()` | Refreshes state after another writer changes storage. |
| `remember(input)` | Adds evidence or updates an explicit ID or fact key. |
| `rememberMany(inputs)` | Validates a batch and commits it through one storage save. |
| `search(query, options)` | Returns records with fused, lexical, and semantic scores. |
| `list()` | Returns detached, unexpired records. Pinned records appear first. |
| `forget(id)` | Deletes one record and its derived search state. |
| `clear()` | Deletes every record in the current scope. |
| `export()` | Returns unexpired records as a versioned JSON string. |
| `import(json)` | Validates and merges a same-scope export. Returns the imported record count. |
| `diagnostics` | Reports record count, embedding status, and embedding failure count. |
| `chunkMemoryText(text, options)` | Splits text without summaries. Defaults to 1,200 characters with 120-character overlap. |
| `formatMemoryContext(results, options)` | Formats attributed evidence as escaped JSON within a complete prompt budget. |

All engine methods in the table are asynchronous. `diagnostics` is a synchronous property. The two formatting helpers are synchronous functions. Public contracts are exported from the package entry point and defined in [types.ts](src/types.ts).

Search defaults to six results and 6,000 raw evidence characters. The formatter separately budgets its framing, source metadata, and escaped content. It skips records that do not fit instead of returning partial JSON. An empty query or an absent lexical match returns no evidence unless semantic retrieval finds a match.

## Storage and data management

`MemoryStorage.load()` returns a snapshot or `undefined`. `save(snapshot)` must atomically replace the complete snapshot or reject without changes. The engine updates live records and indexes only after a successful save. A failed write preserves the previous state and does not block later writes.

One engine serializes its own writes. Separate engines need a shared host lock around reload, mutation, and save. The AIRI adapter uses Web Locks across browser tabs and Electron renderers. If Web Locks is unavailable, the adapter reports a storage error. It does not attempt unsafe concurrent snapshot writes.

Clearing a scope rotates a persistent deletion generation under the same lock. Queued captures must match the current generation before they can write. This prevents turns from another window from restoring cleared evidence. The application-wide **Delete all data** action also clears memory for every scope and revokes automatic memory consent.

Clear operations retain content-free generation markers in IndexedDB. Their keys still identify the user and character, but they contain no evidence text. They prevent a delayed capture from treating a deleted scope as a new writable scope.

Every snapshot contains `version: 1`, `scope`, and `records`. Imports reject a different user or character, malformed records, duplicate IDs, and duplicate fact keys. They merge records by ID. An imported matching ID replaces the existing record. Other existing records remain. A fact-key collision between different IDs rejects the entire import.

Imports do not generate embeddings. Model-mismatched vectors are ignored for semantic search, but their text remains searchable. Reingestion with a configured provider can produce current vectors.

Explicit IDs or fact keys provide correction semantics. The engine does not guess that two different statements contradict each other. Edits preserve creation time and update modification time.

The default 5,000-record capacity removes the oldest unpinned records first. Pinning protects against capacity eviction, but does not override expiration. A write fails if pinned records alone exceed capacity. Batch return values describe changed records before retention. Use `list()` to inspect the retained state.

## Optional embeddings

The constructor accepts an `EmbeddingProvider` with an `id` and `embed(texts, signal)` method. The host owns model loading, request transport, credentials, and batching limits. Its ID must identify the model revision and vector configuration.

The engine embeds changed records and stores their vectors with the model ID. It rejects invalid vectors and ignores incompatible model identities or dimensions during search. Provider errors and the default 1,500 ms deadline leave lexical retrieval available. `diagnostics.embeddingStatus` and `embeddingFailures` expose degraded operation without evidence text or credentials.

The AIRI settings integration currently uses lexical retrieval only. It does not configure an embedding endpoint or download a model. The benchmark results do not measure semantic retrieval with real models.

## Privacy and limitations

- Memory is opt-in for each user and character. Disabling a scope stops automatic capture and recall but keeps saved records available for management.
- Scope separates users and characters inside the application. Local storage is unencrypted and is not an authentication boundary against access to the browser profile.
- Retrieval runs locally by default. Retrieved evidence enters the active chat request and reaches the selected chat provider. The complete chat flow is not necessarily on-device.
- A remote embedding adapter can send evidence and queries to its provider. This requires a separate host implementation.
- Deleting memory does not delete chat history. Ordinary chat deletion does not delete memory. The application-wide **Delete all data** action clears both. JSON exports are separate copies that also require protection or deletion.
- Record IDs and source references help inspection. They do not prove truth or make imported content trustworthy.
- Escaped JSON prevents fake delimiter boundaries. The prompt treats all memory as untrusted evidence. This does not guarantee resistance to every prompt injection.
- Ranking scores are not calibrated confidence. Missing lexical overlap, indirect references, conflicting facts, and multi-session reasoning can reduce recall.
- There is no automatic history backfill, cloud memory synchronization, learned reranker, or LLM fact extraction in this package.

## Evaluation

[LongMemEval](https://arxiv.org/abs/2410.10813) separates long-term memory into indexing, retrieval, and reading. Its questions cover extraction, reasoning across sessions, time, updates, and abstention. The local runner evaluates retrieval using the cleaned [Hugging Face dataset](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned).

The full S benchmark selects all 500 questions. Evidence recall uses 470 answerable questions and excludes 30 abstention questions. It compares lexical retrieval with a diversity variant. It does not measure answer generation or calibrated abstention.

See the [benchmark guide](benchmarks/README.md) for commands, pinned data, metrics, and protocol limits. Machine-readable reports cover the [research budget](benchmarks/reports/longmemeval-s.json) and [application budget after formatting](benchmarks/reports/longmemeval-s-app.json). Both record configuration, timing, results, and engine source hashes.

The benchmark indexes user and assistant history, while automatic AIRI capture stores user text only. Query timings exclude persistence, network access, model calls, and answer generation. The English benchmark does not establish Chinese retrieval quality. A session hit can miss the required turn, and a turn hit can miss the answer span.

## Integration and merge boundaries

The core is contained in `packages/memory-local`. AIRI integration uses these existing boundaries:

| Location | Responsibility |
| --- | --- |
| `packages/stage-ui/src/stores/modules/memory.ts` | Consent, scope, IndexedDB persistence, Web Locks, capture, and recall |
| `packages/stage-ui/src/stores/chat.ts` | Records successful user turns and adds evidence to provider requests |
| `packages/stage-ui/src/composables/use-data-maintenance.ts` | Revokes memory consent and clears all memory with application data |
| `packages/stage-ui/src/composables/use-modules-list.ts` | Shows enabled status for the selected user and character |
| `packages/stage-ui/src/database/storage.ts` | Keeps IndexedDB key enumeration compatible with scoped deletion |
| `packages/stage-pages/src/pages/settings/modules/memory-long-term.vue` | Memory management page |
| `packages/i18n/src/locales/en/settings.yaml` and `zh-Hans/settings.yaml` | Interface text |
| Stage UI and stage pages package manifests | Workspace dependency on `@proj-airi/memory-local` |
| Root `vitest.config.ts` | Registers the core test project |
| Root `eslint.config.ts` | Excludes downloaded benchmark data and generated reports from source linting |
| Root `pnpm-lock.yaml` | Records the package and workspace dependencies |

The integration reuses the existing IndexedDB storage module. It adds no server migration, chat-history schema change, or native database dependency. The package can be reused without the AIRI adapter.

## Development

From the repository root, run the package checks:

```sh
pnpm -F @proj-airi/memory-local test
pnpm -F @proj-airi/memory-local typecheck
pnpm -F @proj-airi/memory-local build
```

The tests cover persistence, scope isolation, atomic failure, concurrent writes, cache invalidation, multilingual tokens, expiration, prompt boundaries, and embedding failure. Adapter tests use a real browser for IndexedDB and Web Locks.

See [Validation](VALIDATION.md) for the recorded checks and unrelated repository failures from this implementation run.
