/**
 * Copia los assets de terceros desde node_modules a static/vendor/.
 *
 * Motivo: la app se distribuye como un .exe para uso local y ANTES cargaba
 * Tailwind, Alpine, Lucide, mana-font y Chart.js desde tres CDNs distintos.
 * Sin internet la interfaz se veía completamente sin estilos, y con internet
 * cada arranque filtraba la IP del usuario a unpkg, jsdelivr y Google Fonts.
 *
 * El resultado de este script SE VERSIONA en el repo. Así:
 *   - clonar y ejecutar no requiere Node ni npm install
 *   - PyInstaller solo tiene que empaquetar static/ tal cual
 *   - una release nunca depende de que un CDN esté vivo
 *
 * Uso:  npm install && npm run vendor && npm run build:css
 */
import {
  mkdirSync, copyFileSync, existsSync, statSync, readFileSync, writeFileSync,
} from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

// `fileURLToPath`, NO `.pathname`. En Windows, el pathname de una file:// URL
// es "/C:/ruta/..." — con una barra inicial de más y separadores POSIX—, así
// que al pasarlo por `join` sale una ruta que no existe y el script reportaba
// "FALTA" para los ocho assets justo después de un `npm ci` correcto.
// `fileURLToPath` devuelve la forma nativa de la plataforma.
const ROOT = fileURLToPath(new URL('..', import.meta.url))
const OUT = join(ROOT, 'static', 'vendor')

/** [origen en node_modules, destino relativo a static/vendor] */
const ASSETS = [
  ['alpinejs/dist/cdn.min.js', 'alpine.min.js'],
  ['@alpinejs/collapse/dist/cdn.min.js', 'alpine-collapse.min.js'],
  // Alpine Focus habilita x-trap: atrapa el foco dentro de los modales, que
  // hasta ahora se escapaba al fondo con Tab. Ver la sección de accesibilidad.
  ['@alpinejs/focus/dist/cdn.min.js', 'alpine-focus.min.js'],
  ['lucide/dist/umd/lucide.min.js', 'lucide.min.js'],
  ['chart.js/dist/chart.umd.js', 'chart.umd.js'],
  ['mana-font/css/mana.min.css', 'mana.min.css'],
  // La hoja de mana-font referencia ../fonts/ — hay que respetar esa ruta
  // relativa o los símbolos de maná salen como cuadrados.
  // Solo woff2 + woff: el .exe embebe un navegador moderno (WebView2/Chromium),
  // así que eot/ttf/svg son 2,6 MB de peso muerto. El postproceso de abajo
  // reescribe el @font-face para que no los referencie.
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

// mana.min.css referencia eot/ttf/svg que ya no copiamos. Reescribimos el
// @font-face a woff2+woff para evitar 404 en cada carga de página.
const cssPath = join(OUT, 'mana.min.css')
if (existsSync(cssPath)) {
  const original = readFileSync(cssPath, 'utf8')
  const trimmed = original.replace(
    /@font-face\{font-family:"Mana";src:[^}]*?font-weight/,
    // OJO con la ruta: el original usa "../fonts/", que desde
    // /static/vendor/mana.min.css resolvería a /static/fonts/ — donde no hay
    // nada. Los ficheros viven en /static/vendor/fonts/, así que la ruta
    // correcta es relativa sin subir un nivel.
    '@font-face{font-family:"Mana";' +
      'src:url("fonts/mana.woff2") format("woff2"),' +
      'url("fonts/mana.woff") format("woff");font-weight'
  ).replace(
    // MPlantin no se usa en la app (solo Mana). Neutralizamos su @font-face
    // para que no pida cinco ficheros que no existen.
    /@font-face\{font-family:"MPlantin";src:[^}]*?\}/,
    ''
  )
  // Los .map no se empaquetan: dejar el comentario provoca un 404 en
  // devtools cada vez que alguien abre el inspector.
  const noMap = trimmed.replace(/\/\*#\s*sourceMappingURL=[^*]*\*\//g, '')
  writeFileSync(cssPath, noMap)
  console.log('  ok     mana.min.css reescrito a woff2+woff')
}

console.log(`\n${copied} assets copiados a static/vendor/`)
if (missing > 0) {
  console.error(`${missing} assets no encontrados.`)
  process.exit(1)
}
console.log('Ahora ejecuta: npm run build:css')
