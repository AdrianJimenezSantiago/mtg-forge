/**
 * Cliente HTTP centralizado.
 *
 * Antes había ~100 llamadas sueltas a `fetch()` repartidas entre los
 * templates: 47 en deck.html, 26 en settings.html, 15 en pdf_studio.html. Cada
 * una decidía por su cuenta si comprobar `res.ok`, cómo extraer el mensaje de
 * error del backend y si mostrar un toast. El resultado era que algunos
 * errores salían como "[object Object]", otros como "Error" a secas y unos
 * cuantos se perdían en silencio.
 *
 * Este módulo concentra esa lógica en un sitio, lo que da gratis:
 *
 *   - mensajes de error coherentes, leídos del campo `detail` de FastAPI
 *   - cancelación con AbortController en cualquier petición
 *   - reintento con backoff para errores de red transitorios
 *   - deduplicación de GET idénticos en vuelo
 *   - un único punto donde añadir cabeceras o cambiar el prefijo de la API
 *
 * Uso:
 *     import { api, ApiError } from '/static/js/api.js'
 *     const deck = await api.decks.get(12)
 */
(function () {
  'use strict'


/** Error con el código HTTP y el detalle que devolvió el backend. */
class ApiError extends Error {
  constructor(status, detail, url) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.url = url
  }

  /** ¿Merece la pena reintentar? Los 4xx son culpa de la petición, no del momento. */
  get isRetryable() {
    return this.status === 0 || this.status === 429 || this.status >= 500
  }

  /** ¿Es un fallo de validación de FastAPI (422)? */
  get isValidation() {
    return this.status === 422
  }
}

/**
 * Extrae un mensaje legible del cuerpo de un error.
 *
 * FastAPI usa `detail`, que puede ser una cadena o —en los 422— una lista de
 * objetos con `loc` y `msg`. Sin este tratamiento, un error de validación se
 * mostraba al usuario como "[object Object]".
 */
async function extractDetail(res) {
  let body
  try {
    body = await res.json()
  } catch {
    return res.statusText || `HTTP ${res.status}`
  }

  const detail = body?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const field = Array.isArray(d.loc) ? d.loc.slice(1).join('.') : ''
        return field ? `${field}: ${d.msg}` : d.msg
      })
      .join('; ')
  }
  return body?.message || res.statusText || `HTTP ${res.status}`
}

const RETRY_DELAYS_MS = [300, 900, 2400]

/** GET idénticos en vuelo: se comparte la misma promesa en vez de duplicar. */
const inFlight = new Map()

/**
 * Ejecuta una petición.
 *
 * @param {string} method
 * @param {string} path
 * @param {object} [options]
 * @param {any}    [options.body]    se serializa como JSON
 * @param {object} [options.params]  query string; se omiten null/undefined/''
 * @param {AbortSignal} [options.signal]
 * @param {number} [options.retries] reintentos para errores transitorios
 * @param {boolean} [options.dedupe] compartir GET idénticos en vuelo
 * @param {boolean} [options.raw]    devolver la Response sin parsear
 */
async function request(method, path, options = {}) {
  const {
    body, params, signal, retries = 0, dedupe = false, raw = false,
  } = options

  let url = path
  if (params) {
    const qs = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      // Omitir vacíos evita `?set=&sort=` en la URL, que además obligaría al
      // backend a distinguir "no enviado" de "enviado vacío".
      if (value === null || value === undefined || value === '') continue
      if (Array.isArray(value)) {
        if (value.length) qs.set(key, value.join(','))
      } else {
        qs.set(key, String(value))
      }
    }
    const query = qs.toString()
    if (query) url += (url.includes('?') ? '&' : '?') + query
  }

  const key = `${method} ${url}`
  if (dedupe && method === 'GET' && inFlight.has(key)) {
    return inFlight.get(key)
  }

  const run = async () => {
    let lastError
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        const res = await fetch(url, {
          method,
          signal,
          headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
          body: body !== undefined ? JSON.stringify(body) : undefined,
        })

        if (!res.ok) {
          throw new ApiError(res.status, await extractDetail(res), url)
        }
        if (raw) return res
        // 204 No Content: no hay cuerpo que parsear.
        if (res.status === 204) return null
        const text = await res.text()
        return text ? JSON.parse(text) : null
      } catch (err) {
        // Una cancelación explícita no es un fallo: se propaga tal cual para
        // que el llamante distinga "el usuario cerró el modal" de "falló".
        if (err.name === 'AbortError') throw err

        // Un fallo de red no produce Response, así que no hay status: se
        // normaliza a 0 para que `isRetryable` lo trate como transitorio.
        lastError = err instanceof ApiError
          ? err
          : new ApiError(0, err.message || 'Error de red', url)

        if (attempt < retries && lastError.isRetryable) {
          const delay = RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)]
          await new Promise((r) => setTimeout(r, delay))
          continue
        }
        throw lastError
      }
    }
    throw lastError
  }

  const promise = run()
  if (dedupe && method === 'GET') {
    inFlight.set(key, promise)
    promise.finally(() => inFlight.delete(key))
  }
  return promise
}

