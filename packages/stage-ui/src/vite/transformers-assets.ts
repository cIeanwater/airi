import type { Plugin } from 'vite'

import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname } from 'node:path'

/** Bundles the WASM runtime shipped with the installed Transformers.js package. */
export function transformersAssets(): Plugin {
  const require = createRequire(import.meta.url)
  const distribution = dirname(require.resolve('@huggingface/transformers'))
  return {
    name: 'airi-transformers-assets',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        name: 'silero-vad-LICENSE.txt',
        source: readFileSync(new URL('../assets/vad/LICENSE', import.meta.url), 'utf8'),
      })
    },
    config: () => ({
      optimizeDeps: {
        exclude: [
          'virtual:airi-transformers/ort-wasm-simd-threaded.jsep.mjs?url',
          'virtual:airi-transformers/ort-wasm-simd-threaded.jsep.wasm?url',
        ],
      },
      resolve: {
        alias: [
          {
            find: /^virtual:airi-transformers\//,
            replacement: `${distribution}/`,
          },
        ],
      },
    }),
  }
}
