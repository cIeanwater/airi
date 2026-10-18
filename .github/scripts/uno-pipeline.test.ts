import { fileURLToPath } from 'node:url'

import UnoCSS from 'unocss/vite'

import { build, normalizePath } from 'vite'
import { describe, expect, it } from 'vitest'

import { sharedUnoConfig } from '../../uno.config'

describe('unocss source extraction', () => {
  // https://github.com/cIeanwater/airi/actions/runs/39136748136
  // ROOT CAUSE:
  // Attributify reads compiled render comparisons as fake HTML tags.
  // The scrollbar patch flag and dialog open prop then form an invalid selector.
  // Excluding compiled SFC script requests keeps extraction on original sources.
  it('keeps source utilities without treating compiled Vue render code as attributes', async () => {
    const root = normalizePath(fileURLToPath(new URL('../../', import.meta.url)))
    const entry = `${root}/uno-regression-entry.js`
    const component = `${root}/packages/stage-ui/src/uno-regression.vue`
    const script = `${component}?vue&type=script&setup=true&lang.ts`
    const styles = `${root}/packages/stage-ui/src/uno-regression-styles.ts`
    // This reduced Vue render output turns the comparison into a fake tag.
    // Attributify then joins the scrollbar patch flag with the dialog open prop.
    const sources = new Map([
      [entry, `import 'uno.css'; import { template } from '${component}'; import '${script}'; import { text } from '${styles}'; console.log(template, text)`],
      [component, `export const template = '<details open class="p-4 scrollbar-none open:bg-red-500"></details>'`],
      [script, `const count = 0; const hidden = count < 1
const history = ["variant", "scrollbar"], dialog = {
  open: true,
  "onUpdate:open": () => {},
}; console.log(hidden, history, dialog)`],
      [styles, 'export const text = "text-sm"'],
    ])
    const result = await build({
      root,
      configFile: false,
      logLevel: 'silent',
      publicDir: false,
      plugins: [
        UnoCSS({ ...sharedUnoConfig(), configFile: false }),
        {
          name: 'uno-regression-source',
          resolveId: id => sources.has(id) ? id : null,
          load: id => sources.get(id),
        },
      ],
      build: {
        write: false,
        cssMinify: 'lightningcss',
        rolldownOptions: { input: entry },
      },
    })
    const outputs = Array.isArray(result) ? result : [result]
    const css = outputs.flatMap((output) => {
      if (!('output' in output))
        throw new Error('Expected a completed build')
      return output.output
    })
      .filter(output => output.type === 'asset')
      .filter(output => output.fileName.endsWith('.css'))
      .map(output => typeof output.source === 'string' ? output.source : new TextDecoder().decode(output.source))
      .join('\n')

    expect(css).toContain('padding:1rem')
    expect(css).toContain('::-webkit-scrollbar')
    expect(css).toContain('[open]')
    expect(css).toContain('font-size:.875rem')
    expect(css).not.toContain('[scrollbar~=')
  })
})
