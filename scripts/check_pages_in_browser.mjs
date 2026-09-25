import { JSDOM, VirtualConsole } from 'jsdom'

const BASE = process.argv[2] || 'http://127.0.0.1:8000'
const PAGES = ['/', '/decks', '/history', '/collection', '/settings', '/print-planner', '/art-library', '/calibrate']

const REAL_FAILURE =
  /is not defined|Alpine Expression Error|ReferenceError|TypeError/

const ENVIRONMENT_NOISE = /Not implemented|localStorage|matchMedia|scrollTo|canvas/

let failures = 0

for (const page of PAGES) {
  const errors = []
  const virtualConsole = new VirtualConsole()
  virtualConsole.on('jsdomError', (e) => errors.push(String(e.message)))
  virtualConsole.on('error', (...args) => errors.push(args.map(String).join(' ')))
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
