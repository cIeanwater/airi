/* eslint-disable no-console */

import type { MemoryInput, MemoryRecord } from '../src'

import process from 'node:process'

import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { cpus, totalmem } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'

import { chunkMemoryText, formatMemoryContext, MemoryEngine } from '../src'

interface Question {
  question_id: string
  question_type: string
  question: string
  question_date: string
  haystack_session_ids: string[]
  haystack_dates: string[]
  haystack_sessions: { role: string, content: string, has_answer?: boolean }[][]
  answer_session_ids: string[]
}

interface Manifest {
  repository: string
  revision: string
  license: string
  paper: string
  source: string
  splits: Record<string, { filename: string, bytes: number, sha256: string, purpose: string }>
}

interface Scores {
  sessionRecall: number
  sessionHit: number
  sessionComplete: number
  sessionMrr: number
  turnRecall: number
  turnHit: number
  turnComplete: number
  turnMrr: number
  characters: number
}

interface Measurement {
  searchMs: number
  formatMs: number
  formattedContextCharacters?: number
  returnedChunks: number
  at: Record<string, Scores>
  ranked: { id: string, sessionId: string, messageId: string }[]
}

interface QueryResult {
  questionId: string
  questionType: string
  sessions: number
  indexedChunks: number
  indexedCharacters: number
  evidenceSessions: number
  evidenceTurns: number
  ingestMs: number
  methods: Record<string, Measurement>
}

const root = fileURLToPath(new URL('.', import.meta.url))
const chunkSettings = { maxCharacters: 1200, overlap: 120 }

function readPositiveInteger(value: string | undefined, fallback: number, allowZero = false) {
  if (value === undefined)
    return fallback
  const number = Number(value)
  if (!Number.isSafeInteger(number) || number < (allowZero ? 0 : 1))
    throw new Error(`Expected ${allowZero ? 'a non-negative' : 'a positive'} integer, received ${value}`)
  return number
}

function parseDate(value: string) {
  const match = /^(\d{4})\/(\d{2})\/(\d{2}) \([A-Z]{3}\) (\d{2}):(\d{2})$/i.exec(value)
  if (!match)
    throw new Error(`Unexpected LongMemEval date: ${value}`)
  // The release has no time-zone field. UTC preserves its relative chronology across hosts.
  return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4]), Number(match[5]))
}

function importHistory(question: Question) {
  if (question.haystack_sessions.length !== question.haystack_session_ids.length
    || question.haystack_sessions.length !== question.haystack_dates.length) {
    throw new Error(`Misaligned history arrays for ${question.question_id}`)
  }
  const records: MemoryInput[] = []
  const evidenceTurns = new Set<string>()
  const evidenceSessions = new Set(question.answer_session_ids)
  for (const sessionId of evidenceSessions) {
    if (!question.haystack_session_ids.includes(sessionId))
      throw new Error(`Missing supporting session for ${question.question_id}: ${sessionId}`)
  }
  for (let sessionIndex = 0; sessionIndex < question.haystack_sessions.length; sessionIndex++) {
    const sessionId = question.haystack_session_ids[sessionIndex]
    const createdAt = parseDate(question.haystack_dates[sessionIndex])
    const session = question.haystack_sessions[sessionIndex]
    for (let turnIndex = 0; turnIndex < session.length; turnIndex++) {
      const turn = session[turnIndex]
      const messageId = `${sessionIndex}:${turnIndex}`
      if (turn.has_answer)
        evidenceTurns.add(messageId)
      // Only dialogue content enters the index. Questions, answers, and evidence labels never do.
      const chunks = chunkMemoryText(`${turn.role}: ${turn.content}`, chunkSettings)
      for (let chunkIndex = 0; chunkIndex < chunks.length; chunkIndex++) {
        records.push({
          id: `${messageId}:${chunkIndex}`,
          text: chunks[chunkIndex],
          kind: 'turn',
          source: { sessionId, messageId },
          createdAt,
        })
      }
    }
  }
  if (!evidenceTurns.size || !evidenceSessions.size)
    throw new Error(`Missing evidence labels for answerable question ${question.question_id}`)
  return { records, evidenceTurns, evidenceSessions }
}

