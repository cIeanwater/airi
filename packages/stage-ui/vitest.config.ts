import { cwd, env } from 'node:process'

import Vue from '@vitejs/plugin-vue'
import UnoCSS from 'unocss/vite'
import Info from 'unplugin-info/vite'
import VueRouter from 'vue-router/vite'

import { playwright } from '@vitest/browser-playwright'
import { mergeConfigs } from 'unocss'
import { loadEnv, normalizePath } from 'vite'
import { defineConfig } from 'vitest/config'

import { sharedUnoConfig } from '../../uno.config'
import { transformersAssets } from './src/vite/transformers-assets'

const externalRequests = new WeakMap<object, string[]>()

export default defineConfig({
  root: import.meta.dirname,
  // Shared settings pages import these optional UI dependencies through the
  // component barrel. Bundle them before a browser test starts to avoid HMR.
  optimizeDeps: {
    include: [
      '@huggingface/transformers',
      '@moeru/std',
      '@vueuse/core',
      'embla-carousel-vue',
      'html2canvas',
      'node-vibrant/browser',
      'reka-ui',
      'vaul-vue',
      'vue',
      'vue-i18n',
    ],
  },
  plugins: [
    transformersAssets(),
    Info(),
    // Use the app's route-block transform when browser tests mount shared pages.
    VueRouter({ routesFolder: [], dts: false }),
    Vue(),
    UnoCSS(mergeConfigs([sharedUnoConfig(), {
      // Browser tests use product styles, not Histoire's hover-preview variants.
      configFile: false,
      // Vitest loads components after the stylesheet. Scan their source before
      // the initial CSS response instead of relying on Vite's HMR updates.
      // File globs require forward slashes on Windows.
      content: {
        filesystem: [
          `${import.meta.dirname}/src/**/*.vue`,
          `${import.meta.dirname}/../ui/src/**/*.vue`,
        ].map(normalizePath),
      },
    }])),
  ],
  test: {
    env: { ...loadEnv('test', cwd(), ''), VITE_AIRI_LOCAL_MODE: 'false' },
    projects: [
      {
        extends: true,
        test: {
          name: 'node',
          include: ['src/**/*.test.ts'],
          exclude: ['src/**/*.browser.test.ts'],
          fileParallelism: false,
          hookTimeout: 20_000,
          maxWorkers: 1,
          testTimeout: 20_000,
        },
      },
      {
        extends: true,
        test: {
          name: 'browser',
          include: ['src/**/*.browser.test.ts'],
          exclude: ['**/node_modules/**'],
          browser: {
            enabled: true,
            headless: true,
            commands: {
              async voiceExternalRequests({ context }, block: boolean) {
                if (!block) {
                  await context.unrouteAll({ behavior: 'wait' })
                  const requests = externalRequests.get(context) ?? []
                  externalRequests.delete(context)
                  return requests
                }
                const requests: string[] = []
                externalRequests.set(context, requests)
                await context.route('**/*', async (route) => {
                  const url = new URL(route.request().url())
                  if (url.protocol === 'http:' || url.protocol === 'https:') {
                    if (!['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) {
                      requests.push(`${url.origin}${url.pathname}`)
                      await route.abort('internetdisconnected')
                      return
                    }
                  }
                  await route.continue()
                })
                return requests
              },
            },
            provider: playwright({ launchOptions: { channel: env.VITEST_BROWSER_CHANNEL, args: ['--autoplay-policy=no-user-gesture-required', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] } }),
            instances: [
              { browser: 'chromium' },
            ],
          },
        },
      },
    ],
  },
})
