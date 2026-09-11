/**
 * Planificador de tiradas — /print-planner
 *
 * Convierte `services/print_planner.py` y `services/print_needs.py` en una
 * vista utilizable. Responde a la pregunta que se hace todo el que imprime
 * proxies: "junto estos mazos, ¿en cuántos pedidos salen, cuánto cuestan de
 * verdad por carta, y con qué relleno los huecos que ya estoy pagando?".
 *
 * Publicado en `window` al final: Alpine resuelve `x-data` en el ámbito global.
 * Ver la nota sobre orden de scripts en base.html.
 */

function printPlanner() {
  return {
    // --- datos ---
    decks: [],
    selected: [],          // ids en el orden en que el usuario los marcó
    plan: null,
    tiers: [],
    maxTier: null,         // null = techo máximo de MPC

    // --- opciones ---
    keepTogether: true,
    subtractCollection: false,
    matchMode: 'oracle',
    includeBasics: true,

    // --- estado ---
    loading: true,
    planning: false,
    error: '',
    expandedRun: null,

    async init() {
      try {
        const [deckList, tierInfo] = await Promise.all([
          window.api.decks.list(),
          window.api.request
            ? window.apiRequest('GET', '/api/planner/tiers')
            : fetch('/api/planner/tiers').then((r) => r.json()),
        ])
        this.decks = Array.isArray(deckList) ? deckList : (deckList.decks || [])
        this.tiers = tierInfo.tiers || []
      } catch (e) {
        this.error = e.detail || e.message || 'No se pudieron cargar los mazos'
      } finally {
        this.loading = false
        this.$nextTick(() => window.icons?.())
      }
    },

    // --- selección ---

    isSelected(id) {
      return this.selected.includes(id)
    },

    toggle(id) {
      const at = this.selected.indexOf(id)
      if (at >= 0) this.selected.splice(at, 1)
      else this.selected.push(id)
      // Sin mazos no hay nada que planificar: se limpia para no dejar en
      // pantalla un plan que ya no corresponde a la selección.
      if (this.selected.length === 0) this.plan = null
      else this.replan()
    },

    selectAll() {
      this.selected = this.decks.map((d) => d.id)
      this.replan()
    },

    clearSelection() {
      this.selected = []
      this.plan = null
    },

    get selectedCardCount() {
      return this.decks
        .filter((d) => this.selected.includes(d.id))
        .reduce((sum, d) => sum + (d.card_count || 0), 0)
    },

    // --- planificación ---

    /**
     * Recalcula el plan.
     *
     * Se debouncea porque cada clic en un mazo dispara un replan y el usuario
     * suele marcar varios seguidos. Sin esto se lanzarían cuatro peticiones
     * para un resultado que solo importa al final.
     */
    replan() {
      clearTimeout(this._replanTimer)
      this._replanTimer = setTimeout(() => this.buildPlan(), 200)
    },

    async buildPlan() {
      if (this.selected.length === 0) {
        this.plan = null
        return
      }
      this.planning = true
      this.error = ''
      try {
        this.plan = await window.apiRequest('POST', '/api/planner/plan', {
          body: {
            deck_ids: this.selected,
            max_tier: this.maxTier,
            keep_decks_together: this.keepTogether,
            subtract_collection: this.subtractCollection,
            match_mode: this.matchMode,
            include_basics: this.includeBasics,
          },
        })
      } catch (e) {
        this.error = e.detail || e.message || 'No se pudo calcular el plan'
        this.plan = null
      } finally {
        this.planning = false
        this.$nextTick(() => window.icons?.())
      }
    },

    // --- formato ---

    usd(value) {
      if (value === null || value === undefined) return '—'
      return '$' + Number(value).toFixed(2)
    },

    unit(value) {
      if (!value) return '—'
      return '$' + Number(value).toFixed(3)
    },

    percent(value) {
      return (value ?? 0).toFixed(0) + '%'
    },

    /**
     * Color del indicador de llenado.
     *
     * Por debajo del 70% se está pagando una parte importante del pedido por
     * huecos vacíos, y merece la pena avisar. Por encima del 95% está bien
     * aprovechado.
     */
    fillClass(percent) {
      if (percent >= 95) return 'text-success'
      if (percent >= 70) return 'text-warning'
      return 'text-danger'
    },

    barClass(percent) {
      if (percent >= 95) return 'bg-success'
      if (percent >= 70) return 'bg-warning'
      return 'bg-danger'
    },

    get cheapestAlternative() {
      return (this.plan?.alternatives || []).find((o) => o.is_cheapest) || null
    },

    /**
     * ¿La opción más barata difiere del plan actual?
     *
     * Es el consejo con más valor de la vista: repartir en dos pedidos
     * medianos puede salir más barato por carta que uno grande medio vacío,
     * y es contraintuitivo.
     */
    get savingsAvailable() {
      const best = this.cheapestAlternative
      if (!best || !this.plan) return null
      const current = this.plan.totals.subtotal_usd
      const saving = current - best.subtotal_usd
      if (saving <= 0.01) return null
      return { saving, option: best }
    },

    applyCheapest() {
      const best = this.cheapestAlternative
      if (!best) return
      this.maxTier = best.ceiling
      this.buildPlan()
    },

    toggleRun(index) {
      this.expandedRun = this.expandedRun === index ? null : index
    },
  }
}

// --- Puente con Alpine ---------------------------------------------------
// Alpine resuelve las expresiones de `x-data` contra el ámbito global.
window.printPlanner = printPlanner