function score(records: MemoryRecord[], evidenceSessions: Set<string>, evidenceTurns: Set<string>): Scores {
  const sessions = new Set<string>()
  const turns = new Set<string>()
  let firstSession = 0
  let firstTurn = 0
  let characters = 0
  for (let index = 0; index < records.length; index++) {
    const record = records[index]
    characters += record.text.length
    if (record.source?.sessionId && evidenceSessions.has(record.source.sessionId)) {
      sessions.add(record.source.sessionId)
      firstSession ||= index + 1
    }
    if (record.source?.messageId && evidenceTurns.has(record.source.messageId)) {
      turns.add(record.source.messageId)
      firstTurn ||= index + 1
    }
  }
  return {
    sessionRecall: sessions.size / evidenceSessions.size,
    sessionHit: Number(sessions.size > 0),
    sessionComplete: Number(sessions.size === evidenceSessions.size),
    sessionMrr: firstSession ? 1 / firstSession : 0,
    turnRecall: turns.size / evidenceTurns.size,
    turnHit: Number(turns.size > 0),
    turnComplete: Number(turns.size === evidenceTurns.size),
    turnMrr: firstTurn ? 1 / firstTurn : 0,
    characters,
  }
}

function measure(records: MemoryRecord[], searchMs: number, evidenceSessions: Set<string>, evidenceTurns: Set<string>, cutoffs: number[], contextBudget?: number): Measurement {
  let selected = records
  let formattedContextCharacters: number | undefined
  let formatMs = 0
  if (contextBudget !== undefined) {
    const formatStart = performance.now()
    const context = formatMemoryContext(records.map(record => ({ record, score: 0, lexicalScore: 0, semanticScore: 0 })), { maxCharacters: contextBudget })
    formatMs = performance.now() - formatStart
    formattedContextCharacters = context.length
    // Measure only evidence that survives the public formatter's total budget, including JSON framing.
    const includedIds = new Set(context.split('\n').filter(line => line.startsWith('{')).map((line) => {
      const item: { id: string } = JSON.parse(line)
      return item.id
    }))
    selected = records.filter(record => includedIds.has(record.id))
  }
  return {
    searchMs,
    formatMs,
    formattedContextCharacters,
    returnedChunks: selected.length,
    at: Object.fromEntries(cutoffs.map(cutoff => [cutoff, score(selected.slice(0, cutoff), evidenceSessions, evidenceTurns)])),
    ranked: selected.map((record) => {
      if (!record.source?.sessionId || !record.source.messageId)
        throw new Error(`Retrieved chunk lost its source: ${record.id}`)
      return { id: record.id, sessionId: record.source.sessionId, messageId: record.source.messageId }
    }),
  }
}

function percentile(values: number[], fraction: number) {
  if (!values.length)
    throw new Error('Cannot measure an empty sample')
  const sorted = [...values].sort((left, right) => left - right)
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)]
}

function mean(values: number[]) {
  return values.reduce((sum, value) => sum + value, 0) / values.length
}

function aggregate(queries: QueryResult[], cutoffs: number[]) {
  const methods: Record<string, { queryCount: number, searchMs: { p50: number, p95: number }, formatMs: { p50: number, p95: number }, formattedContextCharactersMean?: number, at: Record<string, Scores> }> = {}
  for (const method of ['lexical', 'lexical-diversity', 'recency']) {
    const measurements = queries.map(query => query.methods[method])
    const at: Record<string, Scores> = {}
    for (const cutoff of cutoffs) {
      const scores = measurements.map(measurement => measurement.at[cutoff])
      at[cutoff] = {
        sessionRecall: mean(scores.map(value => value.sessionRecall)),
        sessionHit: mean(scores.map(value => value.sessionHit)),
        sessionComplete: mean(scores.map(value => value.sessionComplete)),
        sessionMrr: mean(scores.map(value => value.sessionMrr)),
        turnRecall: mean(scores.map(value => value.turnRecall)),
        turnHit: mean(scores.map(value => value.turnHit)),
        turnComplete: mean(scores.map(value => value.turnComplete)),
        turnMrr: mean(scores.map(value => value.turnMrr)),
        characters: mean(scores.map(value => value.characters)),
      }
    }
    const durations = measurements.map(measurement => measurement.searchMs)
    const formatDurations = measurements.map(measurement => measurement.formatMs)
    const contextCharacters = measurements.flatMap(measurement => measurement.formattedContextCharacters === undefined ? [] : [measurement.formattedContextCharacters])
    methods[method] = {
      queryCount: queries.length,
      searchMs: { p50: percentile(durations, 0.5), p95: percentile(durations, 0.95) },
      formatMs: { p50: percentile(formatDurations, 0.5), p95: percentile(formatDurations, 0.95) },
      formattedContextCharactersMean: contextCharacters.length ? mean(contextCharacters) : undefined,
      at,
    }
  }
  return methods
}

/**
 * Run evidence retrieval against pinned histories without an answer model or network calls.
 * Each question owns a new engine. Labels remain outside that engine.
 * Timing covers in-memory queries after ingestion, not storage, embedding, or answer generation.
 *
 * Call stack:
 *
 * main
 *   -> importHistory -> {@link chunkMemoryText}
 *   -> {@link MemoryEngine.rememberMany}
 *   -> {@link MemoryEngine.search} -> measure
 *   -> aggregate -> write summary and per-question ranks
 */
