<script setup lang="ts">
import type { MessageContextResult } from '../../../../libs/local-message-context'
import type { ChatHistoryItem } from '../../../../types/chat'

import { errorMessageFrom } from '@moeru/std'
import { Button, ScrollableArea } from '@proj-airi/ui'
import { DialogClose, DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'
import { computed, onScopeDispose, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { useDownload } from '../../../../composables/download'
import { projectMessageContext } from '../../../../libs/local-message-context'
import { useMessageContext } from '../composables/use-message-context'

const props = defineProps<{
  message: ChatHistoryItem | undefined
}>()
const open = defineModel<boolean>('open', { required: true })
const { t } = useI18n()
const { load, selectionKey } = useMessageContext()
const result = shallowRef<MessageContextResult>()
const loading = shallowRef(false)
const loadError = shallowRef('')
const copyError = shallowRef('')
const copied = shallowRef(false)
const view = shallowRef<'messages' | 'record'>('messages')
let requestSequence = 0
let copySequence = 0

const json = computed(() => {
  if (result.value?.status !== 'found')
    return ''
  const records = result.value.records
  return JSON.stringify(view.value === 'messages' ? projectMessageContext(records) : records, null, 2)
})
const jsonLabel = computed(() => t(`stage.chat.context.${view.value}-json`))
const unavailableKey = computed(() => result.value?.status === 'unavailable' ? `stage.chat.context.${result.value.reason}` : '')

async function loadContext() {
  const sequence = ++requestSequence
  result.value = undefined
  loadError.value = ''
  copyError.value = ''
  copied.value = false
  view.value = 'messages'
  loading.value = false
  if (!open.value || !props.message)
    return

  loading.value = true
  try {
    const loaded = await load(props.message)
    // Closing or switching the selected message discards its pending response.
    if (sequence === requestSequence)
      result.value = loaded
  }
  catch (error) {
    if (sequence === requestSequence)
      loadError.value = errorMessageFrom(error) ?? t('stage.chat.context.failed')
  }
  finally {
    if (sequence === requestSequence)
      loading.value = false
  }
}

watch([open, () => props.message], () => void loadContext(), { immediate: true })
watch(view, () => {
  copySequence++
  copied.value = false
  copyError.value = ''
}, { flush: 'sync' })
// A saved record belongs to the account and conversation that opened it.
watch(selectionKey, () => {
  open.value = false
  void loadContext()
}, { flush: 'sync' })
onScopeDispose(() => requestSequence++)

async function copyJson() {
  const sequence = requestSequence
  const copy = ++copySequence
  copyError.value = ''
  try {
    await navigator.clipboard.writeText(json.value)
    if (sequence === requestSequence && copy === copySequence)
      copied.value = true
  }
  catch (error) {
    if (sequence === requestSequence && copy === copySequence)
      copyError.value = errorMessageFrom(error) ?? t('stage.chat.context.copy-failed')
  }
}

function downloadJson() {
  const blob = new Blob([json.value], { type: 'application/json;charset=utf-8' })
  const { download } = useDownload(blob, `humanning-message-${view.value}.json`)
  download()
}
</script>

<template>
  <DialogRoot v-model:open="open">
    <DialogPortal>
      <DialogOverlay :class="['fixed inset-0 z-[9999] bg-black/35']" />
      <DialogContent
        :class="[
          'pointer-events-auto fixed left-1/2 top-1/2 z-[9999] w-[calc(100dvw-2rem)] max-w-4xl max-h-[85dvh]',
          'flex flex-col gap-3 rounded-3xl p-5 -translate-x-1/2 -translate-y-1/2 shadow-xl outline-none',
          'bg-neutral-50 text-neutral-900 dark:bg-neutral-900 dark:text-neutral-100',
        ]"
      >
        <div :class="['flex items-start justify-between gap-4']">
          <DialogTitle :class="['text-xl font-semibold tracking-tight']">
            {{ t('stage.chat.context.title') }}
          </DialogTitle>
          <DialogClose as-child>
            <Button size="sm" :aria-label="t('stage.chat.context.close')" icon="i-solar:close-circle-linear">
              {{ t('stage.chat.context.close') }}
            </Button>
          </DialogClose>
        </div>
        <DialogDescription :class="['text-sm text-neutral-600 dark:text-neutral-300']">
          {{ t(`stage.chat.context.${view}-description`) }}
        </DialogDescription>
        <p v-if="loading" role="status" :class="['py-6 text-sm']">
          {{ t('stage.chat.context.loading') }}
        </p>
        <div v-else-if="loadError" role="alert" :class="['flex flex-col items-start gap-3 py-4']">
          <p>{{ t('stage.chat.context.failed') }}</p>
          <pre :class="['max-w-full whitespace-pre-wrap break-all text-xs font-mono']">{{ loadError }}</pre>
          <Button size="sm" @click="loadContext">
            {{ t('stage.chat.context.retry') }}
          </Button>
        </div>
        <p v-else-if="result?.status === 'unavailable'" role="status" :class="['py-6 text-sm leading-relaxed']">
          {{ t(unavailableKey) }}
        </p>
        <template v-else-if="result?.status === 'found'">
          <div role="group" :aria-label="t('stage.chat.context.view')" :class="['flex flex-wrap gap-2']">
            <Button
              size="sm"
              :variant="view === 'messages' ? 'primary' : 'secondary'"
              :aria-pressed="view === 'messages'"
              @click="view = 'messages'"
            >
              {{ t('stage.chat.context.messages') }}
            </Button>
            <Button
              size="sm"
              :variant="view === 'record' ? 'primary' : 'secondary'"
              :aria-pressed="view === 'record'"
              @click="view = 'record'"
            >
              {{ t('stage.chat.context.record') }}
            </Button>
          </div>
          <p v-if="result.match === 'content'" role="status" :class="['text-xs text-neutral-600 dark:text-neutral-300']">
            {{ t('stage.chat.context.content-match') }}
          </p>
          <ScrollableArea
            orientation="both"
            :class="['min-h-0 max-h-[55dvh] rounded-xl border border-neutral-200 bg-white dark:border-neutral-700 dark:bg-neutral-950']"
            :viewport-class="['p-3']"
          >
            <pre
              tabindex="0"
              :aria-label="jsonLabel"
              :class="['m-0 w-max min-w-full select-text text-xs font-mono leading-relaxed outline-none', 'focus-visible:ring-2 focus-visible:ring-primary-400']"
            >{{ json }}</pre>
          </ScrollableArea>
          <div :class="['flex flex-wrap gap-2']">
            <Button size="sm" icon="i-solar:copy-linear" @click="copyJson">
              {{ t(copied ? 'stage.chat.context.copied' : `stage.chat.context.copy-${view}`) }}
            </Button>
            <Button size="sm" icon="i-solar:download-linear" @click="downloadJson">
              {{ t(`stage.chat.context.download-${view}`) }}
            </Button>
          </div>
          <p v-if="copyError" role="alert" :class="['text-sm text-red-600 dark:text-red-300']">
            {{ t('stage.chat.context.copy-failed') }} {{ copyError }}
          </p>
        </template>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
