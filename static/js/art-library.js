function artLibrary() {
  return {
    items: [],
    facets: null,
    overview: null,
    total: 0,
    hasMore: false,

    query: '',
    sources: [],
    variants: [],
    expansion: '',
    cardType: '',
    sort: 'name',

    offset: 0,
    limit: 60,

    loading: true,
    loadingMore: false,
    error: '',
    detail: null,
    _abort: null,

    async init() {
      this.readFiltersFromUrl()
      try {
        this.overview = await window.apiRequest('GET', '/api/library/overview')
      } catch (e) {
        this.error = e.detail || e.message
      }
      await this.reload()
    },

    readFiltersFromUrl() {
      const params = new URLSearchParams(window.location.search)
      this.query = params.get('q') || ''
      this.expansion = params.get('expansion') || ''
      this.cardType = params.get('card_type') || ''
      this.sort = params.get('sort') || 'name'
      this.variants = (params.get('variants') || '').split(',').filter(Boolean)
      this.sources = (params.get('sources') || '')
        .split(',').filter(Boolean).map(Number)
    },

    writeFiltersToUrl() {
      const params = new URLSearchParams()
      if (this.query) params.set('q', this.query)
      if (this.expansion) params.set('expansion', this.expansion)
      if (this.cardType) params.set('card_type', this.cardType)
      if (this.sort !== 'name') params.set('sort', this.sort)
      if (this.variants.length) params.set('variants', this.variants.join(','))
      if (this.sources.length) params.set('sources', this.sources.join(','))
      const qs = params.toString()
      window.history.replaceState(
        {}, '', qs ? `${window.location.pathname}?${qs}` : window.location.pathname
      )
    },

    get filterParams() {
      return {
        q: this.query,
        sources: this.sources,
        variants: this.variants,
        expansion: this.expansion,
        card_type: this.cardType,
      }
    },

    async reload() {
      this._abort?.abort()
      this._abort = new AbortController()
      const signal = this._abort.signal

      this.loading = true
      this.error = ''
      this.offset = 0
      this.writeFiltersToUrl()

      try {
        const [page, facets] = await Promise.all([
          window.apiRequest('GET', '/api/library/browse', {
            params: { ...this.filterParams, offset: 0, limit: this.limit, sort: this.sort },
            signal,
          }),
          window.apiRequest('GET', '/api/library/facets', {
            params: this.filterParams, signal,
          }),
        ])
        this.items = page.items
        this.total = page.total
        this.hasMore = page.has_more
        this.facets = facets
      } catch (e) {
        if (e.name !== 'AbortError') {
          this.error = e.detail || e.message || 'No se pudo cargar la biblioteca'
        }
      } finally {
        this.loading = false
        this.$nextTick(() => window.icons?.())
      }
    },

    async loadMore() {
      if (this.loadingMore || !this.hasMore) return
      this.loadingMore = true
      try {
        const next = this.offset + this.limit
        const page = await window.apiRequest('GET', '/api/library/browse', {
          params: {
            ...this.filterParams, offset: next, limit: this.limit, sort: this.sort,
          },
        })
        this.items.push(...page.items)
        this.offset = next
        this.hasMore = page.has_more
      } catch (e) {
        this.error = e.detail || e.message
      } finally {
        this.loadingMore = false
        this.$nextTick(() => window.icons?.())
      }
    },

    onSearchInput() {
      clearTimeout(this._searchTimer)
      this._searchTimer = setTimeout(() => this.reload(), 300)
    },

    toggleVariant(name) {
      const at = this.variants.indexOf(name)
      if (at >= 0) this.variants.splice(at, 1)
      else this.variants.push(name)
      this.reload()
    },

    toggleSource(id) {
      const at = this.sources.indexOf(id)
      if (at >= 0) this.sources.splice(at, 1)
      else this.sources.push(id)
      this.reload()
    },

    setExpansion(code) {
      this.expansion = this.expansion === code ? '' : code
      this.reload()
    },

    clearFilters() {
      this.query = ''
      this.sources = []
      this.variants = []
      this.expansion = ''
      this.cardType = ''
      this.reload()
    },

    get hasFilters() {
      return Boolean(
        this.query || this.expansion || this.cardType ||
        this.variants.length || this.sources.length
      )
    },

    isVariantActive(name) { return this.variants.includes(name) },
    isSourceActive(id) { return this.sources.includes(id) },

    open(item) {
      this.detail = item
      this.$nextTick(() => window.icons?.())
    },

    close() { this.detail = null },

    fileSize(bytes) {
      if (!bytes) return '—'
      const mb = bytes / (1024 * 1024)
      return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
    },

    compact(value) {
      if (value >= 1000000) return (value / 1000000).toFixed(1) + 'M'
      if (value >= 1000) return (value / 1000).toFixed(1) + 'k'
      return String(value ?? 0)
    },

    variantLabel(name) {
      return (window._t && window._t('library_variant_' + name)) || name
    },
  }
}

window.artLibrary = artLibrary
