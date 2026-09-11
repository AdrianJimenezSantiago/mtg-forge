/**
 * Humo de arranque de la interfaz en un navegador simulado.
 *
 * Por qué existe
 * --------------
 * Al extraer el JavaScript de los templates a ficheros aparte, la aplicación
 * se rompió en el navegador ("Alpine Expression Error: importPanel is not
 * defined", vistas en blanco) con la suite de Python entera en verde. Los
 * tests comprobaban los ficheros AISLADOS y la estructura del HTML, pero nadie
 * cargaba una página de verdad y ejecutaba sus scripts en orden.
 *
 * Este script hace exactamente eso: pide las páginas al servidor, las carga en
 * jsdom ejecutando los scripts como haría un navegador, deja que Alpine
 * arranque, y falla si alguna expresión no se resuelve.
 *
 * Limitación conocida: jsdom NO ejecuta `<script type="module">`. Es una de las
 * razones por las que la interfaz usa scripts clásicos con `defer` — así esta
 * comprobación es representativa. Si algún día se vuelve a los módulos, este
 * script dejará de detectar nada y habrá que sustituirlo por un navegador real.
 *
 * Uso:
 *     python -m mpc_forge &
 *     node scripts/check_pages_in_browser.mjs [http://127.0.0.1:8000]
 */
import { JSDOM, VirtualConsole } from 'jsdom'

const BASE = process.argv[2] || 'http://127.0.0.1:8000'
const PAGES = ['/', '/history', '/collection', '/settings', '/print-planner', '/art-library', '/calibrate']

// Lo que este script vigila: que Alpine resuelva todas las expresiones.
//
// `TypeError` está en la lista y debe seguir estando. Se quitó en su momento
// porque el stub de `fetch` devolvía objetos vacíos y generaba TypeErrors
// artificiales; ahora que `fetch` se reenvía al servidor real ese ruido ya no
// existe, y quitarlo dejó pasar un fallo de verdad: una vista que leía
// `result.x` antes de tener resultado.
const REAL_FAILURE =
  /is not defined|Alpine Expression Error|ReferenceError|TypeError/

// Ruido del entorno, no de la aplicación: jsdom no implementa estas APIs.
const ENVIRONMENT_NOISE = /Not implemented|localStorage|matchMedia|scrollTo|canvas/

let failures = 0

for (const page of PAGES) {
  const errors = []
  const virtualConsole = new VirtualConsole()
  virtualConsole.on('jsdomError', (e) => errors.push(String(e.message)))
  virtualConsole.on('error', (...args) => errors.push(args.map(String).join(' ')))
  // OJO: Alpine reporta los errores de expresión por console.warn, no por
  // console.error. Descartar los warn dejaba pasar exactamente la clase de
  // fallo que este script existe para detectar.
  virtualConsole.on('warn', (...args) => errors.push(args.map(String).join(' ')))
  virtualConsole.on('log', () => {})
  virtualConsole.on('info', () => {})

  let dom
  try {
    dom = await JSDOM.fromURL(BASE + page, {
      runScripts: 'dangerously',
      resources: 'usable',
      virtualConsole,
      pretendToBeVisual: true,
      beforeParse(window) {
        // jsdom no implementa fetch. En vez de devolver respuestas falsas —que
        // provocan TypeErrors artificiales cuando el componente espera una
        // lista y recibe un objeto— se reenvía al servidor real con el fetch
        // de Node. Así la comprobación es de verdad de extremo a extremo.
        window.fetch = async (input, init) => {
          const url = typeof input === 'string' ? input : input.url
          const absolute = url.startsWith('http') ? url : BASE + url
          const response = await fetch(absolute, init)
          const body = await response.text()
          return {
            ok: response.ok,
            status: response.status,
            statusText: response.statusText,
            json: async () => JSON.parse(body),
            text: async () => body,
          }
        }
        window.matchMedia = () => ({
          matches: false, addEventListener() {}, removeEventListener() {},
        })
      },
    })
  } catch (e) {
    console.log(`FALLO ${page.padEnd(14)} no se pudo cargar: ${e.message}`)
    failures++
    continue
  }

  // Tiempo para los `defer`, el arranque de Alpine y los init() asíncronos.
  await new Promise((resolve) => setTimeout(resolve, 2500))

  const alpineLoaded = typeof dom.window.Alpine !== 'undefined'
  const relevant = errors.filter(
    (e) => REAL_FAILURE.test(e) && !ENVIRONMENT_NOISE.test(e)
  )

  const ok = relevant.length === 0 && alpineLoaded
  console.log(
    `${ok ? 'OK   ' : 'FALLO'} ${page.padEnd(14)} ` +
    `Alpine=${alpineLoaded} errores=${relevant.length}`
  )
  for (const e of relevant.slice(0, 5)) {
    console.log('        ' + e.split('\n')[0])
  }
  if (!ok) failures++
  dom.window.close()
}

if (failures > 0) {
  console.error(`\n${failures} pagina(s) no arrancan correctamente.`)
} else {
  console.log('\nTodas las paginas arrancan sin errores de expresion.')
}
process.exit(failures)
