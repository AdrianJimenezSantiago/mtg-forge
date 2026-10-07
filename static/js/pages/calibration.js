function calibrationWizard() {
  return {
    step: 1,

    pageSize: 'a4',
    flipEdge: 'long',
    measuredX: 0,
    measuredY: 0,

    result: null,
    loading: false,
    error: '',
    copied: false,

    get sheetUrl() {
      const params = new URLSearchParams({
        page_size: this.pageSize,
        flip_edge: this.flipEdge,
      })
      return `/api/calibration/sheet?${params}`
    },

    goTo(step) {
      this.step = step
      this.$nextTick(() => window.icons?.())
    },

    async calculate() {
      this.loading = true
      this.error = ''
      try {
        this.result = await window.apiRequest('POST', '/api/calibration/derive', {
          body: {
            measured_x_mm: Number(this.measuredX),
            measured_y_mm: Number(this.measuredY),
            flip_edge: this.flipEdge,
          },
        })
        this.goTo(3)
      } catch (e) {
        this.error = e.detail || e.message || 'No se pudo calcular la corrección'
      } finally {
        this.loading = false
      }
    },

    restart() {
      this.step = 1
      this.result = null
      this.measuredX = 0
      this.measuredY = 0
      this.error = ''
      this.copied = false
      this.$nextTick(() => window.icons?.())
    },

    async copyValues() {
      if (!this.result) return
      const text =
        `X: ${this.result.back_offset_x_mm} mm, ` +
        `Y: ${this.result.back_offset_y_mm} mm`
      try {
        await navigator.clipboard.writeText(text)
        this.copied = true
        setTimeout(() => { this.copied = false }, 2000)
      } catch {
        this.copied = false
      }
    },

    fmt(value) {
      if (value === null || value === undefined) return '—'
      const sign = value > 0 ? '+' : ''
      return `${sign}${Number(value).toFixed(2)} mm`
    },

    get warningKey() {
      return this.result?.warning || ''
    },
  }
}

window.calibrationWizard = calibrationWizard
