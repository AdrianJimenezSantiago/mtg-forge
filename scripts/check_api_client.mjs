/**
 * Pruebas del cliente API en un DOM simulado.
 *
 * La suite de Python cubre el backend y la estructura del frontend, pero no
 * ejecuta la lógica JavaScript. Este fichero cubre el hueco para la parte del
 * cliente que más daño hace cuando falla: la que adapta contratos de la API.
 *
 * Motivo concreto: `/prints` pasó de devolver un array a devolver un sobre
 * paginado, y dos consumidores se quedaron atrás haciendo `.filter()` sobre el
 * sobre. `allPrints` existe para que eso no vuelva a pasar, así que conviene
 * que esté probado de verdad y no solo por inspección del código.
 *
 * Uso:  node scripts/check_api_client.mjs
 */
import { JSDOM } from 'jsdom'
import { readFileSync } from 'node:fs'

let failures = 0

function check(name, condition, detail = '') {
  if (condition) {
    console.log(`  ok     ${name}`)
  } else {
    console.error(`  FALLO  ${name}${detail ? ' — ' + detail : ''}`)
    failures++
  }
}

/** Crea un window con api.js cargado y un servidor falso paginado. */
function makeWindow({ total, customs = 2 }) {
  const dom = new JSDOM('<body></body>', {
    runScripts: 'outside-only', url: 'http://localhost/',
  })
  const w = dom.window
  const state = { calls: 0 }

  w.fetch = async (url) => {
    state.calls++
    const u = new URL(url, 'http://localhost')
    const offset = Number(u.searchParams.get('offset') || 0)
    const limit = Number(u.searchParams.get('limit') || 60)
    const items = []
    for (let i = offset; i < Math.min(offset + limit, total); i++) {
      items.push({ kind: 'scryfall', face: 'front', id: i })
    }
    const custom = offset === 0
      ? Array.from({ length: customs }, (_, i) => ({
          kind: 'custom', face: i === 0 ? 'back' : 'front', id: `c${i}`,
        }))
      : []
    const body = {
      items, custom, total, offset, limit,
      has_more: offset + limit < total,
    }
    return {
      ok: true, status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
    }
  }

  w.eval(readFileSync(new URL('../static/js/api.js', import.meta.url), 'utf8'))
  return { w, state }
}

console.log('allPrints — conjunto que cabe en una página')
{
  const { w } = makeWindow({ total: 250 })
  const arts = await w.api.cards.allPrints(1, 2)
  check('devuelve un array', Array.isArray(arts))
  check('incluye customs e impresiones', arts.length === 252, `son ${arts.length}`)
  check('los customs van primero', arts[0].kind === 'custom')
  check('.filter() funciona', typeof arts.filter === 'function')
  check('conserva las caras traseras',
    arts.filter(a => a.face === 'back').length === 1)
}

console.log('\nallPrints — conjunto que necesita varias páginas')
{
  const { w, state } = makeWindow({ total: 900 })
  const arts = await w.api.cards.allPrints(1, 2)
  check('recorre todas las páginas', arts.length === 902, `son ${arts.length}`)
  check('hace más de una petición', state.calls > 1, `hizo ${state.calls}`)
  check('no duplica elementos',
    new Set(arts.map(a => `${a.kind}${a.id}`)).size === arts.length)
}

console.log('\nallPrints — casos límite')
{
  const { w } = makeWindow({ total: 0, customs: 0 })
  const arts = await w.api.cards.allPrints(1, 2)
  check('un conjunto vacío devuelve []', Array.isArray(arts) && arts.length === 0)
}
{
  // Un servidor que nunca baje `has_more` no debe colgar el navegador.
  const { w, state } = makeWindow({ total: 999999 })
  const arts = await w.api.cards.allPrints(1, 2, { maxPages: 3 })
  check('respeta el tope de páginas', state.calls <= 3, `hizo ${state.calls}`)
  check('devuelve lo que alcanzó a leer', arts.length > 0)
}

console.log('\nExtracción de errores')
{
  const dom = new JSDOM('<body></body>', {
    runScripts: 'outside-only', url: 'http://localhost/',
  })
  const w = dom.window
  // Un 422 de FastAPI trae `detail` como lista de objetos; sin tratarlo, el
  // usuario veía "[object Object]".
  w.fetch = async () => ({
    ok: false, status: 422, statusText: 'Unprocessable',
    json: async () => ({ detail: [{ loc: ['body', 'name'], msg: 'obligatorio' }] }),
    text: async () => '',
  })
  w.eval(readFileSync(new URL('../static/js/api.js', import.meta.url), 'utf8'))
  try {
    await w.apiRequest('GET', '/x')
    check('un 422 lanza', false)
  } catch (e) {
    check('un 422 lanza', true)
    check('el mensaje es legible', /name.*obligatorio/.test(e.message), e.message)
    check('no dice [object Object]', !/\[object Object\]/.test(e.message))
  }
}

console.log(failures === 0
  ? '\nTodas las comprobaciones del cliente API pasan.'
  : `\n${failures} comprobacion(es) fallan.`)
process.exit(failures)
