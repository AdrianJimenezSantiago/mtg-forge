(function () {
  'use strict'

  // Motor del tutorial guiado. Las guías están en core/tour-steps.js y el
  // estado (qué guías se han visto) lo guarda el servidor en /api/tour/.

  const PAD = 8
  const GAP = 16
  const MARGIN = 12
  const WAIT_MS = 1500

  const _t = (key) => (window._T && window._T[key]) || key
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
  const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  function pascal(name) {
    return String(name).replace(/(^|[-_])(\w)/g, (_, __, c) => c.toUpperCase())
  }

  // Lucide reemplaza los <i data-lucide> por <svg>, lo que rompe los bindings de
  // Alpine; para un icono que cambia en cada paso se genera el SVG directamente.
  function iconSvg(name) {
    const node = window.lucide?.icons?.[pascal(name || 'sparkles')]
    if (!node) return ''
    const children = node[0] === 'svg' ? node[2] : node
    const attrs = (o) => Object.entries(o || {}).map(([k, v]) => `${k}="${String(v).replace(/"/g, '&quot;')}"`).join(' ')
    const inner = (children || []).map(([tag, a]) => `<${tag} ${attrs(a)}/>`).join('')
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
      `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`
  }

  function visibleElement(selector) {
    for (const sel of [].concat(selector || [])) {
      for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect()
        if (r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden') return el
      }
    }
    return null
  }

  // Parte del elemento que se ve de verdad: recorta por los contenedores con
  // scroll u overflow oculto (p. ej. la hoja del PDF dentro de su columna).
  function visibleRect(el) {
    const r = el.getBoundingClientRect()
    let left = r.left
    let top = r.top
    let right = r.right
    let bottom = r.bottom
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const cs = getComputedStyle(p)
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue
      const pr = p.getBoundingClientRect()
      left = Math.max(left, pr.left)
      top = Math.max(top, pr.top)
      right = Math.min(right, pr.right)
      bottom = Math.min(bottom, pr.bottom)
    }
    return { left, top, right: Math.max(left, right), bottom: Math.max(top, bottom) }
  }

  async function waitFor(selector, ms) {
    const until = Date.now() + ms
    let el = visibleElement(selector)
    while (!el && Date.now() < until) {
      await sleep(100)
      el = visibleElement(selector)
    }
    return el
  }

  async function api(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  }

  // Coloca la tarjeta junto al elemento, en el lado con más sitio, sin salirse
  // de la ventana. Devuelve la posición y el lado elegido (para la flecha).
  function place(rect, card, preferred) {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const space = {
      bottom: vh - rect.bottom,
      top: rect.top,
      right: vw - rect.right,
      left: rect.left,
    }
    const fits = {
      bottom: space.bottom >= card.h + GAP + MARGIN,
      top: space.top >= card.h + GAP + MARGIN,
      right: space.right >= card.w + GAP + MARGIN,
      left: space.left >= card.w + GAP + MARGIN,
    }
    let side = preferred && fits[preferred] ? preferred : null
    if (!side) side = ['bottom', 'top', 'right', 'left'].find((s) => fits[s]) || 'inside'

    const clamp = (v, min, max) => Math.max(min, Math.min(max, v))
    let x
    let y
    if (side === 'bottom' || side === 'top') {
      x = clamp(rect.left + rect.width / 2 - card.w / 2, MARGIN, vw - card.w - MARGIN)
      y = side === 'bottom' ? rect.bottom + GAP : rect.top - card.h - GAP
    } else if (side === 'right' || side === 'left') {
      y = clamp(rect.top + rect.height / 2 - card.h / 2, MARGIN, vh - card.h - MARGIN)
      x = side === 'right' ? rect.right + GAP : rect.left - card.w - GAP
    } else {
      // El elemento ocupa casi toda la ventana: la tarjeta va dentro, abajo.
      x = clamp(rect.left + rect.width / 2 - card.w / 2, MARGIN, vw - card.w - MARGIN)
      y = clamp(rect.bottom - card.h - GAP, MARGIN, vh - card.h - MARGIN)
    }
    const arrow = side === 'bottom' || side === 'top'
      ? clamp(rect.left + rect.width / 2 - x, 20, card.w - 20)
      : clamp(rect.top + rect.height / 2 - y, 20, card.h - 20)
    return { x: Math.round(x), y: Math.round(y), side, arrow: Math.round(arrow) }
  }

  document.addEventListener('alpine:init', () => {
    Alpine.store('tour', {
      ready: false,
      seen: [],
      auto: true,
      active: false,
      tourId: null,
      steps: [],
      index: 0,
      busy: false,
      spot: null,
      pos: { x: 0, y: 0, side: 'center', arrow: 0 },
      // La tarjeta no se ve hasta tener su posición calculada: si se pintara en
      // (0,0) y luego se moviera, el navegador lo contaría como layout shift.
      placed: false,
      _queue: [],
      _target: null,
      _raf: 0,

      get pageTour() { return window.tourForPath?.(location.pathname) || null },
      get step() { return this.steps[this.index] || null },
      get total() { return this.steps.length },
      get isLast() { return this.index >= this.steps.length - 1 },
      get centered() { return !this.spot },
      get title() { return this.step ? _t(`tour_${this.tourId}_${this.step.id}_title`) : '' },
      get body() { return this.step ? _t(`tour_${this.tourId}_${this.step.id}_body`) : '' },
      get icon() { return this.step ? iconSvg(this.step.icon) : '' },
      get progress() { return this.total ? ((this.index + 1) / this.total) * 100 : 0 },
      get tourLabel() { return this.tourId === 'welcome' ? _t('tour_label_welcome') : _t(`tour_label_${this.tourId}`) },

      async boot() {
        try {
          const state = await api('GET', '/api/tour/')
          this.seen = state.seen
          this.auto = state.auto
        } catch (_) {
          return
        }
        this.ready = true
        if (!this.auto) return
        const queue = []
        if (!this.seen.includes('welcome')) queue.push('welcome')
        const page = this.pageTour
        if (page && !this.seen.includes(page)) queue.push(page)
        if (!queue.length) return
        // Deja que la página pinte su contenido (listas cargadas con fetch) antes.
        await sleep(900)
        if (this.active || this._modalOpen()) return
        this._queue = queue.slice(1)
        this.start(queue[0])
      },

      _modalOpen() {
        return !!document.querySelector('[aria-modal="true"]:not([data-tour-dialog])')
          && [...document.querySelectorAll('[aria-modal="true"]:not([data-tour-dialog])')]
            .some((d) => d.getBoundingClientRect().width > 0)
      },

      async start(tourId) {
        const def = window.TOURS?.[tourId]
        if (!def) return
        this.tourId = tourId
        this.steps = def.steps
        this.index = 0
        this.spot = null
        this.placed = false
        this.active = true
        this._listen(true)
        await this._show(0, 1)
      },

      // Repite la guía de la pantalla actual (o la de bienvenida si no tiene).
      replayPage() {
        this._queue = []
        this.start(this.pageTour || 'welcome')
      },

      // Vuelve a empezar: olvida todo lo visto y arranca la bienvenida.
      async restartAll() {
        try {
          const state = await api('DELETE', '/api/tour/')
          this.seen = state.seen
          this.auto = state.auto
        } catch (_) {}
        const page = this.pageTour
        this._queue = page ? [page] : []
        this.start('welcome')
      },

      async setAuto(auto) {
        try {
          const state = await api('PUT', '/api/tour/auto', { auto })
          this.seen = state.seen
          this.auto = state.auto
        } catch (e) {
          window.toast?.(_t('common_error'), e.message, 'error')
        }
      },

      async _show(index, direction) {
        if (this.busy) return
        this.busy = true
        try {
          let i = index
          while (i >= 0 && i < this.steps.length) {
            const step = this.steps[i]
            if (!step.target) {
              this._target = null
              this.index = i
              this.spot = null
              await this._layout()
              return
            }
            const el = await waitFor(step.target, i === index ? WAIT_MS : 300)
            if (el) {
              this._target = el
              this.index = i
              el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' })
              await sleep(reducedMotion() ? 0 : 320)
              await this._layout()
              return
            }
            i += direction
          }
          // No queda ningún paso visible en esa dirección.
          if (direction > 0) this.finish()
        } finally {
          this.busy = false
        }
      },

      async _layout() {
        await new Promise((r) => requestAnimationFrame(r))
        const card = document.getElementById('tour-card')
        const size = card ? { w: card.offsetWidth, h: card.offsetHeight } : { w: 360, h: 220 }
        if (!this._target || !this._target.isConnected) {
          this.spot = null
          this.pos = {
            x: Math.round((window.innerWidth - size.w) / 2),
            y: Math.round((window.innerHeight - size.h) / 2),
            side: 'center',
            arrow: 0,
          }
          await this._reveal()
          return
        }
        const r = visibleRect(this._target)
        const rect = {
          left: Math.max(4, r.left - PAD),
          top: Math.max(4, r.top - PAD),
          right: Math.min(window.innerWidth - 4, r.right + PAD),
          bottom: Math.min(window.innerHeight - 4, r.bottom + PAD),
        }
        rect.width = rect.right - rect.left
        rect.height = rect.bottom - rect.top
        this.spot = { x: rect.left, y: rect.top, w: rect.width, h: rect.height }
        this.pos = place(rect, size, this.step?.placement)
        await this._reveal()
      },

      // Primera colocación: se aplica la posición sin transición y se muestra en
      // el fotograma siguiente, para que no se deslice desde la posición anterior.
      async _reveal() {
        if (this.placed) return
        await new Promise((r) => requestAnimationFrame(r))
        this.placed = true
      },

      _onViewport() {
        if (!this.active) return
        cancelAnimationFrame(this._raf)
        this._raf = requestAnimationFrame(() => this._layout())
      },

      _listen(on) {
        if (!this._handler) this._handler = () => this._onViewport()
        const method = on ? 'addEventListener' : 'removeEventListener'
        window[method]('resize', this._handler)
        window[method]('scroll', this._handler, true)
      },

      next() {
        if (this.busy) return
        if (this.isLast) return this.finish()
        this._show(this.index + 1, 1)
      },

      prev() {
        if (this.busy || this.index === 0) return
        this._show(this.index - 1, -1)
      },

      onKey(ev) {
        if (!this.active) return
        if (ev.key === 'ArrowRight') { ev.preventDefault(); this.next() }
        else if (ev.key === 'ArrowLeft') { ev.preventDefault(); this.prev() }
      },

      // Terminar o saltar cuentan igual: esa guía no vuelve a salir sola.
      async finish() {
        const id = this.tourId
        this._close()
        if (id) {
          if (!this.seen.includes(id)) this.seen = [...this.seen, id]
          try { await api('POST', '/api/tour/seen', { tour: id }) } catch (_) {}
        }
        const nextTour = this._queue.shift()
        if (nextTour && !this.seen.includes(nextTour)) {
          await sleep(250)
          this.start(nextTour)
        }
      },

      skip() {
        this._queue = []
        this.finish()
      },

      // "No volver a mostrar": se apagan las guías automáticas.
      async dismissAll() {
        this._queue = []
        await this.finish()
        await this.setAuto(false)
        window.toast?.(_t('tour_disabled_title'), _t('tour_disabled_msg'), 'info')
      },

      _close() {
        this.active = false
        this._listen(false)
        this._target = null
        this.spot = null
        this.placed = false
      },
    })
  })

  document.addEventListener('alpine:initialized', () => {
    Alpine.store('tour').boot()
  })

  window.tour = {
    replay: () => Alpine.store('tour').replayPage(),
    restart: () => Alpine.store('tour').restartAll(),
  }
})()
