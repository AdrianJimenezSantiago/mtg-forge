(function () {
  'use strict'

  // Debe coincidir con mpc_forge/services/art/custom_art.py: el servidor vuelve a
  // validar todo, esto solo adelanta el diagnóstico para enseñarlo en la vista previa.
  const LIMITS = {
    maxBytes: 30 * 1024 * 1024,
    maxPixels: 60_000_000,
    minSide: 200,
    ratioMin: 0.70,
    ratioMax: 0.745,
    cardRatio: 63 / 88,
    cardWidthIn: 63 / 25.4,
    lowDpi: 240,
    poorDpi: 150,
    recommendedDpi: 300,
  }
  const TYPES = { 'image/png': 'PNG', 'image/jpeg': 'JPG', 'image/webp': 'WEBP' }
  const EXTENSIONS = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp' }

  const _t = (key, vars) => {
    let text = window._t ? window._t(key) : key
    for (const [k, v] of Object.entries(vars || {})) text = text.replaceAll(`{${k}}`, v)
    return text
  }

  function fileType(file) {
    if (TYPES[file.type]) return file.type
    const ext = (file.name || '').split('.').pop().toLowerCase()
    return EXTENSIONS[ext] || file.type || ''
  }

  function suggestVariant(file) {
    const stem = (file.name || '').replace(/\.[^.]+$/, '')
    if (!stem || /^(image|imagen|img|pasted|screenshot|captura|clipboard)\b/i.test(stem)) return ''
    return stem.replace(/[_]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 60)
  }

  function decode(url) {
    return new Promise((resolve, reject) => {
      const img = new Image()
      img.onload = () => resolve(img)
      img.onerror = () => reject(new Error('decode'))
      img.src = url
    })
  }

  function fileFromDataTransfer(dt) {
    if (!dt) return null
    for (const item of dt.items || []) {
      if (item.kind === 'file') {
        const f = item.getAsFile()
        if (f) return f
      }
    }
    return (dt.files && dt.files[0]) || null
  }

  function dataTransferHasFile(dt) {
    return !!dt && Array.from(dt.types || []).includes('Files')
  }

  async function errorDetail(res) {
    try {
      const body = await res.json()
      const d = body && body.detail
      if (d && typeof d === 'object' && !Array.isArray(d)) {
        const key = `art_upload_err_${d.code}`
        const text = window._T && window._T[key]
        return text || d.message || `HTTP ${res.status}`
      }
      if (typeof d === 'string') return d
      if (Array.isArray(d)) return d.map(x => x.msg).join('; ')
    } catch (_) {}
    return res.statusText || `HTTP ${res.status}`
  }

  const blank = () => ({
    open: false,
    title: '',
    displayName: '',
    cardName: '',
    face: 'front',
    faces: [],
    currentImages: {},
    deckId: null,
    deckCardId: null,
    file: null,
    previewUrl: null,
    info: null,
    checks: [],
    blocking: false,
    fit: 'stretch',
    variant: '',
    reading: false,
    uploading: false,
    error: '',
    dragOver: false,
    showGuides: true,
    _baseChecks: [],
    _suggested: '',
  })

  document.addEventListener('alpine:init', () => {
    Alpine.store('artUpload', {
      ...blank(),
      limits: LIMITS,
      _resolve: null,
      _readSeq: 0,

      /**
       * Abre el diálogo de subida para una carta concreta.
       * opts: { cardName, displayName, face, faces: [{value, label}], currentImage,
       *         currentImages: {front, back}, deckId, deckCardId, file, title }
       * Si se pasan deckId y deckCardId, el arte queda asignado a esa carta.
       * Resuelve con { art, card } o con null si el usuario cancela.
       */
      start(opts = {}) {
        if (this._resolve) this._resolve(null)
        this._revoke()
        Object.assign(this, blank(), {
          open: true,
          title: opts.title || '',
          displayName: opts.displayName || '',
          cardName: opts.cardName || '',
          face: opts.face || 'front',
          faces: Array.isArray(opts.faces) ? opts.faces : [],
          currentImages: opts.currentImages || { [opts.face || 'front']: opts.currentImage || null },
          deckId: opts.deckId ?? null,
          deckCardId: opts.deckCardId ?? null,
        })
        const promise = new Promise((resolve) => { this._resolve = resolve })
        if (opts.file) this.pick(opts.file)
        requestAnimationFrame(() => window.icons?.())
        return promise
      },

      close(result = null) {
        if (this.uploading) return
        const resolve = this._resolve
        this._resolve = null
        this._revoke()
        Object.assign(this, blank())
        if (resolve) resolve(result)
      },

      _revoke() {
        if (this.previewUrl) URL.revokeObjectURL(this.previewUrl)
        this.previewUrl = null
      },

      clearFile() {
        this._revoke()
        Object.assign(this, {
          file: null, info: null, checks: [], _baseChecks: [], blocking: false, error: '', fit: 'stretch',
        })
      },

      async pick(file) {
        if (!file) return
        const seq = ++this._readSeq
        this.clearFile()
        this.file = file
        this.reading = true
        const checks = []
        const type = fileType(file)
        const info = {
          name: file.name || _t('art_upload_pasted'),
          bytes: file.size,
          type: TYPES[type] || (type.split('/')[1] || '?').toUpperCase(),
          width: 0, height: 0, ratio: 0,
        }
        if (!TYPES[type]) {
          checks.push({ level: 'error', text: _t('art_upload_err_unsupported_format') })
        }
        if (file.size === 0) {
          checks.push({ level: 'error', text: _t('art_upload_err_empty') })
        } else if (file.size > LIMITS.maxBytes) {
          checks.push({ level: 'error', text: _t('art_upload_err_too_large') })
        }

        if (!checks.some(c => c.level === 'error')) {
          const url = URL.createObjectURL(file)
          try {
            const img = await decode(url)
            if (seq !== this._readSeq) { URL.revokeObjectURL(url); return }
            this.previewUrl = url
            info.width = img.naturalWidth
            info.height = img.naturalHeight
            info.ratio = info.height ? info.width / info.height : 0
          } catch (_) {
            URL.revokeObjectURL(url)
            if (seq !== this._readSeq) return
            checks.push({ level: 'error', text: _t('art_upload_err_not_an_image') })
          }
        }

        if (info.width) {
          if (Math.min(info.width, info.height) < LIMITS.minSide) {
            checks.push({ level: 'error', text: _t('art_upload_err_too_small') })
          } else if (info.width * info.height > LIMITS.maxPixels) {
            checks.push({ level: 'error', text: _t('art_upload_err_too_many_pixels') })
          }
        }

        if (seq !== this._readSeq) return
        this.info = info
        this.blocking = checks.some(c => c.level === 'error')
        this._baseChecks = checks
        if (!this.blocking && !this.ratioOk) this.fit = 'crop'
        // Solo se reemplaza la sugerencia automática, nunca lo que escribió el usuario.
        if (!this.variant || this.variant === this._suggested) {
          this._suggested = suggestVariant(file)
          this.variant = this._suggested
        }
        this.reading = false
        this._refreshChecks()
        requestAnimationFrame(() => window.icons?.())
      },

      get currentImage() {
        return (this.currentImages && this.currentImages[this.face]) || null
      },

      get ratioOk() {
        const r = this.info && this.info.ratio
        return !!r && r >= LIMITS.ratioMin && r <= LIMITS.ratioMax
      },

      // Tamaño final tras aplicar el ajuste elegido (igual que el servidor).
      get finalSize() {
        if (!this.info || !this.info.width) return null
        const { width: w, height: h } = this.info
        if (this.ratioOk || this.fit === 'stretch') return { w, h }
        const wide = w / h > LIMITS.cardRatio
        if (this.fit === 'crop') {
          return wide ? { w: Math.round(h * LIMITS.cardRatio), h } : { w, h: Math.round(w / LIMITS.cardRatio) }
        }
        return wide ? { w, h: Math.round(w / LIMITS.cardRatio) } : { w: Math.round(h * LIMITS.cardRatio), h }
      },

      get dpi() {
        const s = this.finalSize
        return s ? Math.round(s.w / LIMITS.cardWidthIn) : 0
      },

      get objectFit() {
        if (this.ratioOk) return 'fill'
        return { stretch: 'fill', crop: 'cover', contain: 'contain' }[this.fit] || 'fill'
      },

      setFit(fit) {
        this.fit = fit
        this._refreshChecks()
      },

      _refreshChecks() {
        const checks = [...(this._baseChecks || [])]
        if (!this.blocking && this.info && this.info.width) {
          if (this.ratioOk) {
            checks.push({ level: 'ok', text: _t('art_upload_check_ratio_ok') })
          } else if (this.fit === 'stretch') {
            checks.push({ level: 'warn', text: _t('art_upload_check_ratio_stretch', { ratio: this.info.ratio.toFixed(2) }) })
          } else {
            checks.push({ level: 'ok', text: _t(this.fit === 'crop' ? 'art_upload_check_ratio_crop' : 'art_upload_check_ratio_contain') })
          }
          const dpi = this.dpi
          if (dpi < LIMITS.poorDpi) {
            checks.push({ level: 'warn', text: _t('art_upload_check_dpi_poor', { dpi }) })
          } else if (dpi < LIMITS.lowDpi) {
            checks.push({ level: 'warn', text: _t('art_upload_check_dpi_low', { dpi }) })
          } else {
            checks.push({ level: 'ok', text: _t('art_upload_check_dpi_ok', { dpi }) })
          }
          if (this.file && this.file.size > 0) {
            checks.push({ level: 'ok', text: _t('art_upload_check_format', { type: this.info.type }) })
          }
        }
        this.checks = checks
      },

      get canUpload() {
        return !!this.file && !!this.info && !this.blocking && !this.reading && !this.uploading
          && !!this.cardName
      },

      onDrop(ev) {
        this.dragOver = false
        const file = fileFromDataTransfer(ev.dataTransfer)
        if (file) this.pick(file)
      },

      onPaste(ev) {
        if (!this.open || this.uploading) return
        const file = fileFromDataTransfer(ev.clipboardData)
        if (file) {
          ev.preventDefault()
          this.pick(file)
        }
      },

      async upload() {
        if (!this.canUpload) return
        this.uploading = true
        this.error = ''
        try {
          const form = new FormData()
          form.append('file', this.file, this.file.name || 'pasted.png')
          form.append('card_name', this.cardName)
          form.append('face', this.face)
          form.append('fit', this.ratioOk ? 'stretch' : this.fit)
          const variant = (this.variant || '').trim()
          if (variant) form.append('variant', variant)

          const res = await fetch('/api/custom-art/upload', { method: 'POST', body: form })
          if (!res.ok) throw new Error(await errorDetail(res))
          const art = await res.json()

          let card = null
          if (this.deckId != null && this.deckCardId != null) {
            const r = await fetch(`/api/decks/${this.deckId}/cards/change-art`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ deck_card_id: this.deckCardId, custom_art_id: art.id, face: this.face }),
            })
            if (!r.ok) throw new Error(await errorDetail(r))
            card = await r.json()
          }

          if (art.duplicate) {
            window.toast?.(_t('art_upload_duplicate_title'), _t('art_upload_duplicate_msg'), 'info')
          } else {
            window.toast?.(_t('art_upload_done'), art.filename, 'success')
          }
          this.uploading = false
          this.close({ art, card })
        } catch (e) {
          this.error = e.message || String(e)
          this.uploading = false
        }
      },
    })
  })

  window.addEventListener('paste', (ev) => {
    try { Alpine.store('artUpload').onPaste(ev) } catch (_) {}
  })

  window.artUpload = {
    open: (opts) => Alpine.store('artUpload').start(opts),
    fileFrom: fileFromDataTransfer,
    hasFile: dataTransferHasFile,
  }
})()
