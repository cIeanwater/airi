<script setup lang="ts">
import type { MemoryRecord, MemoryScope } from '@proj-airi/memory-local'

import { errorMessageFrom } from '@moeru/std'
import { useAuthStore } from '@proj-airi/stage-ui/stores/auth'
import { useAiriCardStore } from '@proj-airi/stage-ui/stores/modules/airi-card'
import { useMemoryStore } from '@proj-airi/stage-ui/stores/modules/memory'
import { Button, DoubleCheckButton, FieldCheckbox, FieldInput, FieldTextArea } from '@proj-airi/ui'
import { storeToRefs } from 'pinia'
import { computed, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const memory = useMemoryStore()
const { lastError } = storeToRefs(memory)
const { userId } = storeToRefs(useAuthStore())
const { activeCardId } = storeToRefs(useAiriCardStore())
const scope = computed<MemoryScope>(() => ({ userId: userId.value || 'local', characterId: activeCardId.value || 'default' }))
const enabled = computed({
  get: () => memory.isEnabled(scope.value),
  set: value => memory.setEnabled(scope.value, value),
})
const records = shallowRef<MemoryRecord[]>([])
const query = ref('')
const draft = ref('')
const factKey = ref('')
const pinned = ref(false)
const editing = shallowRef<MemoryRecord>()
const transfer = ref('')
const busy = ref(false)
const status = ref('')
const error = ref('')
let readGeneration = 0

function text(key: string) {
  return t(`settings.pages.modules.memory-long-term.${key}`)
}

function resetEditor() {
  editing.value = undefined
  draft.value = ''
  factKey.value = ''
  pinned.value = false
}

async function refresh() {
  const owner = scope.value
  const generation = ++readGeneration
  busy.value = true
  error.value = ''
  try {
    const results = query.value.trim()
      ? (await memory.search(owner, query.value)).map(result => result.record)
      : await memory.list(owner)
    // Account or character changes clear the previous view immediately. A late
    // IndexedDB read must never restore records from the previous scope.
    if (owner === scope.value && generation === readGeneration)
      records.value = results
  }
  catch (cause) {
    if (owner === scope.value && generation === readGeneration)
      error.value = errorMessageFrom(cause) ?? text('operation-failed')
  }
  finally {
    if (owner === scope.value && generation === readGeneration)
      busy.value = false
  }
}

async function change(operation: (owner: MemoryScope) => Promise<unknown>) {
  const owner = scope.value
  busy.value = true
  error.value = ''
  status.value = ''
  try {
    await operation(owner)
    if (owner === scope.value) {
      resetEditor()
      status.value = text('saved')
      await refresh()
    }
  }
  catch (cause) {
    if (owner === scope.value)
      error.value = errorMessageFrom(cause) ?? text('operation-failed')
  }
  finally {
    if (owner === scope.value)
      busy.value = false
  }
}

function edit(record: MemoryRecord) {
  editing.value = record
  draft.value = record.text
  factKey.value = record.key ?? ''
  pinned.value = record.pinned
}

async function save() {
  const previous = editing.value
  const input = {
    id: previous?.id,
    text: draft.value,
    key: factKey.value.trim() || undefined,
    pinned: pinned.value,
    kind: previous?.kind ?? 'note',
    createdAt: previous?.createdAt,
    expiresAt: previous?.expiresAt,
    source: previous?.source,
  }
  await change(owner => memory.remember(owner, input))
}

async function exportMemories() {
  const owner = scope.value
  error.value = ''
  try {
    const json = await memory.exportMemories(owner)
    if (owner === scope.value)
      transfer.value = json
  }
  catch (cause) {
    if (owner === scope.value)
      error.value = errorMessageFrom(cause) ?? text('operation-failed')
  }
}

watch(scope, () => {
  records.value = []
  query.value = ''
  transfer.value = ''
  status.value = ''
  resetEditor()
  void refresh()
}, { immediate: true, flush: 'sync' })
</script>

<template>
  <div :class="['flex flex-col gap-6 pb-6']">
    <FieldCheckbox v-model="enabled" :label="text('enabled')" :description="text('privacy')" />
    <p :class="['text-sm text-neutral-500 dark:text-neutral-400']">
      {{ text('limits') }}
    </p>
    <p :class="['text-sm text-neutral-500 dark:text-neutral-400']">
      {{ text('scope') }} {{ scope.userId }} / {{ scope.characterId }}
    </p>
    <p v-if="error || lastError" role="alert" :class="['rounded-lg bg-red-100 p-3 text-sm text-red-800', 'dark:bg-red-950/40 dark:text-red-200']">
      {{ error || lastError }}
    </p>
    <p v-if="status" role="status" :class="['text-sm text-green-700 dark:text-green-300']">
      {{ status }}
    </p>

    <section :class="['flex flex-col gap-4 rounded-xl p-4', 'border border-neutral-200 dark:border-neutral-800']">
      <h2 :class="['text-lg font-medium']">
        {{ editing ? text('edit') : text('add') }}
      </h2>
      <FieldTextArea v-model="draft" :label="text('content')" :rows="4" />
      <FieldInput v-model="factKey" :label="text('fact-key')" :description="text('fact-key-description')" />
      <FieldCheckbox v-model="pinned" :label="text('pinned')" :description="text('pinned-description')" />
      <div :class="['flex flex-wrap gap-2']">
        <Button :disabled="busy || !draft.trim()" color="primary" @click="save">
          {{ text('save') }}
        </Button>
        <Button v-if="editing" :disabled="busy" @click="resetEditor">
          {{ text('cancel') }}
        </Button>
      </div>
    </section>

    <section :class="['flex flex-col gap-4']">
      <div :class="['flex flex-col gap-3 sm:flex-row sm:items-end']">
        <FieldInput v-model="query" :label="text('search')" :class="['flex-1']" />
        <Button :loading="busy" @click="refresh">
          {{ text('search-refresh') }}
        </Button>
      </div>
      <p :class="['text-sm text-neutral-500 dark:text-neutral-400']">
        {{ t('settings.pages.modules.memory-long-term.count', { count: records.length }) }}
      </p>
      <p v-if="!busy && !records.length" :class="['text-sm text-neutral-500 dark:text-neutral-400']">
        {{ text('empty') }}
      </p>
      <article
        v-for="record in records.slice(0, 100)"
        :key="record.id"
        :class="['flex flex-col gap-3 rounded-xl p-4', 'border border-neutral-200 dark:border-neutral-800']"
      >
        <p :class="['whitespace-pre-wrap break-words text-sm']">
          {{ record.text }}
        </p>
        <p :class="['break-all text-xs text-neutral-500 dark:text-neutral-400']">
          {{ record.kind === 'note' ? text('note') : text('user-message') }}
          · {{ new Date(record.createdAt).toLocaleString() }}
          <span v-if="record.key"> · {{ record.key }}</span>
          <span v-if="record.source?.sessionId"> · {{ record.source.sessionId }}</span>
          <span v-if="record.pinned"> · {{ text('pinned') }}</span>
        </p>
        <div :class="['flex gap-2']">
          <Button size="sm" :disabled="busy" @click="edit(record)">
            {{ text('edit') }}
          </Button>
          <DoubleCheckButton size="sm" :disabled="busy" @confirm="change(owner => memory.forget(owner, record.id))">
            {{ text('delete') }}
            <template #confirm>
              {{ text('confirm-delete') }}
            </template>
            <template #cancel>
              {{ text('cancel') }}
            </template>
          </DoubleCheckButton>
        </div>
      </article>
      <p v-if="records.length > 100" :class="['text-sm text-neutral-500 dark:text-neutral-400']">
        {{ text('list-limit') }}
      </p>
    </section>

    <section :class="['flex flex-col gap-4 rounded-xl p-4', 'border border-neutral-200 dark:border-neutral-800']">
      <FieldTextArea v-model="transfer" :label="text('transfer')" :description="text('transfer-description')" :required="false" :rows="6" />
      <div :class="['flex flex-wrap gap-2']">
        <Button :disabled="busy" @click="exportMemories">
          {{ text('export') }}
        </Button>
        <Button :disabled="busy || !transfer.trim()" @click="change(owner => memory.importMemories(owner, transfer))">
          {{ text('import') }}
        </Button>
        <DoubleCheckButton :disabled="busy" @confirm="change(owner => memory.clear(owner))">
          {{ text('clear') }}
          <template #confirm>
            {{ text('confirm-clear') }}
          </template>
          <template #cancel>
            {{ text('cancel') }}
          </template>
        </DoubleCheckButton>
      </div>
      <p :class="['text-sm text-neutral-500 dark:text-neutral-400']">
        {{ text('clear-description') }}
      </p>
    </section>
  </div>
</template>

<route lang="yaml">
meta:
  layout: settings
  titleKey: settings.pages.modules.memory-long-term.title
  subtitleKey: settings.title
  stageTransition:
    name: slide
</route>
