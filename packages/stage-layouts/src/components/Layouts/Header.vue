<script setup lang="ts">
import { ProfileSwitcherPopover } from '@proj-airi/stage-ui/components'
import { isLocalMode } from '@proj-airi/stage-ui/libs/deployment'
import { useRouter } from 'vue-router'

import HeaderAvatar from './HeaderAvatar.vue'
import HeaderLink from './HeaderLink.vue'

const router = useRouter()
const localMode = isLocalMode()

function handleNavigation() {
  router.push('/settings/airi-card')
}
</script>

<template>
  <header :class="['mb-1 w-full flex items-center justify-between gap-2']">
    <HeaderLink />
    <div :class="['flex items-center gap-2']">
      <ProfileSwitcherPopover @manage="handleNavigation" />
      <HeaderAvatar v-if="!localMode" />
    </div>
  </header>
</template>
