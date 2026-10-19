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

// Misma clave que usa PDF Studio: la escala es de la impresora, no del mazo.
const LS_PRINT_SCALE = 'pdfStudio.printScale'

function readPrintScale() {
  try {
    const scale = JSON.parse(localStorage.getItem(LS_PRINT_SCALE) || 'null')
    if (scale && typeof scale.x === 'number' && typeof scale.y === 'number') return scale
  } catch {}
  return { x: 100, y: 100 }
}

function scaleCalibration() {
  const current = readPrintScale()
  return {
    pageSize: 'a4',
    currentX: current.x,
    currentY: current.y,
    measuredX: null,
    measuredY: null,

    result: null,
    loading: false,
    error: '',
    saved: false,

    get currentActive() {
      return this.currentX !== 100 || this.currentY !== 100
    },

    // La hoja sale con la compensación vigente, para verificar o afinar.
    get sheetUrl() {
      const params = new URLSearchParams({
        page_size: this.pageSize,
        scale_x_pct: this.currentX,
        scale_y_pct: this.currentY,
      })
      return `/api/calibration/scale-sheet?${params}`
    },

    async calculate() {
      this.loading = true
      this.error = ''
      this.saved = false
      try {
        this.result = await window.apiRequest('POST', '/api/calibration/derive-scale', {
          body: {
            measured_x_mm: Number(this.measuredX),
            measured_y_mm: Number(this.measuredY),
            current_x_pct: this.currentX,
            current_y_pct: this.currentY,
          },
        })
        this.$nextTick(() => window.icons?.())
      } catch (e) {
        this.error = e.detail || e.message || 'No se pudo calcular la escala'
      } finally {
        this.loading = false
      }
    },

    save() {
      if (!this.result) return
      const scale = { x: this.result.print_scale_x_pct, y: this.result.print_scale_y_pct }
      try {
        localStorage.setItem(LS_PRINT_SCALE, JSON.stringify(scale))
      } catch {
        return
      }
      this.currentX = scale.x
      this.currentY = scale.y
      this.saved = true
      this.$nextTick(() => window.icons?.())
    },
  }
}

window.calibrationWizard = calibrationWizard
window.scaleCalibration = scaleCalibration
