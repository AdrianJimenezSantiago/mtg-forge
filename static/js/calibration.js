/**
 * Asistente de calibración de dúplex — /calibrate
 *
 * El PDF Studio tiene `back_offset_x_mm` y `back_offset_y_mm`, pero hasta
 * ahora el usuario tenía que adivinarlos. Esta vista los deriva de una
 * medición sobre una hoja impresa.
 *
 * El cálculo del signo lo hace el servidor a propósito: en dúplex por borde
 * largo el reverso queda espejado, así que la corrección en X va en el mismo
 * sentido que la deriva y no en el contrario. Es donde se equivoca todo el
 * mundo, y un signo invertido deja las cartas el DOBLE de descentradas.
 *
 * Publicado en `window` al final: Alpine resuelve `x-data` en el ámbito global.
 */

function calibrationWizard() {
  return {
    step: 1,               // 1 imprimir · 2 medir · 3 resultado

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

    /** Copia los dos valores para pegarlos en el PDF Studio. */
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
        // El portapapeles puede estar bloqueado según el contexto de
        // seguridad. No es motivo para mostrar un error: los números están
        // en pantalla y se pueden teclear.
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

// --- Puente con Alpine ---------------------------------------------------
window.calibrationWizard = calibrationWizard
