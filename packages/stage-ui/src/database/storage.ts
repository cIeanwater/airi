import indexedDbDriver from 'unstorage/drivers/indexedb'
import memoryDriver from 'unstorage/drivers/memory'

import { createStorage } from 'unstorage'

export const storage = createStorage({
  driver: memoryDriver(),
})

const localDriver = indexedDbDriver({ base: 'airi-local' })
const getLocalKeys = localDriver.getKeys

// NOTICE:
// Local key enumeration must hide other mounts in the shared IDB object store.
// unstorage 1.17.5's indexeddb driver returns raw keys without stripping its base.
// Source: node_modules/unstorage/drivers/indexedb.mjs, getKeys().
// Remove this adapter when the upstream driver returns only relative scoped keys.
localDriver.getKeys = async (base, options) => (await getLocalKeys(base, options))
  .filter(key => key.startsWith('airi-local:'))
  .map(key => key.slice('airi-local:'.length))

storage.mount('local', localDriver)
storage.mount('outbox', indexedDbDriver({ base: 'airi-sync-queue' }))
