import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    name: '@proj-airi/memory-local',
    include: ['src/**/*.test.ts'],
  },
})
