import { resolve } from 'node:path'
import { env } from 'node:process'

import Vue from '@vitejs/plugin-vue'
import UnoCSS from 'unocss/vite'
import Info from 'unplugin-info/vite'
import VueRouter from 'vue-router/vite'

import { playwright } from '@vitest/browser-playwright'
import { normalizePath } from 'vite'
import { defineConfig } from 'vitest/config'

import { sharedUnoConfig } from '../../uno.config'
import { transformersAssets } from '../stage-ui/src/vite/transformers-assets'

export default defineConfig({
  root: import.meta.dirname,
  plugins: [
    transformersAssets(),
    Info(),
    Vue(),
    VueRouter({
      extensions: ['.vue'],
      dts: false,
      routesFolder: resolve(import.meta.dirname, 'src', 'pages'),
      exclude: ['**/components/**', '**/*.test.ts'],
    }),
    UnoCSS({
      ...sharedUnoConfig(),
      configFile: false,
      content: {
        filesystem: [
          `${import.meta.dirname}/src/**/*.vue`,
          `${import.meta.dirname}/../stage-ui/src/**/*.vue`,
          `${import.meta.dirname}/../ui/src/**/*.vue`,
        ].map(normalizePath),
      },
    }),
  ],
  test: {
    env: { VITE_AIRI_LOCAL_MODE: 'false' },
    include: ['src/**/*.browser.test.ts'],
    browser: {
      enabled: true,
      headless: true,
      provider: playwright({ launchOptions: { channel: env.VITEST_BROWSER_CHANNEL } }),
      instances: [
        { browser: 'chromium' },
      ],
    },
  },
})
