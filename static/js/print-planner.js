function printPlanner() {
  return {
    decks: [],
    selected: [],
    plan: null,
    tiers: [],
    maxTier: null,

    keepTogether: true,
    subtractCollection: false,
    matchMode: 'oracle',
    includeBasics: true,

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

    isSelected(id) {
      return this.selected.includes(id)
    },

    toggle(id) {
      const at = this.selected.indexOf(id)
      if (at >= 0) this.selected.splice(at, 1)
      else this.selected.push(id)
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

window.printPlanner = printPlanner
