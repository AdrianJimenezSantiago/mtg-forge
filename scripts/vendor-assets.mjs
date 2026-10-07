import {
  mkdirSync, copyFileSync, existsSync, statSync, readFileSync, writeFileSync, readdirSync,
} from 'node:fs'
import { dirname, join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const OUT = join(ROOT, 'static', 'vendor')

const ASSETS = [
  ['alpinejs/dist/cdn.min.js', 'alpine.min.js'],
  ['@alpinejs/collapse/dist/cdn.min.js', 'alpine-collapse.min.js'],
  ['@alpinejs/focus/dist/cdn.min.js', 'alpine-focus.min.js'],
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

const ICON_SOURCES = [join(ROOT, 'templates'), join(ROOT, 'static', 'js')]

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    return entry.isDirectory() ? walk(full) : [full]
  })
}

function toPascalCase(name) {
  return name.replace(/(\w)(\w*)(_|-|\s*)/g, (_, first, rest) => first.toUpperCase() + rest.toLowerCase())
}

async function usedLucideIcons() {
  const { icons } = await import('lucide')
  const tokens = new Set()
  for (const dir of ICON_SOURCES) {
    for (const file of walk(dir)) {
      if (!['.html', '.js'].includes(extname(file))) continue
      for (const match of readFileSync(file, 'utf8').matchAll(/[a-z][a-z0-9]*(?:-[a-z0-9]+)*/g)) {
        tokens.add(match[0])
      }
    }
  }
  return [...tokens].map(toPascalCase).filter((name) => name in icons).sort()
}

const lucideIcons = await usedLucideIcons()

const IIFE_BUNDLES = [
  {
    pkg: 'lucide',
    out: 'lucide.min.js',
    contents: [
      "import replaceElement from 'lucide/dist/esm/replaceElement.js';",
      `import { ${lucideIcons.join(', ')} } from 'lucide';`,
      `const icons = { ${lucideIcons.join(', ')} };`,
      'function createIcons(root) {',
      "  const scope = root && root.querySelectorAll ? root : document;",
      "  scope.querySelectorAll('[data-lucide]:not(svg)').forEach((element) =>",
      "    replaceElement(element, { nameAttr: 'data-lucide', icons, attrs: {} }));",
      '}',
      'window.lucide = { createIcons, icons };',
    ].join('\n'),
  },
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