const get = (path, options) => request('GET', path, options)
const post = (path, body, options) => request('POST', path, { ...options, body })
const patch = (path, body, options) => request('PATCH', path, { ...options, body })
const put = (path, body, options) => request('PUT', path, { ...options, body })
const del = (path, options) => request('DELETE', path, options)

/**
 * Superficie de la API agrupada por dominio.
 *
 * Que las rutas vivan aquí y no repartidas por los templates significa que
 * renombrar un endpoint es un cambio en un solo fichero, y que se puede ver de
 * un vistazo qué consume el frontend.
 */
const api = {
  decks: {
    list:        (signal) => get('/api/decks/', { signal, dedupe: true }),
    withActivity:(signal) => get('/api/decks/_/with-activity', { signal, dedupe: true }),
    get:         (id, signal) => get(`/api/decks/${id}`, { signal }),
    create:      (payload) => post('/api/decks/import/text', payload),
    importUrl:   (payload) => post('/api/decks/import/url', payload),
    importMoxfield: (payload) => post('/api/decks/import/moxfield', payload),
    supportedSites: () => get('/api/decks/import/supported-sites', { dedupe: true }),
    update:      (id, payload) => patch(`/api/decks/${id}`, payload),
    remove:      (id) => del(`/api/decks/${id}`),
    duplicate:   (id) => post(`/api/decks/${id}/duplicate`),
    validation:  (id) => get(`/api/decks/${id}/validation`),
    activity:    (id, signal) => get(`/api/decks/${id}/activity`, { signal }),
    undo:        (id, eventId) => post(`/api/decks/${id}/activity/${eventId}/undo`),
    searchCards: (q, signal) => get('/api/decks/_/search-cards', { params: { q }, signal }),
    autocomplete:(q, signal) => get('/api/decks/_/autocomplete', { params: { q }, signal }),
    localize:    (id, lang) => post(`/api/decks/${id}/localize`, { lang }),
    supportedLangs: () => get('/api/decks/_/supported-langs', { dedupe: true }),
    tokensAnalysis: (id) => get(`/api/decks/${id}/tokens-analysis`),
    addTokens:   (id, payload) => post(`/api/decks/${id}/tokens-add-many`, payload),
  },

  cards: {
    add:      (deckId, payload) => post(`/api/decks/${deckId}/cards`, payload),
    update:   (deckId, cardId, payload) =>
                patch(`/api/decks/${deckId}/cards/${cardId}`, payload),
    remove:   (deckId, cardId) => del(`/api/decks/${deckId}/cards/${cardId}`),
    toggle:   (deckId, cardId) => post(`/api/decks/${deckId}/cards/${cardId}/toggle`),
    clearRole:(deckId, role) => del(`/api/decks/${deckId}/role/${role}`),
    addRelated: (deckId, cardId, payload) =>
                post(`/api/decks/${deckId}/cards/${cardId}/add-related`, payload),
    changeArt:(deckId, payload) => post(`/api/decks/${deckId}/cards/change-art`, payload),

    /**
     * Página de opciones de arte.
     *
     * Este endpoint puede devolver cientos de resultados, así que siempre se
     * pasa `signal`: cerrar el modal debe cortar las peticiones en vuelo.
     */
    prints: (deckId, cardId, { offset = 0, limit = 60, sort = 'released_desc',
                               q = '', only = [], signal } = {}) =>
      get(`/api/decks/${deckId}/cards/${cardId}/prints`, {
        params: { offset, limit, sort, q, only }, signal,
      }),

    /**
     * Todas las opciones de arte en un único array plano.
     *
     * El endpoint pasó de devolver un array a devolver un sobre paginado
     * (`{items, custom, total, has_more}`). Ese cambio dejó atrás dos
     * consumidores que seguían haciendo `arts.filter(...)` sobre el sobre y
     * fallaban con "arts.filter is not a function": el mini selector del PDF
     * Studio y el panel lateral del editor.
     *
     * Este helper existe para que nadie tenga que volver a paginar a mano.
     * Devuelve exactamente lo que devolvía el contrato antiguo —los artes
     * custom primero, después las impresiones oficiales— así que un consumidor
     * que solo quiera "dame todo" no necesita saber que hay paginación.
     *
     * Ojo: para una carta muy reimpresa esto son varias peticiones. Si lo que
     * necesitas es pintar una rejilla, usa `prints()` y ve anexando páginas.
     */
    allPrints: async (deckId, cardId, { signal, maxPages = 20 } = {}) => {
      const PAGE = 300     // el máximo que admite el endpoint
      const first = await api.cards.prints(deckId, cardId, { limit: PAGE, signal })
      const out = [...(first.custom || []), ...(first.items || [])]

      let offset = PAGE
      let pages = 1
      let more = first.has_more
      while (more && pages < maxPages) {
        const page = await api.cards.prints(deckId, cardId, {
          offset, limit: PAGE, signal,
        })
        out.push(...(page.items || []))
        offset += PAGE
        pages += 1
        more = page.has_more
      }
      return out
    },
  },

  preload: {
    start:    (deckId) => post(`/api/decks/${deckId}/preload-prints`),
    progress: (deckId, signal) => get(`/api/decks/${deckId}/preload-progress`, { signal }),
    cancel:   (deckId) => post(`/api/decks/${deckId}/preload-cancel`),
  },

  build: {
    estimate: (deckId, params) => get(`/api/decks/${deckId}/estimate`, { params }),
    xml:      (deckId, payload) => post(`/api/decks/${deckId}/build-xml`, payload),
    pdf:      (deckId, payload) => post(`/api/decks/${deckId}/build-pdf`, payload),
    images:   (deckId, payload) => post(`/api/decks/${deckId}/export-images`, payload),
    progress: (deckId, signal) => get(`/api/decks/${deckId}/build-progress`, { signal }),
    decklist: (deckId, format) =>
                get(`/api/decks/${deckId}/decklist`, { params: { format } }),
  },

  cardback: {
    get:    (deckId) => get(`/api/decks/${deckId}/cardback-settings`),
    set:    (deckId, payload) => put(`/api/decks/${deckId}/cardback-settings`, payload),
    clear:  (deckId) => del(`/api/decks/${deckId}/cardback-settings`),
  },

  drives: {
    search:     (params, signal) => get('/api/drives/search', { params, signal }),
    cardbacks:  (params, signal) => get('/api/drives/cardbacks', { params, signal }),
    // `dedupe` porque varias partes de la interfaz consultan las estadísticas
    // a la vez al abrir el selector.
    stats:      (signal) => get('/api/drives/stats', { signal, dedupe: true }),
    indexBatch: (payload) => post('/api/drives/index-batch', payload),
    indexProgress: (signal) => get('/api/drives/index-progress', { signal }),
    indexCancel:() => post('/api/drives/index-cancel'),
    indexClear: () => post('/api/drives/index-clear'),
    similar:    (fileId, params) => get(`/api/drives/phash/similar/${fileId}`, { params }),
    phashStats: () => get('/api/drives/phash/stats'),
    phashCompute: (payload) => post('/api/drives/phash/compute', payload),
    rebuildFts: () => post('/api/drives/rebuild-fts5'),
  },

  sources: {
    list:    () => get('/api/art-sources/'),
    create:  (payload) => post('/api/art-sources/', payload),
    update:  (id, payload) => patch(`/api/art-sources/${id}`, payload),
    remove:  (id) => del(`/api/art-sources/${id}`),
    index:   (id) => post(`/api/art-sources/${id}/index`),
    clearIndex: (id) => del(`/api/art-sources/${id}/index`),
    validate:(payload) => post('/api/art-sources/validate', payload),
    restoreCatalog: () => post('/api/art-sources/restore-catalog'),
    catalogInfo: () => get('/api/art-sources/catalog-info'),
  },

  customArt: {
    list:    () => get('/api/custom-art/'),
    rescan:  () => post('/api/custom-art/rescan'),
    fromUrl: (payload) => post('/api/custom-art/from-url', payload),
    remove:  (id) => del(`/api/custom-art/${id}`),
  },

  collection: {
    sidebarStats: (signal) => get('/api/collection/sidebar-stats', { signal, dedupe: true }),
    recentDecks:  (signal) => get('/api/collection/recent-decks', { signal, dedupe: true }),
    sets:         (signal) => get('/api/collection/sets', { signal, dedupe: true }),
    setCards:     (code, signal) => get(`/api/collection/sets/${code}/cards`, { signal }),
    toggleOwned:  (payload) => post('/api/collection/toggle-owned', payload),
    batchToggle:  (payload) => post('/api/collection/batch-toggle', payload),
    stats:        () => get('/api/collection/stats'),
  },

  settings: {
    get:   () => get('/api/settings/'),
    paths: () => get('/api/settings/paths'),
    save:  (payload) => put('/api/settings/', payload),
  },

  bulk: {
    status:   () => get('/api/bulk/status'),
    check:    (kind) => get('/api/bulk/check', { params: { kind } }),
    sync:     (kind, force) => post('/api/bulk/sync', undefined, { params: { kind, force } }),
    cancel:   () => post('/api/bulk/cancel'),
    progress: (signal) => get('/api/bulk/progress', { signal }),
  },

  thumbs: {
    stats: () => get('/api/thumbs/stats'),
    clear: () => post('/api/thumbs/clear'),
  },

  runs: {
    list:   () => get('/api/runs'),
    remove: (id) => del(`/api/runs/${id}`),
  },

  backup: {
    create: () => post('/api/backup'),
  },

  autofill: {
    status: () => get('/api/mpc-autofill/status'),
    launch: (payload) => post('/api/mpc-autofill/launch', payload),
  },
}

