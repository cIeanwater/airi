import type { MemoryInput, MemoryScope, MemorySnapshot } from './types'

import * as v from 'valibot'

const identifier = v.pipe(v.string(), v.minLength(1), v.maxLength(512))
const timestamp = v.pipe(v.number(), v.finite(), v.minValue(0))
const fields = {
  text: v.pipe(v.string(), v.trim(), v.minLength(1), v.maxLength(16000)),
  kind: v.optional(v.picklist(['note', 'turn'])),
  source: v.optional(v.strictObject({
    sessionId: v.optional(identifier),
    messageId: v.optional(identifier),
    label: v.optional(v.pipe(v.string(), v.maxLength(1024))),
  })),
  key: v.optional(identifier),
  pinned: v.optional(v.boolean()),
  expiresAt: v.optional(timestamp),
  createdAt: v.optional(timestamp),
}
const inputSchema = v.strictObject({ id: v.optional(identifier), ...fields })
const scopeSchema = v.strictObject({ userId: identifier, characterId: identifier })
const recordSchema = v.strictObject({
  ...fields,
  id: identifier,
  kind: v.picklist(['note', 'turn']),
  pinned: v.boolean(),
  createdAt: timestamp,
  updatedAt: timestamp,
  embedding: v.optional(v.strictObject({
    model: identifier,
    vector: v.pipe(v.array(v.pipe(v.number(), v.finite())), v.minLength(1), v.maxLength(8192)),
  })),
})
const snapshotSchema = v.strictObject({
  version: v.literal(1),
  scope: scopeSchema,
  records: v.pipe(v.array(recordSchema), v.maxLength(100000)),
})

export function parseInput(input: MemoryInput): MemoryInput {
  return v.parse(inputSchema, input)
}

export function parseScope(scope: MemoryScope): MemoryScope {
  return v.parse(scopeSchema, scope)
}

export function parseSnapshot(value: unknown, scope: MemoryScope): MemorySnapshot {
  const snapshot = v.parse(snapshotSchema, value)
  if (snapshot.scope.userId !== scope.userId || snapshot.scope.characterId !== scope.characterId)
    throw new Error('Memory scope does not match the current user and character')
  const ids = new Set<string>()
  const keys = new Set<string>()
  for (const record of snapshot.records) {
    if (ids.has(record.id) || (record.key !== undefined && keys.has(record.key)))
      throw new Error('Memory snapshot contains duplicate IDs or keys')
    ids.add(record.id)
    if (record.key !== undefined)
      keys.add(record.key)
  }
  return snapshot
}