async function main() {
  const { values } = parseArgs({ options: {
    split: { type: 'string', default: 's' },
    profile: { type: 'string', default: 'research' },
    limit: { type: 'string' },
    offset: { type: 'string' },
    output: { type: 'string' },
  } })
  const manifest: Manifest = JSON.parse(await readFile(new URL('./datasets.json', import.meta.url), 'utf8'))
  const split = values.split ?? 's'
  const profile = values.profile ?? 'research'
  if (profile !== 'research' && profile !== 'app')
    throw new Error(`Unknown benchmark profile: ${profile}`)
  const cutoffs = profile === 'app' ? [1, 3, 6] : [1, 5, 10, 20]
  const searchSettings = {
    limit: profile === 'app' ? 6 : 20,
    maxCharacters: profile === 'app' ? 4000 : 24000,
    halfLifeDays: 0,
  }
  const contextBudget = profile === 'app' ? 4000 : undefined
  const entry = manifest.splits[split]
  if (!entry)
    throw new Error(`Unknown dataset split: ${split}`)
  const data = await readFile(resolve(root, 'data', entry.filename))
  if (data.length !== entry.bytes || createHash('sha256').update(data).digest('hex') !== entry.sha256)
    throw new Error('The dataset does not match the pinned size and SHA-256. Run benchmarks/download.py.')
  const questions: Question[] = JSON.parse(data.toString('utf8'))
  const offset = readPositiveInteger(values.offset, 0, true)
  const limit = readPositiveInteger(values.limit, questions.length)
  const selected = questions.slice(offset, offset + limit)
  const answerable = selected.filter(question => !question.question_id.endsWith('_abs'))
  if (!answerable.length)
    throw new Error('The selected range contains no answerable questions')
  const sourceFiles = ['../src/index.ts', '../src/memory.ts', '../src/schema.ts', '../src/text.ts', '../src/types.ts', './run.ts']
  const sourceHashes = Object.fromEntries(await Promise.all(sourceFiles.map(async (path) => {
    const source = await readFile(new URL(path, import.meta.url))
    return [path, createHash('sha256').update(source).digest('hex')]
  })))
  const miniSearchPackage: { version: string } = JSON.parse(await readFile(new URL('../node_modules/minisearch/package.json', import.meta.url), 'utf8'))
  const startedAt = new Date().toISOString()
  const start = performance.now()
  let peakRss = process.memoryUsage().rss
  const queries: QueryResult[] = []
  for (const [queryIndex, question] of answerable.entries()) {
    const { records, evidenceTurns, evidenceSessions } = importHistory(question)
    const now = parseDate(question.question_date)
    const engine = new MemoryEngine({
      scope: { userId: question.question_id, characterId: 'longmemeval' },
      // No capacity eviction: every strategy sees the complete released history.
      maxRecords: Math.max(1, records.length),
      now: () => now,
    })
    const ingestStart = performance.now()
    await engine.rememberMany(records)
    const ingestMs = performance.now() - ingestStart
    const indexed = await engine.list()
    if (indexed.length !== records.length)
      throw new Error(`History records were lost for ${question.question_id}: ${indexed.length}/${records.length}`)
    const methods: Record<string, Measurement> = {}
    const historyOrder = new Map(records.map((record, index) => [record.id, index]))
    // Alternate query order to reduce a systematic timing advantage from warmed token/index caches.
    const variants = queryIndex % 2 === 0
      ? [{ name: 'lexical', diversity: 0 }, { name: 'lexical-diversity', diversity: 0.15 }]
      : [{ name: 'lexical-diversity', diversity: 0.15 }, { name: 'lexical', diversity: 0 }]
    for (const variant of variants) {
      const queryStart = performance.now()
      const found = await engine.search(question.question, { ...searchSettings, diversity: variant.diversity })
      const searchMs = performance.now() - queryStart
      methods[variant.name] = measure(found.map(result => result.record), searchMs, evidenceSessions, evidenceTurns, cutoffs, contextBudget)
    }
    const recencyStart = performance.now()
    // Turns share a session timestamp. Preserve their numeric transcript order for this baseline.
    const chronological = [...indexed].sort((left, right) => right.createdAt - left.createdAt || historyOrder.get(right.id)! - historyOrder.get(left.id)!)
    const recent: MemoryRecord[] = []
    let recentCharacters = 0
    for (const record of chronological) {
      if (recentCharacters + record.text.length > searchSettings.maxCharacters)
        continue
      recent.push(record)
      recentCharacters += record.text.length
      if (recent.length === searchSettings.limit)
        break
    }
    methods.recency = measure(recent, performance.now() - recencyStart, evidenceSessions, evidenceTurns, cutoffs, contextBudget)
    queries.push({
      questionId: question.question_id,
      questionType: question.question_type,
      sessions: question.haystack_sessions.length,
      indexedChunks: indexed.length,
      indexedCharacters: indexed.reduce((sum, record) => sum + record.text.length, 0),
      evidenceSessions: evidenceSessions.size,
      evidenceTurns: evidenceTurns.size,
      ingestMs,
      methods,
    })
    peakRss = Math.max(peakRss, process.memoryUsage().rss)
    if ((queryIndex + 1) % 25 === 0 || queryIndex + 1 === answerable.length)
      console.log(`Evaluated ${queryIndex + 1}/${answerable.length} answerable questions`)
  }
  const types = [...new Set(queries.map(query => query.questionType))].sort()
  const report = {
    dataset: { repository: manifest.repository, revision: manifest.revision, split, filename: entry.filename, sha256: entry.sha256, license: manifest.license },
    scope: entry.purpose,
    evaluatedAt: startedAt,
    host: { node: process.version, platform: process.platform, architecture: process.arch, cpu: cpus()[0]?.model, totalMemoryBytes: totalmem() },
    implementation: { sourceSha256: sourceHashes, miniSearchVersion: miniSearchPackage.version },
    selection: { totalDatasetQuestions: questions.length, offset, requestedLimit: limit, selectedQuestions: selected.length, scoredQuestions: queries.length, excludedAbstentionQuestions: selected.length - answerable.length },
    configuration: { profile, chunk: chunkSettings, search: searchSettings, contextMaxCharacters: contextBudget, cutoffs, embeddings: false, storage: 'none', historyRoles: ['user', 'assistant'], diversityAblation: [0, 0.15] },
    methodology: {
      unit: 'First k chunks, without expansion to complete sessions',
      recall: 'Macro mean of unique supporting source IDs retrieved / all supporting source IDs',
      hit: 'Fraction of questions with at least one supporting source ID retrieved',
      complete: 'Fraction of questions with all supporting source IDs retrieved',
      mrr: 'Macro mean of reciprocal rank of the first supporting chunk, truncated at k',
      turnCaveat: 'A hit identifies a labeled source turn. A split chunk can omit the answer span within that turn.',
      sessionCaveat: 'A hit identifies a supporting session. It does not prove that the returned chunk contains the answer.',
      timing: 'One in-memory query per method per question after ingestion. Method order alternates. No model, storage, or network time.',
      formatting: 'The app profile scores only records present after the public context formatter. Formatting time is reported separately. Benchmark source IDs differ from application IDs.',
      abstention: 'Excluded from evidence recall. No answer generation or calibrated abstention is measured.',
    },
    resources: {
      wallSeconds: (performance.now() - start) / 1000,
      peakObservedRssBytes: peakRss,
      ingestMs: { p50: percentile(queries.map(query => query.ingestMs), 0.5), p95: percentile(queries.map(query => query.ingestMs), 0.95) },
      indexedChunks: { min: Math.min(...queries.map(query => query.indexedChunks)), mean: mean(queries.map(query => query.indexedChunks)), max: Math.max(...queries.map(query => query.indexedChunks)) },
      indexedCharactersMean: mean(queries.map(query => query.indexedCharacters)),
    },
    overall: aggregate(queries, cutoffs),
    byQuestionType: Object.fromEntries(types.map(type => [type, aggregate(queries.filter(query => query.questionType === type), cutoffs)])),
  }
  const output = values.output ? resolve(values.output) : resolve(root, 'runs', `${split}-${profile}-summary.json`)
  const details = resolve(root, 'runs', `${split}-${profile}-details.json`)
  await mkdir(dirname(output), { recursive: true })
  await mkdir(dirname(details), { recursive: true })
  await writeFile(output, `${JSON.stringify(report, null, 2)}\n`)
  await writeFile(details, `${JSON.stringify(queries)}\n`)
  const displayCutoff = Math.min(10, searchSettings.limit)
  console.table(Object.entries(report.overall).map(([method, result]) => ({
    method,
    'questions': result.queryCount,
    [`session recall@${displayCutoff}`]: result.at[displayCutoff].sessionRecall.toFixed(4),
    [`turn recall@${displayCutoff}`]: result.at[displayCutoff].turnRecall.toFixed(4),
    [`session MRR@${displayCutoff}`]: result.at[displayCutoff].sessionMrr.toFixed(4),
    'search p50 ms': result.searchMs.p50.toFixed(2),
    'search p95 ms': result.searchMs.p95.toFixed(2),
  })))
  console.log(`Summary: ${output}\nPer-question ranks: ${details}`)
}

void main().catch((error: unknown) => {
  console.error(error)
  process.exitCode = 1
})