/**
 * Envuelve una llamada mostrando un toast si falla.
 *
 * Ahorra el `try/catch` repetido en cada manejador de la interfaz. Devuelve
 * `undefined` cuando hay error, así que el llamante puede comprobarlo si le
 * interesa distinguir el caso.
 *
 *     const deck = await withToast(() => api.decks.get(id), 'No se pudo cargar')
 *     if (!deck) return
 */
async function withToast(fn, fallbackMessage = '') {
  try {
    return await fn()
  } catch (err) {
    if (err.name === 'AbortError') return undefined
    const t = window._t || ((k) => k)
    window.Alpine?.store('ui')?.error(
      t('common_error'),
      err.detail || err.message || fallbackMessage
    )
    return undefined
  }
}

/**
 * Sondea un endpoint hasta que una condición se cumple.
 *
 * Los endpoints de progreso (build de PDF, indexado, bulk data) seguían todos
 * el mismo patrón de `setInterval` copiado y pegado, cada uno con su propia
 * forma de parar y su propia fuga cuando el usuario cambiaba de página.
 *
 * @param {Function} fetcher    devuelve el estado actual
 * @param {Function} isDone     recibe el estado, devuelve true para parar
 * @param {object}  [opts]
 * @param {number}  [opts.intervalMs]
 * @param {number}  [opts.timeoutMs]  0 = sin límite
 * @param {Function}[opts.onTick]     se llama con cada estado intermedio
 * @param {AbortSignal} [opts.signal]
 */
async function poll(fetcher, isDone, opts = {}) {
  const { intervalMs = 700, timeoutMs = 0, onTick, signal } = opts
  const startedAt = Date.now()

  for (;;) {
    if (signal?.aborted) throw new DOMException('Cancelado', 'AbortError')
    const state = await fetcher()
    onTick?.(state)
    if (isDone(state)) return state
    if (timeoutMs && Date.now() - startedAt > timeoutMs) {
      throw new Error('Se agotó el tiempo de espera')
    }
    await new Promise((r) => setTimeout(r, intervalMs))
  }
}

// Puente para el código que aún vive inline en los templates y no puede usar
// `import`. Se elimina cuando toda la interfaz esté modularizada.
window.api = api
window.ApiError = ApiError
window.apiRequest = request
window.withToast = withToast
window.apiPoll = poll
})()
