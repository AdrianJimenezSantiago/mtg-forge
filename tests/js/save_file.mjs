import { JSDOM } from 'jsdom'
import { readFileSync } from 'node:fs'
// window.saveFile: "Guardar como" del sistema donde existe, descarga normal donde no.
const src = readFileSync(process.argv[2], 'utf8')
const problems = []

function makeWindow({ picker }) {
  const dom = new JSDOM('<body></body>', { runScripts: 'outside-only', url: 'http://127.0.0.1/' })
  const w = dom.window
  w.eval(src)
  const log = []
  w._t = (k) => k
  w.toast = (title, message, type) => log.push(['toast', type, title, message])
  w.apiErrorDetail = async (r) => `HTTP ${r.status}`
  w.fetch = async (url) => { log.push(['fetch', url]); return new Response(`contenido de ${url}`) }
  w.HTMLAnchorElement.prototype.click = function () { log.push(['download', this.getAttribute('href'), this.download]) }
  if (picker) {
    w.showSaveFilePicker = async (opts) => {
      log.push(['picker', opts.suggestedName, opts.types?.[0]?.description])
      if (picker === 'cancel') throw new w.DOMException('cancel', 'AbortError')
      const chunks = []
      w.__written = chunks
      return {
        name: 'elegido.pdf',
        createWritable: async () => new WritableStream({ write: (c) => { chunks.push(Buffer.from(c).toString()) } }),
        written: chunks,
      }
    }
  }
  return { w, log }
}

const producer = (log, url = '/api/exports/x.pdf') => async () => { log.push(['produce']); return url }

{
  const { w, log } = makeWindow({ picker: 'ok' })
  const ok = await w.saveFile(producer(log), w.saveFileName('Mazo: "A/B"', 'pdf'))
  const kinds = log.map(e => e[0])
  if (!ok) problems.push('picker: no devuelve true')
  if (JSON.stringify(kinds) !== JSON.stringify(['picker', 'produce', 'fetch', 'toast'])) problems.push({ order: kinds })
  if (log[0][1] !== 'Mazo_ _A_B_.pdf' || log[0][2] !== 'PDF') problems.push({ suggested: log[0] })
  if (log[3][1] !== 'success' || log[3][3] !== 'elegido.pdf') problems.push({ toast: log[3] })
  if (w.__written.join('') !== 'contenido de /api/exports/x.pdf') problems.push({ written: w.__written })
}
{
  const { w, log } = makeWindow({ picker: 'cancel' })
  const ok = await w.saveFile(producer(log), 'a.pdf')
  if (ok || log.some(e => e[0] === 'produce' || e[0] === 'fetch')) problems.push({ cancelGenerated: log })
}
{
  const { w, log } = makeWindow({ picker: false })
  const ok = await w.saveFile(producer(log, '/api/exports/y.zip'), 'y.zip')
  const download = log.find(e => e[0] === 'download')
  if (!ok || !download || download[1] !== '/api/exports/y.zip' || download[2] !== 'y.zip') problems.push({ fallback: log })
}
{
  const { w, log } = makeWindow({ picker: 'ok' })
  w.fetch = async () => new Response('no', { status: 500 })
  const ok = await w.saveFile('/api/exports/z.pdf', 'z.pdf')
  const toast = log.find(e => e[0] === 'toast')
  if (ok || !toast || toast[1] !== 'error') problems.push({ httpError: log })
}
console.log(JSON.stringify({ problems }))
