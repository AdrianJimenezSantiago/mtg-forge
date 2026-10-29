import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const src = readFileSync(process.argv[2], 'utf8')
const problems = []

function makeStore() {
  const calls = []
  const events = []
  let init = null
  let store = null
  const w = {
    console, URL, File, FormData, Response, setTimeout, CustomEvent: class { constructor(type, opts) { this.type = type; this.detail = opts?.detail } },
    requestAnimationFrame: (fn) => fn(),
    document: { addEventListener: (type, fn) => { if (type === 'alpine:init') init = fn } },
    dispatchEvent: (ev) => events.push(ev.type),
    Alpine: { store: (name, value) => { store = value } },
    _t: (key) => key,
    toast: () => {},
    confirmDialog: async () => true,
  }
  w.window = w
  w.fetch = async (url, opts = {}) => {
    const body = opts.body instanceof FormData ? Object.fromEntries(opts.body) : (opts.body ? JSON.parse(opts.body) : null)
    calls.push({ url, method: opts.method || 'GET', body })
    if (url.endsWith('/plan-files')) {
      return Response.json({
        deck_id: 7, source: 'files', rejected: [],
        summary: { cards: 3, match: 1, same: 0, keep: 0, missing: 2, backs: 0, unused: 1, rejected: 0 },
        items: [
          { deck_card_id: 1, name: 'Sol Ring', role: 'mainboard', quantity: 1, face: 'front', status: 'match',
            current_custom_art_id: null, proposed: 0, candidates: [{ ref: 0, filename: 'sol_ring.png', variant: null, face: 'front', thumb_url: null }] },
          { deck_card_id: 2, name: 'Arcane Signet', role: 'mainboard', quantity: 1, face: 'front', status: 'missing',
            current_custom_art_id: null, proposed: null, candidates: [] },
          { deck_card_id: 3, name: 'Counterspell', role: 'mainboard', quantity: 1, face: 'front', status: 'missing',
            current_custom_art_id: null, proposed: null, candidates: [] },
        ],
        unused: [{ ref: 1, filename: 'Arcane Signte.png', variant: null, face: 'front', thumb_url: null, suggestions: [2] }],
      })
    }
    if (url === '/api/custom-art/upload') {
      if (body.card_name === 'Arcane Signet') return Response.json({ detail: { code: 'too_small', message: 'x' } }, { status: 400 })
      return Response.json({ id: 100 + calls.length })
    }
    if (url.endsWith('/custom-art/apply')) return Response.json({ changed: body.assignments.length, unchanged: 0, invalid: 0, event_id: 9, snapshot_id: 4 })
    if (url === '/api/custom-art/folders') return Response.json([])
    if (url === '/api/settings/paths') return Response.json({ custom_art_dir: '/tmp/custom' })
    return new Response('{}', { status: 404 })
  }
  vm.createContext(w)
  vm.runInContext(src, w)
  init()
  return { store, calls, events }
}

const file = (name) => ({ file: new File(['x'], name), path: `Mazo/${name}` })

{
  const { store, calls, events } = makeStore()
  store.start({ deckId: 7 })
  await store.handleFiles([file('sol_ring.png'), file('Arcane Signte.png'), file('notas.txt'), file('.DS_Store')])
  const plan = calls.find((c) => c.url.endsWith('/plan-files'))
  if (JSON.stringify(plan?.body.filenames) !== JSON.stringify(['Mazo/sol_ring.png', 'Mazo/Arcane Signte.png'])) problems.push({ filenames: plan?.body })
  if (store.ignored !== 1) problems.push({ ignored: store.ignored })
  if (store.step !== 'review' || store.rows.length !== 1 || store.missing.length !== 2) problems.push({ review: [store.step, store.rows.length, store.missing.length] })

  store.assignUnused(store.unused[0], '2')
  if (store.unused.length || store.missing.length !== 1 || store.pending.length !== 2) problems.push({ assign: [store.unused.length, store.missing.length, store.pending.length] })

  await store.apply()
  const uploads = calls.filter((c) => c.url === '/api/custom-art/upload')
  if (uploads.length !== 2 || uploads.some((u) => u.body.fit !== 'crop')) problems.push({ uploads: uploads.map((u) => u.body) })
  if (JSON.stringify(uploads.map((u) => u.body.card_name).sort()) !== JSON.stringify(['Arcane Signet', 'Sol Ring'])) problems.push({ names: uploads })
  const apply = calls.find((c) => c.url.endsWith('/custom-art/apply'))
  if (apply?.body.assignments.length !== 1 || apply.body.assignments[0].deck_card_id !== 1) problems.push({ apply: apply?.body })
  if (store.step !== 'done' || store.result.failed.length !== 1 || store.result.failed[0].card !== 'Arcane Signet') problems.push({ result: store.result })
  if (!events.includes('custom-art-applied')) problems.push({ events })
}
{
  const { store, calls } = makeStore()
  store.start({ deckId: 7 })
  store.step = 'working'
  store.close()
  if (!store.open) problems.push({ closedWhileWorking: true })
  store.step = 'source'
  await store.handleFiles([file('leeme.txt')])
  if (!store.error || calls.some((c) => c.url.includes('/plan'))) problems.push({ noImages: store.error })
}
console.log(JSON.stringify({ problems }))
