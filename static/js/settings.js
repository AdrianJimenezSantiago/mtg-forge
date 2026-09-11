/**
 * Vista de Ajustes
 *
 * Extraído de `templates/settings.html`, donde vivía como un bloque `<script>`
 * de 822 líneas. La lógica es idéntica: solo ha cambiado de fichero.
 *
 * Las funciones que Alpine necesita resolver desde los atributos `x-data` del
 * HTML se publican en `window` al final del módulo. Es deliberado: Alpine
 * evalúa `x-data` como una expresión en el ámbito global, así que un `export`
 * por sí solo no basta.
 *
 * Regenerar con:  python scripts/extract_inline_js.py
 */
// ============================================================================
// SHELL DE LA VISTA DE AJUSTES
// ============================================================================
// Contiene el estado principal (settings, sección activa, búsqueda, modal drives)
// y helpers de render. Los sub-componentes (autofillStatus, artSourcesPanel,
// debugLogPanel) se registran aparte, reutilizando su lógica original.
function settingsShell() {
  return {
    // Estado principal
    loading: true,
    definitions: [],
    values: {},
    saving: false,
    backingUp: false,
    lastBackup: null,

    // --- Ubicación de datos (rutas personalizables) ---
    // paths: dict con rutas efectivas en uso, viene de GET /api/settings/paths.
    // pathOverrides: valores editados por el usuario en los inputs (posiblemente
    //   distintos de lo que hay en BD hasta que se guarda). Empieza con lo
    //   guardado en values (values['paths.art_dir'] etc).
    // pathsDirty: true si algún input cambió respecto a lo guardado.
    paths: {
      install_root: '', data_dir: '', db_path: '',
      art_dir: '', custom_art_dir: '', exports_dir: '',
      backups_dir: '', cardbacks_dir: '',
    },
    pathOverrides: {
      'paths.art_dir': '', 'paths.custom_art_dir': '',
      'paths.exports_dir': '', 'paths.backups_dir': '', 'paths.cardbacks_dir': '',
    },
    savingPaths: false,
    pathsDirty: false,

    // Configuración estática de los inputs. La label/description también
    // vive en DEFINITIONS del backend, pero aquí duplicamos las mínimas para
    // no depender de el JOIN + orden. Estable y auto-documentada.
    get customPathsConfig() {
      const map = [
        {
          key: 'paths.art_dir',
          label: 'Art cache (Scryfall)',
          description: 'Downloaded thumbnails. Can grow several GB — moving to another disk frees space on the main one.',
          effectiveKey: 'art_dir',
          hint: 'ej: D:\\mtg\\art',
        },
        {
          key: 'paths.custom_art_dir',
          label: 'User custom art',
          description: 'Local images that replace official art.',
          effectiveKey: 'custom_art_dir',
          hint: '',
        },
        {
          key: 'paths.exports_dir',
          label: 'Generated XMLs and PDFs',
          description: 'Final pipeline output. Useful to point to a cloud-synced folder.',
          effectiveKey: 'exports_dir',
          hint: '',
        },
        {
          key: 'paths.backups_dir',
          label: 'Backups (.zip)',
          description: 'Recommended to point to a different disk or cloud folder.',
          effectiveKey: 'backups_dir',
          hint: '',
        },
        {
          key: 'paths.cardbacks_dir',
          label: 'Cardbacks (back faces)',
          description: 'Back face images available in the picker.',
          effectiveKey: 'cardbacks_dir',
          hint: '',
        },
      ];
      return map.map(m => ({
        ...m,
        effectivePath: this.paths[m.effectiveKey] || '',
      }));
    },

    // Navegación entre secciones
    activeSection: 'general',
    searchQuery: '',
    showDrivesModal: false,

    // Resumen ligero de fuentes de arte (solo para la sección "Fuentes de arte").
    // El detalle vive en el modal (componente artSourcesPanel). Vive aquí y no en
    // un x-data hijo para que showDrivesModal y selectSection sean accesibles
    // desde los botones de esa sección sin necesidad de $parent (Alpine 3 no lo
    // expone como magic property).
    artSources: [],
    driveStats: {total_files: 0, sources_indexed: 0},
    hasGoogleApiKey: false,
    _artSummaryLoaded: false,

    get pinnedCount() {
      return this.artSources.filter(s => s.pinned).length;
    },
    get pctIndexed() {
      if (!this.artSources.length) return 0;
      return Math.round(100 * this.driveStats.sources_indexed / this.artSources.length);
    },

    // Definición del sidebar. `group` es la etiqueta que usa settings.py; la
    // relación key ↔ group se utiliza también para saltar de un resultado de
    // búsqueda a su sección.
    get sections() {
      const _t = window._t || ((k) => k);
      return [
        { key: 'general',      label: _t('settings_nav_general'),      icon: 'settings',      group: 'General' },
        { key: 'prices',       label: _t('settings_nav_prices'),       icon: 'dollar-sign',   group: 'Precios y envío' },
        { key: 'network',      label: _t('settings_nav_network'),      icon: 'wifi',          group: 'Red y conexión' },
        { key: 'autofill',     label: _t('settings_nav_autofill'),     icon: 'zap',           group: 'MPC Autofill' },
        { key: 'art-sources',  label: _t('settings_nav_art_sources'),  icon: 'image',         group: null },
        { key: 'custom-art',   label: _t('settings_nav_custom_art'),   icon: 'folder',        group: null },
        { key: 'backup',       label: _t('settings_nav_backup'),       icon: 'archive',       group: null },
        { key: 'log',          label: _t('settings_nav_log'),          icon: 'file-text',     group: null },
      ];
    },

    async load() {
      this.loading = true;
      try {
        const r = await fetch('/api/settings/');
        const data = await r.json();
        this.definitions = data.definitions;
        this.values = data.values;
        // Rutas efectivas + overrides — se pintan en la sección Backup y datos.
        // Fire-and-forget: si falla, la sección muestra "..." pero no rompe
        // el resto de settings.
        this.loadPaths();
      } catch (e) {
        window.toast(window._t('settings_loading'), e.message);
      } finally {
        this.loading = false;
        // Reload lucide iconos tras render inicial
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    selectSection(key) {
      this.activeSection = key;
      this.searchQuery = '';
      // Refrescar iconos tras el swap de sección (los que aparecen en el nuevo panel)
      this.$nextTick(() => window.icons && window.icons());
    },

    // Carga perezosa del resumen de fuentes de arte. Se dispara al entrar en la
    // sección "art-sources" (x-init en el div de la sección). Solo se ejecuta
    // una vez por vida de la vista — al reabrir el modal, artSourcesPanel hace
    // su propio load() completo y ese es el estado autoritativo mientras el
    // modal está abierto.
    async loadArtSourcesSummary() {
      if (this._artSummaryLoaded) return;
      this._artSummaryLoaded = true;
      try {
        const [r, statsR, settingsR] = await Promise.all([
          fetch('/api/art-sources/'),
          fetch('/api/drives/stats'),
          fetch('/api/settings/'),
        ]);
        if (r.ok) this.artSources = await r.json();
        if (statsR.ok) this.driveStats = await statsR.json();
        if (settingsR.ok) {
          const s = await settingsR.json();
          this.hasGoogleApiKey = !!(s.values.google_api_key || '').trim();
        }
      } catch (e) { /* silent — resumen es best-effort */ }
      this.$nextTick(() => window.icons && window.icons());
    },

    // ------- Helpers de agrupación / búsqueda -------

    defsInGroup(groupName) {
      return this.definitions.filter(d => d.group === groupName);
    },

    sectionKeyForGroup(groupName) {
      // Mapeo especial: los settings del grupo "Ubicación de datos" viven
      // dentro de la sección "Backup y datos" (no tienen sección propia).
      if (groupName === 'Ubicación de datos') return 'backup';
      const s = this.sections.find(x => x.group === groupName);
      return s ? s.key : 'general';
    },

    iconForGroup(groupName) {
      if (groupName === 'Ubicación de datos') return 'archive';
      const s = this.sections.find(x => x.group === groupName);
      return s ? s.icon : 'settings';
    },

    get searchResults() {
      const q = this.searchQuery.trim().toLowerCase();
      if (!q) return [];
      return this.definitions.filter(d => {
        return (d.label || '').toLowerCase().includes(q)
            || (d.description || '').toLowerCase().includes(q)
            || (d.key || '').toLowerCase().includes(q)
            || (d.group || '').toLowerCase().includes(q);
      });
    },

    isDefault(def) {
      return this.values[def.key] === def.default;
    },

    // ------- Actualización de un setting -------

    async update(key, value) {
      // Actualización optimista
      this.values[key] = value;
      try {
        const r = await fetch('/api/settings/', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({values: {[key]: value}}),
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const data = await r.json();
        this.values = data.values;
        window.toast(window._t('settings_saved'), key);
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
        this.load();  // recarga estado auténtico
      }
    },

    async resetAll() {
      if (!await window.confirmDialog(
        window._t('settings_reset_defaults'),
        window._t('common_reset') + '?',
        { danger: true, icon: 'rotate-ccw', confirmLabel: window._t('common_reset') })) return;
      const updates = {};
      for (const d of this.definitions) updates[d.key] = d.default;
      try {
        const r = await fetch('/api/settings/', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({values: updates}),
        });
        const data = await r.json();
        this.values = data.values;
        window.toast(window._t('settings_saved'), window._t('common_reset'));
      } catch (e) {
        window.toast('Error', e.message);
      }
    },

    async createBackup() {
      this.backingUp = true;
      try {
        const r = await fetch('/api/backup', {method: 'POST'});
        if (!r.ok) throw new Error(window._t('common_error'));
        this.lastBackup = await r.json();
        window.toast(window._t('settings_backup_created'), this.lastBackup.path);
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this.backingUp = false;
      }
    },

    // --- Métodos de paths ---
    // Se llama desde load() principal (al arrancar la vista) y desde el botón
    // Refrescar. Actualiza tanto `paths` (rutas efectivas) como los inputs
    // (pathOverrides) desde el valor guardado en `values`.
    async loadPaths() {
      try {
        const r = await fetch('/api/settings/paths');
        if (!r.ok) throw new Error(window._t('settings_paths'));
        this.paths = await r.json();
        // Poblamos los inputs con los overrides guardados. Si values[key] no
        // existe (primera vez), queda vacío = usa default.
        for (const k of Object.keys(this.pathOverrides)) {
          this.pathOverrides[k] = String(this.values[k] || '');
        }
        this.pathsDirty = false;
      } catch (e) {
        window.toast('Error', e.message);
      }
    },
    markPathDirty(_key) {
      // Detecta si algún override difiere del valor guardado en values.
      this.pathsDirty = this.customPathsConfig.some(cfg =>
        (this.pathOverrides[cfg.key] || '') !== String(this.values[cfg.key] || '')
      );
    },
    resetPath(key) {
      // Vaciar el override — el backend lo interpretará como "usar default".
      this.pathOverrides[key] = '';
      this.markPathDirty(key);
    },
    discardPathChanges() {
      // Restaurar los inputs al valor guardado en BD, descartando cambios.
      for (const k of Object.keys(this.pathOverrides)) {
        this.pathOverrides[k] = String(this.values[k] || '');
      }
      this.pathsDirty = false;
    },
    async savePathOverrides() {
      this.savingPaths = true;
      try {
        // Enviamos SOLO las claves de paths.* — no queremos que este PUT sobre-
        // escriba otros settings modificados en paralelo.
        const updates = {};
        for (const k of Object.keys(this.pathOverrides)) {
          updates[k] = (this.pathOverrides[k] || '').trim();
        }
        const r = await fetch('/api/settings/', {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({values: updates}),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(err.detail || window._t('common_error'));
        }
        const data = await r.json();
        this.values = data.values;
        // Recargamos las rutas efectivas para reflejar los cambios aplicados.
        await this.loadPaths();
        this.pathsDirty = false;
        window.toast(window._t('settings_paths_saved'), window._t('settings_paths_saved_desc'));
      } catch (e) {
        window.toast('Error', e.message);
      } finally {
        this.savingPaths = false;
      }
    },

    // ------- Renders inline (para vista de búsqueda y filas dentro de sección) -------
    //
    // En lugar de repetir el markup del input para cada tipo en cada sección,
    // usamos x-html con estos helpers. Como necesitamos acceder al método
    // update() al cambiar el valor, la función devuelve HTML con @change que
    // llama a $data.update(...). x-html + eventos delegados funciona bien
    // dentro de Alpine porque el HTML se procesa por Alpine tras insertarse.

    renderInput(def) {
      const val = this.values[def.key];
      const escLabel = String(val ?? '').replace(/"/g, '&quot;');
      if (def.type === 'bool') {
        return `
          <label class="relative inline-flex items-center cursor-pointer">
            <input type="checkbox" class="sr-only peer" ${val ? 'checked' : ''}
                   @change="update('${def.key}', $event.target.checked)">
            <div class="w-11 h-6 bg-bg-subtle border border-border-subtle rounded-full
                        peer peer-checked:after:translate-x-full
                        after:content-[''] after:absolute after:top-[3px] after:left-[3px]
                        after:bg-fg-muted after:rounded-full after:h-4 after:w-4
                        after:transition-all peer-checked:bg-accent/30 peer-checked:after:bg-accent
                        peer-checked:border-accent/50"></div>
          </label>`;
      }
      if (def.type === 'str' && def.choices) {
        const opts = def.choices.map(c =>
          `<option value="${c}" ${c === val ? 'selected' : ''}>${c}</option>`
        ).join('');
        return `<select @change="update('${def.key}', $event.target.value)"
                        class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm
                               focus:outline-none focus:border-accent min-w-[220px]">${opts}</select>`;
      }
      if (def.type === 'str') {
        return `<input type="text" value="${escLabel}"
                       @change="update('${def.key}', $event.target.value)"
                       class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm
                              focus:outline-none focus:border-accent w-72 md:w-80 max-w-full">`;
      }
      if (def.type === 'path') {
        // Como str pero con placeholder "por defecto" — comunica claramente
        // que vacío ≠ desactivado, sino "usar la ruta calculada por la app".
        return `<input type="text" value="${escLabel}" placeholder=""
                       @change="update('${def.key}', $event.target.value)"
                       class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm
                              font-mono focus:outline-none focus:border-accent w-72 md:w-80 max-w-full">`;
      }
      if (def.type === 'float' || def.type === 'int') {
        const step = def.type === 'int' ? '1' : '0.01';
        const min = def.min_value ?? '';
        const max = def.max_value ?? '';
        const parseFn = def.type === 'int' ? 'parseInt' : 'parseFloat';
        return `<input type="number" value="${escLabel}" step="${step}"
                       ${min !== '' ? `min="${min}"` : ''} ${max !== '' ? `max="${max}"` : ''}
                       @change="update('${def.key}', ${parseFn}($event.target.value))"
                       class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm text-right
                              focus:outline-none focus:border-accent w-32">`;
      }
      return '';
    },

    renderRow(def) {
      const desc = def.description || '';
      const isDefault = this.values[def.key] === def.default;
      const defaultBadge = !isDefault
        ? `<div class="text-[11px] text-accent/80 mt-1">≠ default (${String(def.default)})</div>`
        : '';
      return `
        <div class="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-2 md:gap-4 items-center">
          <div class="min-w-0">
            <label class="text-sm font-medium">${def.label}</label>
            <div class="text-xs text-fg-muted mt-0.5">${desc}</div>
            ${defaultBadge}
          </div>
          <div>${this.renderInput(def)}</div>
        </div>`;
    },
  };
}

// ============================================================================
// SUB-COMPONENTE: estado de detección MPC Autofill
// Se conserva sin cambios funcionales; solo estilos ajustados al nuevo panel.
// ============================================================================
function autofillStatus() {
  return {
    status: {available: false, exe_path: null, source: 'not_found', hint: null},
    async load() {
      try {
        const r = await fetch('/api/mpc-autofill/status');
        if (r.ok) this.status = await r.json();
      } catch (e) { /* silent */ }
      this.$nextTick(() => window.icons && window.icons());
    }
  };
}

// ============================================================================
// SUB-COMPONENTE: gestión completa de art sources (dentro del modal)
// Lógica idéntica a la versión anterior. Se llama con x-init="load()" al
// abrir el modal, así solo se cargan sus datos cuando el usuario lo pide.
// ============================================================================
function artSourcesPanel() {
  return {
    sources: [],
    loading: true,
    showAdd: false,
    saving: false,
    restoring: false,
    catalogSize: 67,
    stats: {total_files: 0, sources_indexed: 0},
    indexingIds: [],          // solo para indexOne (drive individual)
    hasApiKey: false,
    _pollTimer: null,         // polling de indexOne
    // --- Batch indexing state ---
    batchProgress: null,      // null = no batch; object = datos de /drives/index-progress
    _batchPollTimer: null,
    // --- Add form ---
    draft: {name: '', url: '', description: '', tags: '', pinned: false, source_type: ''},
    // Extras · F2/T7: estado de validación de URL antes de guardar.
    validating: false,
    validation: {
      checked: false, valid: false,
      detected_type: '', canonical_url: '', label: '', error: '',
    },

    async load() {
      this.loading = true;
      try {
        const [r, info, statsR, settingsR] = await Promise.all([
          fetch('/api/art-sources/'),
          fetch('/api/art-sources/catalog-info'),
          fetch('/api/drives/stats'),
          fetch('/api/settings/'),
        ]);
        if (r.ok) this.sources = await r.json();
        if (info.ok) {
          const d = await info.json();
          this.catalogSize = d.total_curated;
        }
        if (statsR.ok) this.stats = await statsR.json();
        if (settingsR.ok) {
          const s = await settingsR.json();
          this.hasApiKey = !!(s.values.google_api_key || '').trim();
        }
      } catch (e) { /* silent */ }
      finally {
        this.loading = false;
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    // ==================================================================
    // indexOne — sigue usando el endpoint individual (sin cambios)
    // ==================================================================
    async indexOne(s) {
      if (this.indexingIds.includes(s.id)) return;
      if (this.batchProgress) {
        window.toast('Batch en curso', 'Espera a que termine el indexado en lote.');
        return;
      }
      this.indexingIds.push(s.id);
      try {
        const wait = !this.hasApiKey;
        const r = await fetch(`/api/art-sources/${s.id}/index?wait=${wait}`, {method: 'POST'});
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        if (wait) {
          const data = await r.json();
          if (data.error) {
            window.toast(window._t('common_error'), data.error.substring(0, 100));
          } else {
            window.toast(window._t('settings_indexed_files') + ' ' + s.name,
              `+${data.files_added} / ~${data.files_updated} en ${data.folders_visited} carpetas`);
          }
        } else {
          window.toast(window._t('settings_indexing_progress'), s.name);
          this._startPolling();
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        if (!this.hasApiKey) {
          this.indexingIds = this.indexingIds.filter(id => id !== s.id);
          await this.load();
        }
      }
    },

    // Polling legacy para indexOne (sin cambios funcionales)
    _startPolling() {
      if (this._pollTimer) return;
      const initialSnapshot = {};
      for (const id of this.indexingIds) {
        const s = this.sources.find(x => x.id === id);
        initialSnapshot[id] = s ? {indexed_at: s.indexed_at, error: s.index_error} : null;
      }
      this._pollStartedAt = Date.now();
      this._pollSnapshot = initialSnapshot;

      this._pollTimer = setInterval(async () => {
        await this.load();
        const done = [];
        for (const id of this.indexingIds) {
          const before = this._pollSnapshot[id];
          const after = this.sources.find(s => s.id === id);
          if (!after) { done.push(id); continue; }
          const timeChanged = before && after.indexed_at && after.indexed_at !== before.indexed_at;
          const errorAppeared = after.index_error && (!before || after.index_error !== before.error);
          if (timeChanged || errorAppeared) done.push(id);
        }
        this.indexingIds = this.indexingIds.filter(id => !done.includes(id));
        if (this.indexingIds.length === 0 || (Date.now() - this._pollStartedAt) / 1000 > 900) {
          this.indexingIds = [];
          clearInterval(this._pollTimer);
          this._pollTimer = null;
        }
      }, 3000);
    },

    // ==================================================================
    // indexAll / indexPinned — ahora usan el endpoint batch
    // ==================================================================
    async _startBatch(sourceIds, mode) {
      if (this.batchProgress && !this.batchProgress.all_done && !this.batchProgress.cancelled) {
        window.toast('Batch en curso', 'Ya hay un indexado corriendo.');
        return;
      }
      try {
        const r = await fetch('/api/drives/index-batch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ source_ids: sourceIds, mode }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(err.detail || `Error ${r.status}`);
        }
        const data = await r.json();
        if (data.queued === 0) {
          window.toast(window._t('settings_indexed_files'), 'Todos los drives ya están indexados.');
          return;
        }
        // Arrancamos el polling de progreso
        this._startBatchPolling();
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      }
    },

    async indexPinned() {
      const pinned = this.sources.filter(s => s.pinned);
      if (pinned.length === 0) {
        window.toast(window._t('settings_pinned_col'), window._t('settings_pin_drive') + ' ★');
        return;
      }
      const pendingCount = pinned.filter(s => !s.indexed_at).length;
      const msg = pendingCount > 0
        ? `Indexar ${pendingCount} drives pinned pendientes?`
        : `Re-indexar ${pinned.length} drives pinned?`;
      if (!await window.confirmDialog(window._t('settings_index_pinned_btn'), msg,
        { icon: 'refresh-cw', confirmLabel: 'Indexar' })) return;
      const ids = pinned.map(s => s.id);
      await this._startBatch(ids, 'pending');
    },

    async indexAll() {
      const allIds = this.sources.map(s => s.id);
      const pendingCount = this.sources.filter(s => !s.indexed_at).length;
      const total = this.sources.length;
      const msg = pendingCount < total
        ? `Indexar ${pendingCount} drives pendientes? (${total - pendingCount} ya indexados se saltan)\n\nSe procesan de uno en uno — sin saturar la API.`
        : `Indexar los ${total} drives?\n\nSe procesan de uno en uno — sin saturar la API.`;
      if (!await window.confirmDialog(window._t('settings_index_all_btn'), msg,
        { icon: 'refresh-cw', confirmLabel: 'Indexar todos' })) return;
      await this._startBatch(allIds, 'pending');
    },

    // --- Batch polling ---
    _startBatchPolling() {
      if (this._batchPollTimer) return;
      // Primera lectura inmediata
      this._pollBatchProgress();
      this._batchPollTimer = setInterval(() => this._pollBatchProgress(), 2500);
    },

    async _pollBatchProgress() {
      try {
        const r = await fetch('/api/drives/index-progress');
        if (!r.ok) return;
        const data = await r.json();
        this.batchProgress = data;
        this.$nextTick(() => window.icons && window.icons());

        // ¿Terminó?
        if (data.all_done || data.cancelled) {
          clearInterval(this._batchPollTimer);
          this._batchPollTimer = null;
          // Recargar la lista de sources para ver los nuevos indexed_at
          await this.load();
        }
      } catch (e) { /* silent */ }
    },

    async cancelBatch() {
      try {
        await fetch('/api/drives/index-cancel', {method: 'POST'});
        // El polling detectará el cancelled y parará solo
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      }
    },

    async closeBatchProgress() {
      // Limpiar estado del batch
      this.batchProgress = null;
      clearInterval(this._batchPollTimer);
      this._batchPollTimer = null;
      try {
        await fetch('/api/drives/index-clear', {method: 'POST'});
      } catch (e) { /* silent */ }
      await this.load();
    },

    // --- Helpers para la vista de progreso ---
    batchProgressTitle() {
      if (!this.batchProgress) return '';
      const p = this.batchProgress;
      if (p.cancelled) return `Indexado cancelado — ${p.done_count} de ${p.total} completados`;
      if (p.all_done) return `Indexado completo — ${p.done_count} drives`;
      return `Indexando ${p.done_count + 1} de ${p.total}…`;
    },

    formatEta(seconds) {
      if (!seconds || seconds < 0) return '';
      if (seconds < 60) return `${Math.round(seconds)}s`;
      const m = Math.floor(seconds / 60);
      const s = Math.round(seconds % 60);
      return s > 0 ? `${m}m ${s}s` : `${m}m`;
    },

    // ==================================================================
    // Resto de métodos (sin cambios)
    // ==================================================================

    async restore() {
      this.restoring = true;
      try {
        const r = await fetch('/api/art-sources/restore-catalog', {method: 'POST'});
        if (!r.ok) throw new Error('Error');
        const d = await r.json();
        if (d.added === 0) {
          window.toast(window._t('settings_restore_catalog'), `${d.total_curated}`);
        } else {
          window.toast(`${d.added} ${window._t('settings_restore_catalog')}`, `${d.total_curated}`);
        }
        await this.load();
      } catch (e) {
        window.toast('Error', e.message);
      } finally {
        this.restoring = false;
      }
    },

    async add() {
      if (!this.draft.name || !this.draft.url) return;
      this.saving = true;
      try {
        const r = await fetch('/api/art-sources/', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(this.draft),
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        window.toast(window._t('settings_add_btn'), this.draft.name);
        this.draft = {name: '', url: '', description: '', tags: '', pinned: false, source_type: ''};
        this.validation = {checked: false, valid: false, detected_type: '', canonical_url: '', label: '', error: ''};
        this.showAdd = false;
        await this.load();
      } catch (e) {
        window.toast('Error', e.message);
      } finally {
        this.saving = false;
      }
    },

    urlPlaceholder() {
      switch (this.draft.source_type) {
        case 'gdrive':       return 'https://drive.google.com/drive/folders/…';
        case 'gdrive-file':  return 'https://drive.google.com/file/d/…';
        case 'local-folder': return 'C:/mtg-art/  o  /home/user/mtg-art  o  file://…';
        case 'http-listing': return 'https://example.com/manifest.json';
        case 's3':           return 's3://bucket/prefix  o  https://bucket.s3.amazonaws.com/';
        default:             return 'URL o ruta (se auto-detectará el tipo)';
      }
    },

    async validateSource() {
      if (!this.draft.url) {
        this.validation = {checked: false, valid: false, detected_type: '', canonical_url: '', label: '', error: ''};
        return;
      }
      this.validating = true;
      try {
        const r = await fetch('/api/art-sources/validate', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url: this.draft.url, source_type: this.draft.source_type || null}),
        });
        if (r.ok) {
          const d = await r.json();
          this.validation = {
            checked: true,
            valid: d.valid,
            detected_type: d.detected_type,
            canonical_url: d.canonical_url,
            label: d.label,
            error: d.error || '',
          };
          if (d.valid && !this.draft.source_type) {
            this.draft.source_type = d.detected_type;
          }
        } else {
          this.validation = {checked: true, valid: false, detected_type: '', canonical_url: '', label: '', error: 'Error del servidor'};
        }
      } catch (e) {
        this.validation = {checked: true, valid: false, detected_type: '', canonical_url: '', label: '', error: e.message};
      } finally {
        this.validating = false;
      }
    },

    async togglePin(s) {
      try {
        const r = await fetch(`/api/art-sources/${s.id}`, {
          method: 'PATCH', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({pinned: !s.pinned}),
        });
        if (!r.ok) throw new Error('Error');
        await this.load();
      } catch (e) { window.toast('Error', e.message); }
    },

    async remove(s) {
      if (!await window.confirmDialog(
        window._t('common_delete'),
        `"${s.name}"`,
        { danger: true, icon: 'trash-2', confirmLabel: window._t('common_delete') })) return;
      try {
        const r = await fetch(`/api/art-sources/${s.id}`, {method: 'DELETE'});
        if (!r.ok) throw new Error('Error');
        window.toast(window._t('common_delete'), s.name);
        await this.load();
      } catch (e) { window.toast('Error', e.message); }
    }
  };
}

// ============================================================================
// SUB-COMPONENTE: log de depuración (sin cambios funcionales)
// ============================================================================
function debugLogPanel() {
  return {
    info: {exists: false, path: null, size_bytes: 0},
    showTail: false,
    tailText: '',
    copied: false,
    async load() {
      try {
        const r = await fetch('/api/debug/log/info');
        if (r.ok) this.info = await r.json();
      } catch (e) { /* silent */ }
      this.$nextTick(() => window.icons && window.icons());
    },
    async viewTail() {
      try {
        const r = await fetch('/api/debug/log/tail?n=500');
        this.tailText = await r.text();
        this.showTail = true;
        this.$nextTick(() => window.icons && window.icons());
      } catch (e) {
        window.toast('Error', e.message);
      }
    },
    async copyTail() {
      try {
        await navigator.clipboard.writeText(this.tailText);
        this.copied = true;
        setTimeout(() => this.copied = false, 2000);
      } catch (e) {
        window.toast(window._t('common_error'), '');
      }
    }
  };
}


// --- Puente con Alpine -------------------------------------
// Alpine resuelve las expresiones de `x-data` contra el ámbito
// global, así que estas funciones tienen que estar en `window`.
window.artSourcesPanel = artSourcesPanel
window.autofillStatus = autofillStatus
window.debugLogPanel = debugLogPanel
window.settingsShell = settingsShell
