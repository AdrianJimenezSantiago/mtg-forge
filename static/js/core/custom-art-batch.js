(function () {
  'use strict'

  const MAX_FILES = 2000
  const UPLOAD_CONCURRENCY = 3
  const IMAGE_EXT = /\.(png|jpe?g|webp)$/i
  const ZIP_EXT = /\.zip$/i

  // Fuera del store a propósito: crear una URL al pintar una miniatura no debe
  // disparar la reactividad de Alpine.
  const previewUrls = new Map()

  const _t = (key, vars) => {
    let text = window._t ? window._t(key) : key
    for (const [k, v] of Object.entries(vars || {})) text = text.replaceAll(`{${k}}`, v)
    return text
  }

  async function detail(res) {
    if (window.artUpload?.errorDetail) return window.artUpload.errorDetail(res)
    return res.statusText || `HTTP ${res.status}`
  }

  async function postJson(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    })
    if (!res.ok) throw new Error(await detail(res))
    return res.json()
  }

  // Los items de un DataTransfer dejan de ser válidos tras el primer await, así
  // que las entradas se recogen antes de recorrer carpetas.
  async function filesFromDrop(dt) {
    const entries = Array.from(dt?.items || [])
      .filter((i) => i.kind === 'file')
      .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
      .filter(Boolean)
    if (!entries.length) return Array.from(dt?.files || []).map((file) => ({ file, path: file.name }))

    const out = []
    const read = (reader) => new Promise((resolve, reject) => reader.readEntries(resolve, reject))
    async function walk(entry, prefix) {
      if (out.length > MAX_FILES || entry.name.startsWith('.')) return
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject))
        out.push({ file, path: prefix + file.name })
        return
      }
      if (!entry.isDirectory) return
      const reader = entry.createReader()
      for (;;) {
        const batch = await read(reader)
        if (!batch.length) break
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`)
      }
    }
    for (const entry of entries) await walk(entry, '')
    return out
  }

  function filesFromInput(list) {
    return Array.from(list || []).map((file) => ({ file, path: file.webkitRelativePath || file.name }))
  }

  async function pool(items, limit, worker) {
    let next = 0
    const run = async () => {
      while (next < items.length) {
        const i = next++
        await worker(items[i], i)
      }
    }
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, run))
  }

  const blank = () => ({
    open: false,
    deckId: null,
    step: 'source',
    source: null,
    plan: null,
    rows: [],
    missing: [],
    unused: [],
    deckCards: [],
    tab: 'assign',
    overwrite: false,
    fit: 'crop',
    ignored: 0,
    truncated: false,
    progress: { done: 0, total: 0, label: '' },
    result: null,
    error: '',
    dragOver: false,
    linkPath: '',
    linking: false,
    folderError: '',
  })

  document.addEventListener('alpine:init', () => {
    Alpine.store('artBatch', {
      ...blank(),
      folders: [],
      foldersLoaded: false,
      appFolder: '',

      start(opts = {}) {
        this._revoke()
        Object.assign(this, blank(), { open: true, deckId: opts.deckId ?? null })
        this.loadFolders()
        this._loadAppFolder()
        requestAnimationFrame(() => window.icons?.())
      },

      // El estado se limpia en el siguiente tick: si se borrase ya, Alpine aún
      // evaluaría las plantillas que se están desmontando con `plan` a null y
      // las miniaturas perderían su blob mientras se pintan.
      close() {
        if (this.step === 'working') return
        this.open = false
        setTimeout(() => {
          if (this.open) return
          this._revoke()
          Object.assign(this, blank())
        })
      },

      back() {
        if (this.step === 'working') return
        const deckId = this.deckId
        this.step = 'source'
        setTimeout(() => {
          if (!this.open || this.step !== 'source') return
          this._revoke()
          Object.assign(this, blank(), { open: true, deckId })
          requestAnimationFrame(() => window.icons?.())
        })
      },

      _revoke() {
        for (const url of previewUrls.values()) URL.revokeObjectURL(url)
        previewUrls.clear()
      },

      async _loadAppFolder() {
        if (this.appFolder) return
        try {
          const res = await fetch('/api/settings/paths')
          if (res.ok) this.appFolder = (await res.json()).custom_art_dir || ''
        } catch (_) {}
      },

      // --- Origen: ficheros sueltos, carpetas y ZIP desde el navegador -------

      async onDrop(ev) {
        this.dragOver = false
        if (this.step !== 'source') return
        this.handleFiles(await filesFromDrop(ev.dataTransfer))
      },

      onInput(ev) {
        const picked = filesFromInput(ev.target.files)
        ev.target.value = ''
        this.handleFiles(picked)
      },

      async handleFiles(picked) {
        const visible = picked.filter((p) => !p.file.name.startsWith('.'))
        const zips = visible.filter((p) => ZIP_EXT.test(p.file.name))
        const images = visible.filter((p) => IMAGE_EXT.test(p.file.name))
        this.ignored = visible.length - zips.length - images.length
        if (zips.length) {
          if (images.length) window.toast?.(_t('batch_zip_only_title'), _t('batch_zip_only_msg'), 'info')
          return this.importZips(zips.map((p) => p.file))
        }
        if (!images.length) {
          this.error = _t('batch_no_images')
          return
        }
        this.truncated = images.length > MAX_FILES
        const kept = images.slice(0, MAX_FILES)
        this.source = {
          kind: 'files',
          files: kept.map((p) => p.file),
          names: kept.map((p) => p.path),
          label: _t('batch_source_files_label', { n: kept.length }),
        }
        await this._plan()
      },

      async importZips(zips) {
        this.error = ''
        this.step = 'loading'
        const artIds = new Set()
        const skipped = []
        try {
          for (const [i, zip] of zips.entries()) {
            this.progress = { done: i, total: zips.length, label: _t('batch_importing_zip', { name: zip.name }) }
            const form = new FormData()
            form.append('file', zip, zip.name)
            form.append('fit', this.fit)
            const res = await fetch('/api/custom-art/import-zip', { method: 'POST', body: form })
            if (!res.ok) throw new Error(`${zip.name}: ${await detail(res)}`)
            const body = await res.json()
            body.art_ids.forEach((id) => artIds.add(id))
            skipped.push(...body.skipped)
          }
        } catch (e) {
          this.error = e.message || String(e)
          this.step = 'source'
          return
        }
        this.ignored += skipped.length
        this.source = {
          kind: 'library',
          artIds: [...artIds],
          label: zips.length === 1 ? zips[0].name : _t('batch_source_zips_label', { n: zips.length }),
        }
        await this._plan()
      },

      // --- Origen: biblioteca de la app y carpetas vinculadas ----------------

      useLibrary() {
        this.source = { kind: 'library', rescan: true, label: _t('batch_source_library_label') }
        return this._plan()
      },

      useFolder(folder, rescan = true) {
        this.source = { kind: 'library', rescan, folderId: folder.id, label: folder.label }
        return this._plan()
      },

      async loadFolders() {
        try {
          const res = await fetch('/api/custom-art/folders')
          if (res.ok) this.folders = await res.json()
        } catch (_) {}
        this.foldersLoaded = true
        requestAnimationFrame(() => window.icons?.())
      },

      async linkFolder() {
        const path = this.linkPath.trim()
        if (!path || this.linking) return null
        this.linking = true
        this.folderError = ''
        try {
          const folder = await postJson('/api/custom-art/folders', { path })
          this.linkPath = ''
          await this.loadFolders()
          window.toast?.(_t('batch_folder_linked'), _t('batch_folder_linked_msg', { name: folder.label, n: folder.file_count }), 'success')
          return folder
        } catch (e) {
          this.folderError = e.message || String(e)
          return null
        } finally {
          this.linking = false
        }
      },

      async linkAndUse() {
        const folder = await this.linkFolder()
        if (folder && this.deckId != null) await this.useFolder(folder, false)
      },

      async unlinkFolder(folder) {
        const message = folder.in_use
          ? _t('batch_unlink_in_use', { n: folder.in_use })
          : _t('batch_unlink_msg')
        const ok = await window.confirmDialog?.(_t('batch_unlink_title', { name: folder.label }), message, {
          danger: folder.in_use > 0, icon: 'unlink', confirmLabel: _t('batch_unlink'),
        })
        if (!ok) return
        const res = await fetch(`/api/custom-art/folders/${folder.id}`, { method: 'DELETE' })
        if (!res.ok) {
          window.toast?.(_t('common_error'), await detail(res), 'error')
          return
        }
        await this.loadFolders()
        if (folder.in_use) window.dispatchEvent(new CustomEvent('custom-art-applied'))
      },

      // --- Plan -------------------------------------------------------------

      async _plan() {
        if (this.deckId == null || !this.source) return
        this.error = ''
        this.step = 'loading'
        this.progress = { done: 0, total: 0, label: _t('batch_matching') }
        const src = this.source
        try {
          const plan = src.kind === 'files'
            ? await postJson(`/api/decks/${this.deckId}/custom-art/plan-files`, {
              filenames: src.names, overwrite: this.overwrite,
            })
            : await postJson(`/api/decks/${this.deckId}/custom-art/plan`, {
              overwrite: this.overwrite,
              rescan: !!src.rescan,
              art_ids: src.artIds ?? null,
              folder_id: src.folderId ?? null,
            })
          src.rescan = false
          this._setPlan(plan)
          this.step = 'review'
        } catch (e) {
          this.error = e.message || String(e)
          this.step = 'source'
        }
        requestAnimationFrame(() => window.icons?.())
      },

      setOverwrite(value) {
        this.overwrite = value
        return this._plan()
      },

      _setPlan(plan) {
        this.plan = plan
        const cards = new Map()
        this.rows = []
        this.missing = []
        for (const item of plan.items) {
          if (item.face === 'front') cards.set(item.deck_card_id, item.name)
          if (item.status === 'missing') {
            if (item.face === 'front') this.missing.push(item)
            continue
          }
          this.rows.push({
            key: `${item.deck_card_id}:${item.face}`,
            ...item,
            chosen: item.proposed ?? item.candidates[0].ref,
            enabled: item.status === 'match',
          })
        }
        this.deckCards = [...cards].map(([id, name]) => ({ id, name }))
          .sort((a, b) => a.name.localeCompare(b.name))
        this.unused = plan.unused.map((u) => ({ ...u, target: '' }))
        this.tab = this.rows.length ? 'assign' : (this.unused.length ? 'unused' : 'missing')
      },

      count(key, n) {
        const one = window._T?.[`${key}_one`]
        return (n === 1 && one ? one : _t(key)).replaceAll('{n}', n)
      },

      thumb(cand) {
        if (!cand) return null
        if (this.source?.kind !== 'files') return cand.thumb_url
        let url = previewUrls.get(cand.ref)
        if (!url) {
          const file = this.source.files[cand.ref]
          if (!file) return null
          url = URL.createObjectURL(file)
          previewUrls.set(cand.ref, url)
        }
        return url
      },

      chosenOf(row) {
        return row.candidates.find((c) => c.ref === row.chosen) || row.candidates[0]
      },

      cardName(id) {
        return this.deckCards.find((c) => c.id === id)?.name || ''
      },

      assignUnused(image, deckCardId) {
        const id = Number(deckCardId)
        if (!id) return
        const cand = {
          ref: image.ref, filename: image.filename, variant: image.variant, face: 'front', thumb_url: image.thumb_url,
        }
        let row = this.rows.find((r) => r.deck_card_id === id && r.face === 'front')
        if (!row) {
          const missing = this.missing.find((m) => m.deck_card_id === id)
          this.missing = this.missing.filter((m) => m.deck_card_id !== id)
          row = {
            key: `${id}:front`,
            deck_card_id: id,
            name: this.cardName(id),
            role: missing?.role || '',
            quantity: missing?.quantity || 1,
            face: 'front',
            status: 'match',
            current_custom_art_id: missing?.current_custom_art_id ?? null,
            proposed: image.ref,
            candidates: [],
          }
          this.rows.push(row)
        }
        if (!row.candidates.some((c) => c.ref === image.ref)) row.candidates.push(cand)
        row.chosen = image.ref
        row.enabled = true
        this.unused = this.unused.filter((u) => u.ref !== image.ref)
      },

      setAll(enabled) {
        for (const row of this.rows) if (row.status !== 'same') row.enabled = enabled
      },

      get pending() {
        return this.rows.filter((r) => r.enabled && !(r.status === 'same' && r.chosen === r.current_custom_art_id))
      },

      // --- Aplicar ----------------------------------------------------------

      async apply() {
        const rows = this.pending
        if (!rows.length || this.step !== 'review') return
        this.step = 'working'
        this.error = ''
        const failed = []
        let assignments
        try {
          assignments = this.source.kind === 'files'
            ? await this._uploadAll(rows, failed)
            : rows.map((r) => ({ deck_card_id: r.deck_card_id, face: r.face, custom_art_id: r.chosen }))
          this.progress = { done: 0, total: 0, label: _t('batch_saving') }
          const result = assignments.length
            ? await postJson(`/api/decks/${this.deckId}/custom-art/apply`, { assignments })
            : { changed: 0, unchanged: 0, invalid: 0, event_id: null }
          this.result = { ...result, failed }
          this.step = 'done'
          if (result.changed) {
            window.dispatchEvent(new CustomEvent('custom-art-applied', { detail: { deckId: this.deckId } }))
          }
        } catch (e) {
          this.error = e.message || String(e)
          this.step = 'review'
        }
        requestAnimationFrame(() => window.icons?.())
      },

      // Cada fichero se sube una vez por carta destino: el nombre guardado es el
      // de la carta, que es lo que lo vuelve a emparejar al re-escanear.
      async _uploadAll(rows, failed) {
        const jobs = new Map()
        for (const row of rows) {
          const cand = this.chosenOf(row)
          const key = `${row.chosen}|${row.deck_card_id}|${row.face}`
          if (!jobs.has(key)) jobs.set(key, { row, cand, artId: null })
        }
        const list = [...jobs.values()]
        let done = 0
        this.progress = { done, total: list.length, label: _t('batch_uploading') }
        await pool(list, UPLOAD_CONCURRENCY, async (job) => {
          const file = this.source.files[job.row.chosen]
          try {
            const form = new FormData()
            form.append('file', file, file.name)
            form.append('card_name', job.row.name)
            form.append('face', job.row.face)
            form.append('fit', this.fit)
            if (job.cand?.variant) form.append('variant', job.cand.variant.slice(0, 120))
            const res = await fetch('/api/custom-art/upload', { method: 'POST', body: form })
            if (!res.ok) throw new Error(await detail(res))
            job.artId = (await res.json()).id
          } catch (e) {
            failed.push({ name: file.name, card: job.row.name, error: e.message || String(e) })
          } finally {
            this.progress = { ...this.progress, done: ++done }
          }
        })
        return list.filter((j) => j.artId != null).map((j) => ({
          deck_card_id: j.row.deck_card_id, face: j.row.face, custom_art_id: j.artId,
        }))
      },

      async undo() {
        const eventId = this.result?.event_id
        if (!eventId || this.deckId == null) return
        const res = await fetch(`/api/decks/${this.deckId}/activity/${eventId}/undo`, { method: 'POST' })
        if (!res.ok) {
          window.toast?.(_t('common_error'), await detail(res), 'error')
          return
        }
        window.toast?.(_t('batch_undone'), (await res.json()).summary || '', 'success')
        window.dispatchEvent(new CustomEvent('custom-art-applied', { detail: { deckId: this.deckId } }))
        this.close()
      },
    })
  })

  window.artBatch = {
    open: (opts) => Alpine.store('artBatch').start(opts),
  }
})()
