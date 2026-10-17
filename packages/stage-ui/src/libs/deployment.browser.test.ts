import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { useLocalFirstRequest } from '../composables/use-local-first'
import { useAuthStore } from '../stores/auth'
import { authedFetch } from './auth-fetch'
import { isHostedRoute, isLocalMode } from './deployment'
import { isAnalyticsAvailableInBuild } from './product-signals'

const persistedKeys = ['auth/v1/token', 'local-mode:preserved-chat'] as const
const previousValues = new Map<string, string | null>()

beforeEach(() => {
  vi.stubEnv('VITE_AIRI_LOCAL_MODE', 'true')
  setActivePinia(createPinia())
  for (const key of persistedKeys)
    previousValues.set(key, localStorage.getItem(key))
})

afterEach(() => {
  for (const [key, value] of previousValues) {
    if (value === null)
      localStorage.removeItem(key)
    else
      localStorage.setItem(key, value)
  }
  previousValues.clear()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

it('starts with anonymous local identity without restoring hosted credentials or requesting login', async () => {
  localStorage.setItem('auth/v1/token', 'synthetic-old-token')
  localStorage.setItem('local-mode:preserved-chat', 'synthetic saved conversation')
  const fetchRequest = vi.fn<typeof fetch>()
  vi.stubGlobal('fetch', fetchRequest)
  const auth = useAuthStore()

  await auth.initialize()
  await auth.requestLogin()
  await auth.expireSession(auth.sessionVersion)
  expect(await auth.fetchSession()).toBe(false)
  expect(await auth.refreshTokenNow()).toBeNull()
  expect(await auth.completeSignIn({ accessToken: 'new-token', clientId: 'test', expiresIn: 60 })).toBe(false)
  expect(await auth.listSessions()).toEqual({ data: [], error: null })
  expect(await auth.consumeLoginRequest()).toBe(false)
  expect(auth.userId).toBe('local')
  expect(auth.isAuthenticated).toBe(false)
  expect(auth.needsLogin).toBe(false)
  expect(auth.token).toBeNull()
  expect(fetchRequest).not.toHaveBeenCalled()
  expect(localStorage.getItem('auth/v1/token')).toBe('synthetic-old-token')
  expect(localStorage.getItem('local-mode:preserved-chat')).toBe('synthetic saved conversation')
})

it('never calls hosted sync even when a caller explicitly permits a remote read', async () => {
  const local = vi.fn(async () => 'local conversation')
  const remote = vi.fn(async () => 'cloud conversation')
  const request = useLocalFirstRequest({ local, remote, allowRemote: () => true, lazy: true })

  await request.execute()

  expect(request.state.value).toBe('local conversation')
  expect(local).toHaveBeenCalledOnce()
  expect(remote).not.toHaveBeenCalled()
})

it('blocks hosted HTTP and analytics before any network request', async () => {
  const fetchRequest = vi.fn<typeof fetch>()
  vi.stubGlobal('fetch', fetchRequest)
  vi.stubEnv('VITE_ENABLE_ANALYTICS', 'true')

  await expect(authedFetch('https://api.airi.build/api/v1/chats')).rejects.toThrow('disabled in local mode')

  expect(fetchRequest).not.toHaveBeenCalled()
  expect(isAnalyticsAvailableInBuild()).toBe(false)
})

it('requires explicit false for connected mode and keeps local tools outside hosted routes', () => {
  expect(isLocalMode()).toBe(true)
  expect(isHostedRoute('/auth/callback')).toBe(true)
  expect(isHostedRoute('/settings/account')).toBe(true)
  expect(isHostedRoute('/settings/flux')).toBe(true)
  expect(isHostedRoute('/settings/modules/memory-long-term')).toBe(false)
  expect(isHostedRoute('/settings/providers')).toBe(false)
  expect(isHostedRoute('/settings/connection')).toBe(false)
  vi.stubEnv('VITE_AIRI_LOCAL_MODE', '')
  expect(isLocalMode()).toBe(true)
  vi.stubEnv('VITE_AIRI_LOCAL_MODE', 'false')
  expect(isLocalMode()).toBe(false)
})
