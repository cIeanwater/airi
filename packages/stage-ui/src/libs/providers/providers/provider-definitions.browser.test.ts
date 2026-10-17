import type { ProviderTranslator } from '@proj-airi/provider-inference'

import { expect, it } from 'vitest'
import { z } from 'zod'

import { providerSherpawTranscription } from './sherpaw'

const translate: ProviderTranslator = key => key

// ROOT CAUSE:
// The Node test assumed an English host locale. Sherpaw reads the real interface
// language, so a Chinese host produced the correct Chinese default and failed.
// Browser storage now selects each tested locale without changing production defaults.
it.each([
  ['en', 'en'],
  ['zh-Hans', 'zh'],
])('accepts supported Sherpaw models with the %s interface language', async (locale, language) => {
  const previous = localStorage.getItem('settings/language')
  localStorage.setItem('settings/language', locale)
  try {
    const schema = await providerSherpawTranscription.createProviderConfig({ t: translate })

    expect(z.parse(schema, {})).toEqual({ model: 'paraformer-zh-en', modelLanguageFilter: language })
    for (const model of ['paraformer-zh-en', 'zipformer-multilingual', 'x-asr-zh-en-480ms-int8'])
      expect(z.parse(schema, { model })).toEqual({ model, modelLanguageFilter: language })
    expect(z.safeParse(schema, { model: 'zipformer-zh-en' }).success).toBe(false)
    expect(z.safeParse(schema, { model: 'unknown-model' }).success).toBe(false)
  }
  finally {
    if (previous === null)
      localStorage.removeItem('settings/language')
    else
      localStorage.setItem('settings/language', previous)
  }
})
