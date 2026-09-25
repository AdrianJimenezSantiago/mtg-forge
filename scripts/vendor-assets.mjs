import {
  mkdirSync, copyFileSync, existsSync, statSync, readFileSync, writeFileSync,
} from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const OUT = join(ROOT, 'static', 'vendor')

const ASSETS = [
  ['alpinejs/dist/cdn.min.js', 'alpine.min.js'],
  ['@alpinejs/collapse/dist/cdn.min.js', 'alpine-collapse.min.js'],
  ['@alpinejs/focus/dist/cdn.min.js', 'alpine-focus.min.js'],
  ['lucide/dist/umd/lucide.min.js', 'lucide.min.js'],
  ['chart.js/dist/chart.umd.js', 'chart.umd.js'],
  ['mana-font/css/mana.min.css', 'mana.min.css'],
  ['mana-font/fonts/mana.woff2', 'fonts/mana.woff2'],
  ['mana-font/fonts/mana.woff', 'fonts/mana.woff'],
]

let copied = 0
let missing = 0

for (const [src, dest] of ASSETS) {
  const from = join(ROOT, 'node_modules', src)
  const to = join(OUT, dest)
  if (!existsSync(from)) {
    console.error(`  FALTA  ${src} — ¿has ejecutado npm install?`)
    missing++
    continue
  }
  mkdirSync(dirname(to), { recursive: true })
  copyFileSync(from, to)
  const kb = (statSync(to).size / 1024).toFixed(1)
  console.log(`  ok     ${dest.padEnd(28)} ${kb.padStart(8)} KB`)
  copied++
}

const cssPath = join(OUT, 'mana.min.css')
if (existsSync(cssPath)) {
  const original = readFileSync(cssPath, 'utf8')
  const trimmed = original.replace(
    /@font-face\{font-family:"Mana";src:[^}]*?font-weight/,
    '@font-face{font-family:"Mana";' +
      'src:url("fonts/mana.woff2") format("woff2"),' +
      'url("fonts/mana.woff") format("woff");font-weight'
  ).replace(
    /@font-face\{font-family:"MPlantin";src:[^}]*?\}/,
    ''
  )
  const noMap = trimmed.replace(/\/\*#\s*sourceMappingURL=[^*]*\*\//g, '')
  writeFileSync(cssPath, noMap)
  console.log('  ok     mana.min.css reescrito a woff2+woff')
}

const IIFE_BUNDLES = [
  {
    pkg: '@formkit/auto-animate',
    out: 'auto-animate.min.js',
    contents: "import autoAnimate from '@formkit/auto-animate';\nwindow.autoAnimate = autoAnimate;\n",
  },
]

try {
  const esbuild = await import('esbuild')
  for (const bundle of IIFE_BUNDLES) {
    const to = join(OUT, bundle.out)
    esbuild.buildSync({
      stdin: { contents: bundle.contents, resolveDir: ROOT, loader: 'js' },
      bundle: true,
      format: 'iife',
      minify: true,
      target: ['chrome110', 'firefox115', 'safari16'],
      legalComments: 'none',
      outfile: to,
    })
    const kb = (statSync(to).size / 1024).toFixed(1)
    console.log(`  ok     ${bundle.out.padEnd(28)} ${kb.padStart(8)} KB  (iife de ${bundle.pkg})`)
    copied++
  }
} catch (err) {
  console.error(`  FALTA  bundles IIFE — ¿has ejecutado npm install? (${err.message})`)
  missing++
}

console.log(`\n${copied} assets copiados a static/vendor/`)
if (missing > 0) {
  console.error(`${missing} assets no encontrados.`)
  process.exit(1)
}
console.log('Ahora ejecuta: npm run build:css')
