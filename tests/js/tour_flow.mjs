import fs from 'fs'; import vm from 'vm';
const [stepsSrc, tourSrc] = process.argv.slice(2).map((p) => fs.readFileSync(p, 'utf8'));

function backend() {
  const state = { seen: [], auto: true }
  const posts = []
  return {
    state, posts,
    async fetch(path, opts = {}) {
      const method = opts.method || 'GET'
      const body = opts.body ? JSON.parse(opts.body) : null
      if (method === 'POST' && path === '/api/tour/seen') {
        posts.push(body.tour)
        if (!state.seen.includes(body.tour)) state.seen.push(body.tour)
      } else if (method === 'PUT') state.auto = body.auto
      else if (method === 'DELETE') { state.seen = []; state.auto = true }
      return { ok: true, json: async () => JSON.parse(JSON.stringify(state)) }
    },
  }
}

async function visit(server, path, present, script) {
  let now = 0
  const timers = []
  const el = { getBoundingClientRect: () => ({ left: 300, top: 200, right: 400, bottom: 260, width: 100, height: 60 }),
    scrollIntoView() {}, isConnected: true, parentElement: null }
  const document = {
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn) },
    querySelectorAll(sel) {
      const m = /data-tour="([^"]+)"/.exec(sel)
      return m && present.includes(m[1]) ? [el] : []
    },
    querySelector() { return null },
    getElementById() { return null },
    body: {},
  }
  const store = {}
  const ctx = {
    console, document, location: { pathname: path },
    fetch: server.fetch,
    Date: { now: () => now },
    setTimeout: (fn, ms) => { timers.push({ at: now + (ms || 0), fn }); return timers.length },
    requestAnimationFrame: (fn) => ctx.setTimeout(fn, 16),
    cancelAnimationFrame() {},
    getComputedStyle: () => ({ visibility: 'visible', overflowX: 'visible', overflowY: 'visible' }),
    Alpine: { store: (name, def) => (def ? (store[name] = def) : store[name]) },
  }
  ctx.window = Object.assign(ctx, { innerWidth: 1280, innerHeight: 800, matchMedia: () => ({ matches: true }),
    addEventListener() {}, removeEventListener() {} })
  vm.createContext(ctx)
  vm.runInContext(stepsSrc, ctx)
  vm.runInContext(tourSrc, ctx)
  for (const fn of document.listeners['alpine:init']) fn()
  const tour = store.tour
  for (const fn of document.listeners['alpine:initialized']) fn()

  const flush = async () => {
    for (let guard = 0; guard < 10000; guard++) {
      await new Promise((r) => setImmediate(r))
      if (!timers.length) { await new Promise((r) => setImmediate(r)); if (!timers.length) return }
      timers.sort((a, b) => a.at - b.at)
      const t = timers.shift()
      now = Math.max(now, t.at)
      t.fn()
    }
    throw new Error('el tutorial no se detiene')
  }
  const shown = []
  const watch = () => { if (tour.active && tour.placed) {
    const key = `${tour.tourId}.${tour.step?.id}`
    if (shown[shown.length - 1] !== key) shown.push(key)
  } }
  const api = {
    tour, flush, shown,
    async step() { await flush(); watch() },
    async next() { tour.next(); await api.step() },
    async completeAll() {
      await api.step()
      while (tour.active) await api.next()
    },
  }
  await api.step()
  if (script) await script(api)
  else await api.completeAll()
  await flush()
  return { shown: [...new Set(shown.map((s) => s.split('.')[0]))], steps: shown, tour }
}

const failures = []
const check = (name, actual, expected) => {
  const a = JSON.stringify(actual), e = JSON.stringify(expected)
  if (a !== e) failures.push(`${name}: ${a} !== ${e}`)
}

const NAV = ['nav-home', 'nav-decks', 'nav-settings', 'nav-tour']
const DECKS = [...NAV, 'decks-import-url', 'decks-import-text', 'decks-empty']
const HISTORY = [...NAV, 'history-decks', 'history-runs']

{
  const s = backend()
  check('primera visita: bienvenida y guía de la vista', (await visit(s, '/decks', DECKS)).shown, ['welcome', 'decks'])
  check('se marca una sola vez cada guía', s.posts, ['welcome', 'decks'])
  check('al volver a la vista no sale nada', (await visit(s, '/decks/', DECKS)).shown, [])
  check('otra vista: solo su guía', (await visit(s, '/history', HISTORY)).shown, ['history'])
  check('y tampoco se repite', (await visit(s, '/history', HISTORY)).shown, [])
  check('sin POST de más', s.posts, ['welcome', 'decks', 'history'])
}

{
  const s = backend()
  check('abandono a mitad', (await visit(s, '/decks', DECKS, async () => {})).shown, ['welcome'])
  check('la bienvenida no se repite; la de la vista sí sale', (await visit(s, '/decks', DECKS)).shown, ['decks'])
}

{
  const s = backend()
  const r = await visit(s, '/decks', DECKS, async (v) => { v.tour.skip(); await v.completeAll() })
  check('saltar la bienvenida no quita la guía de la vista', r.shown, ['welcome', 'decks'])
  check('la saltada cuenta como vista', s.state.seen, ['welcome', 'decks'])
}

{
  const s = backend()
  s.state.seen = ['welcome']
  check('vista sin ningún elemento visible: no sale', (await visit(s, '/history', NAV)).shown, [])
  check('y no se marca como vista', s.state.seen, ['welcome'])
  check('sale cuando ya tiene contenido', (await visit(s, '/history', HISTORY)).shown, ['history'])
}

{
  const s = backend()
  const r = await visit(s, '/decks', DECKS, async (v) => { v.tour.dismissAll(); await v.flush() })
  check('no volver a mostrar', r.shown, ['welcome'])
  check('auto apagado', s.state.auto, false)
  check('apagado no sale nada', (await visit(s, '/history', HISTORY)).shown, [])
}

{
  const s = backend()
  s.state.seen = ['welcome']
  const r = await visit(s, '/decks', [...NAV, 'decks-import-url', 'decks-import-text'], async (v) => {
    await v.next(); await v.next()
    v.tour.next()
    v.tour.skip()
    await v.flush()
  })
  check('saltar durante la espera cierra la guía', [r.tour.active, r.tour.busy], [false, false])
}

{
  const s = backend()
  s.state.seen = ['welcome', 'decks']
  const r = await visit(s, '/decks', DECKS, async (v) => { v.tour.replayPage(); await v.completeAll() })
  check('repetir la guía a mano funciona aunque ya se viera', r.shown, ['decks'])
  check('repetir no duplica el registro', s.posts, [])
}

console.log(JSON.stringify({ failures }))
process.exit(failures.length ? 1 : 0)
