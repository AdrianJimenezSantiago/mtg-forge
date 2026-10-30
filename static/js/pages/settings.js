function settingsShell() {
  return {
    loading: true,
    definitions: [],
    secretsSet: [],
    secretInputs: {},
    values: {},
    tiersDraft: [],
    tiersDirty: false,
    saving: false,
    backingUp: false,
    lastBackup: null,

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

    storage: null,
    storageLoading: false,
    storageError: '',
    storagePurging: '',
    exportsKeepRecent: true,
    _storageLoaded: false,
    backups: [],
    backupsLoading: false,
    restoringBackup: '',

    get customPathsConfig() {
      const map = [
        {
          key: 'paths.art_dir',
          storageKey: 'art',
          label: window._t('setting_paths_art_dir_label'),
          description: window._t('setting_paths_art_dir_desc'),
          effectiveKey: 'art_dir',
          hint: 'D:\\mtg\\art',
        },
        {
          key: 'paths.custom_art_dir',
          storageKey: 'custom_art',
          label: window._t('setting_paths_custom_art_dir_label'),
          description: window._t('setting_paths_custom_art_dir_desc'),
          effectiveKey: 'custom_art_dir',
          hint: '',
        },
        {
          key: 'paths.exports_dir',
          storageKey: 'exports',
          label: window._t('setting_paths_exports_dir_label'),
          description: window._t('setting_paths_exports_dir_desc'),
          effectiveKey: 'exports_dir',
          hint: '',
        },
        {
          key: 'paths.backups_dir',
          storageKey: 'backups',
          label: window._t('setting_paths_backups_dir_label'),
          description: window._t('setting_paths_backups_dir_desc'),
          effectiveKey: 'backups_dir',
          hint: '',
        },
        {
          key: 'paths.cardbacks_dir',
          storageKey: 'cardbacks',
          label: window._t('setting_paths_cardbacks_dir_label'),
          description: window._t('setting_paths_cardbacks_dir_desc'),
          effectiveKey: 'cardbacks_dir',
          hint: '',
        },
      ];
      return map.map(m => ({
        ...m,
        effectivePath: this.paths[m.effectiveKey] || '',
        bytes: this.sizeForCategory(m.storageKey),
      }));
    },

    activeSection: 'general',
    searchQuery: '',
    showDrivesModal: false,

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

    get sections() {
      const _t = window._t || ((k) => k);
      return [
        { key: 'general',      label: _t('settings_nav_general'),      icon: 'settings',      group: 'General' },
        { key: 'prices',       label: _t('settings_nav_prices'),       icon: 'dollar-sign',   group: 'Precios y envío' },
        { key: 'network',      label: _t('settings_nav_network'),      icon: 'wifi',          group: 'Red y conexión' },
        { key: 'autofill',     label: _t('settings_nav_autofill'),     icon: 'zap',           group: 'MPC Autofill' },
        { key: 'art-sources',  label: _t('settings_nav_art_sources'),  icon: 'image',         group: null },
        { key: 'offline',      label: _t('settings_nav_offline'),      icon: 'cloud-download', group: 'Datos de cartas' },
        { key: 'custom-art',   label: _t('settings_nav_custom_art'),   icon: 'folder',        group: null },
        { key: 'backup',       label: _t('settings_nav_backup'),       icon: 'archive',       group: null },
        { key: 'storage',      label: _t('settings_nav_storage'),      icon: 'hard-drive',    group: null },
        { key: 'log',          label: _t('settings_nav_log'),          icon: 'file-text',     group: null },
      ];
    },

    _sectionFromHash() {
      const key = decodeURIComponent((window.location.hash || '').slice(1));
      if (key && this.sections.some((s) => s.key === key) && key !== this.activeSection) {
        this.selectSection(key);
      }
    },

    async load() {
      if (!this._hashBound) {
        this._hashBound = true;
        window.addEventListener('hashchange', () => this._sectionFromHash());
        this._sectionFromHash();
      }
      this.loading = true;
      try {
        const r = await fetch('/api/settings/');
        const data = await r.json();
        this.definitions = data.definitions;
        this.values = data.values;
        this.secretsSet = data.secrets_set || [];
        this._syncTiers();
        this.loadPaths();
      } catch (e) {
        window.toast(window._t('settings_error_loading'), e.message, 'error');
      } finally {
        this.loading = false;
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    selectSection(key) {
      this.activeSection = key;
      this.searchQuery = '';
      if (key === 'storage' || key === 'backup') this.loadStorage();
      if (key === 'backup') this.loadBackups();
      this.$nextTick(() => window.icons && window.icons());
    },

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
      } catch (e) {}
      this.$nextTick(() => window.icons && window.icons());
    },

    defsInGroup(groupName) {
      return this.definitions.filter(d => d.group === groupName);
    },

    iconForSection(sectionKey) {
      const s = this.sections.find(x => x.key === sectionKey);
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
      if (def.secret) return !this.isSecretSet(def.key);
      return this.values[def.key] === def.default;
    },

    isSecretSet(key) {
      return (this.secretsSet || []).includes(key);
    },

    async updateSecret(key, value) {
      const text = String(value || '').trim();
      if (!text) return;
      await this.update(key, text);
      this.secretInputs[key] = '';
    },

    async clearSecret(key) {
      await this.update(key, '__CLEAR__');
      this.secretInputs[key] = '';
    },

    _syncTiers() {
      const tiers = this.values.mpc_tiers;
      this.tiersDraft = Array.isArray(tiers) ? tiers.map((t) => ({ ...t })) : [];
      this.tiersDirty = false;
    },

    async saveTiers() {
      const tiers = this.tiersDraft
        .filter((t) => t.size > 0 && t.unit_usd > 0)
        .sort((a, b) => a.size - b.size);
      await this.update('mpc_tiers', tiers);
      this._syncTiers();
    },

    resetTiers() {
      const def = this.definitions.find((d) => d.key === 'mpc_tiers');
      if (!def) return;
      this.tiersDraft = def.default.map((t) => ({ ...t }));
      this.tiersDirty = true;
    },

    async update(key, value) {
      this.values[key] = value;
      try {
        const r = await fetch('/api/settings/', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({values: {[key]: value}}),
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const data = await r.json();
        this.values = data.values;
        this.secretsSet = data.secrets_set || [];
        if (key === 'mpc_tiers') this._syncTiers();
        window.toast(window._t('settings_saved'), '', 'success');
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
        this.load();
      }
    },

    async resetAll() {
      if (!await window.confirmDialog(
        window._t('settings_reset_defaults'),
        window._t('common_reset') + '?',
        { danger: true, icon: 'rotate-ccw', confirmLabel: window._t('common_reset') })) return;
      const updates = {};
      for (const d of this.definitions) {
        if (d.secret) continue;
        updates[d.key] = d.default;
      }
      try {
        const r = await fetch('/api/settings/', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({values: updates}),
        });
        const data = await r.json();
        this.values = data.values;
        this.secretsSet = data.secrets_set || [];
        window.toast(window._t('settings_saved'), window._t('settings_reset_done'), 'success');
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      }
    },

    async createBackup() {
      this.backingUp = true;
      try {
        const r = await fetch('/api/backup', {method: 'POST'});
        if (!r.ok) throw new Error(window._t('common_error'));
        this.lastBackup = await r.json();
        window.toast(window._t('settings_backup_created'), this.lastBackup.path, 'success');
        this.loadBackups();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.backingUp = false;
      }
    },

    async loadBackups() {
      this.backupsLoading = true;
      try {
        this.backups = await window.api.backups.list();
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error');
      } finally {
        this.backupsLoading = false;
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    backupTagLabel(tag) {
      const key = 'backup_tag_' + String(tag || '').replace('-', '_');
      return (window._T && window._T[key]) || tag;
    },

    _backupSummaryText(s) {
      return window._t('backup_restore_summary')
        .replace('{decks}', s.decks).replace('{cards}', s.cards)
        .replace('{collection}', s.collection).replace('{arts}', s.custom_arts + s.art_files)
        .replace('{date}', new Date(s.created_at).toLocaleString());
    },

    async restoreBackup(b) {
      let summary;
      try {
        summary = await window.api.backups.inspect(b.filename);
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error');
        return;
      }
      const ok = await window.confirmDialog(
        window._t('backup_restore_confirm_title'),
        this._backupSummaryText(summary) + '\n\n' + window._t('backup_restore_confirm_body'),
        { danger: true, icon: 'rotate-ccw', confirmLabel: window._t('backup_restore_btn') },
      );
      if (!ok) return;
      this.restoringBackup = b.filename;
      try {
        const res = await window.api.backups.restore(b.filename);
        window.toast(window._t('backup_restore_done'),
          window._t('backup_restore_done_body').replace('{file}', res.safety_backup), 'success');
        setTimeout(() => window.location.reload(), 1500);
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error');
        this.restoringBackup = '';
      }
    },

    async uploadBackup(ev) {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (!file) return;
      this.restoringBackup = '__upload__';
      try {
        const body = new FormData();
        body.append('file', file);
        const r = await fetch('/api/backups/upload', { method: 'POST', body });
        if (!r.ok) throw new window.ApiError(r.status, await window.apiErrorDetail(r), r.url);
        const summary = await r.json();
        this.restoringBackup = '';
        await this.loadBackups();
        await this.restoreBackup({ filename: summary.filename });
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error');
        this.restoringBackup = '';
      }
    },

    async deleteBackup(b) {
      const ok = await window.confirmDialog(window._t('backup_delete_title'),
        new Date(b.created_at).toLocaleString(), { danger: true });
      if (!ok) return;
      try {
        await window.api.backups.remove(b.filename);
        this.backups = this.backups.filter((x) => x.filename !== b.filename);
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error');
      }
    },

    async loadStorage(refresh = false) {
      if (this._storageLoaded && !refresh) return;
      this._storageLoaded = true;
      this.storageLoading = true;
      this.storageError = '';
      try {
        const r = await fetch(`/api/storage/${refresh ? '?refresh=true' : ''}`);
        if (!r.ok) throw new Error(window._t('storage_error'));
        this.storage = await r.json();
      } catch (e) {
        this.storageError = e.message;
      } finally {
        this.storageLoading = false;
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    async purgeStorage(target) {
      const confirmKey = {
        exports: 'storage_confirm_exports',
        backups: 'storage_confirm_backups',
      }[target];
      if (confirmKey && !window.confirm(window._t(confirmKey))) return;

      this.storagePurging = target;
      try {
        const r = await fetch('/api/storage/purge', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            targets: [target],
            exports_older_than_days: this.exportsKeepRecent ? 30 : 0,
          }),
        });
        if (!r.ok) {
          throw new Error(await window.apiErrorDetail(r));
        }
        const data = await r.json();
        this.storage = data.storage;
        window.toast(
          window._t('storage_cleanup_done'),
          window._t('storage_cleanup_freed')
            .replace('{size}', this.fmtBytes(data.freed_bytes))
            .replace('{n}', data.removed),
          'success',
        );
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.storagePurging = '';
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    fmtBytes(value) {
      const bytes = Number(value) || 0;
      if (bytes < 1024) return `${bytes} B`;
      const units = ['KB', 'MB', 'GB', 'TB'];
      let size = bytes / 1024;
      let i = 0;
      while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
      return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[i]}`;
    },

    storagePct(bytes) {
      const total = this.storage && this.storage.totals ? this.storage.totals.bytes : 0;
      if (!total) return 0;
      return (Number(bytes) || 0) * 100 / total;
    },

    get storageRows() {
      if (!this.storage) return [];
      return this.storage.categories
        .filter(c => c.bytes > 0 || c.exists)
        .sort((a, b) => b.bytes - a.bytes);
    },

    get storagePurgeable() {
      return this.storageRows.filter(c => c.reclaimable && c.bytes > 0);
    },

    catLabel(key) { return window._t(`storage_cat_${key}`); },
    catDesc(key) { return window._t(`storage_cat_${key}_desc`); },
    kindLabel(kind) { return window._t(`storage_kind_${kind}`); },

    kindColor(kind) {
      return {
        essential:   '#d4af37',
        refetchable: '#5b9bd5',
        derived:     '#8e8ea6',
        output:      '#4ade80',
        safety:      '#fb923c',
      }[kind] || '#8e8ea6';
    },

    diskFreeText(vol) {
      return window._t('storage_disk_free')
        .replace('{size}', this.fmtBytes(vol.free_bytes))
        .replace('{total}', this.fmtBytes(vol.total_bytes));
    },
    diskShareText(vol) {
      const pct = vol.total_bytes
        ? (vol.app_bytes * 100 / vol.total_bytes)
        : 0;
      const shown = pct > 0 && pct < 0.1 ? '<0,1' : pct.toFixed(1);
      return window._t('storage_disk_app_share').replace('{pct}', shown);
    },
    diskUsedPct(vol) {
      return vol.total_bytes ? vol.used_bytes * 100 / vol.total_bytes : 0;
    },
    backupEstimateText() {
      if (!this.storage) return '';
      return window._t('storage_backup_next')
        .replace('{size}', this.fmtBytes(this.storage.backup_estimate.full_bytes))
        .replace('{db}', this.fmtBytes(this.storage.backup_estimate.db_only_bytes));
    },
    backupsStoredText() {
      if (!this.storage) return '';
      const b = this.storage.backups;
      if (!b.count) return window._t('storage_backup_none');
      return window._t('storage_backup_stored')
        .replace('{n}', b.count)
        .replace('{size}', this.fmtBytes(b.bytes));
    },

    sizeForCategory(key) {
      if (!this.storage) return null;
      const row = this.storage.categories.find(c => c.key === key);
      return row ? row.bytes : null;
    },

    async loadPaths() {
      try {
        const r = await fetch('/api/settings/paths');
        if (!r.ok) throw new Error(window._t('settings_paths'));
        this.paths = await r.json();
        for (const k of Object.keys(this.pathOverrides)) {
          this.pathOverrides[k] = String(this.values[k] || '');
        }
        this.pathsDirty = false;
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      }
    },
    markPathDirty(_key) {
      this.pathsDirty = this.customPathsConfig.some(cfg =>
        (this.pathOverrides[cfg.key] || '') !== String(this.values[cfg.key] || '')
      );
    },
    resetPath(key) {
      this.pathOverrides[key] = '';
      this.markPathDirty(key);
    },
    discardPathChanges() {
      for (const k of Object.keys(this.pathOverrides)) {
        this.pathOverrides[k] = String(this.values[k] || '');
      }
      this.pathsDirty = false;
    },
    async savePathOverrides() {
      this.savingPaths = true;
      try {
        const updates = {};
        for (const k of Object.keys(this.pathOverrides)) {
          updates[k] = (this.pathOverrides[k] || '').trim();
        }
        const r = await fetch('/api/settings/', {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({values: updates}),
        });
        if (!r.ok) {
          throw new Error(await window.apiErrorDetail(r));
        }
        const data = await r.json();
        this.values = data.values;
        await this.loadPaths();
        this.pathsDirty = false;
        window.toast(window._t('settings_paths_saved'), window._t('settings_paths_saved_desc'), 'success');
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.savingPaths = false;
      }
    },

    renderSecretInput(def) {
      const isSet = this.isSecretSet(def.key);
      const placeholder = isSet
        ? window._t('settings_secret_stored')
        : window._t('settings_secret_empty');
      const clearBtn = isSet
        ? `<button type="button" @click="clearSecret('${def.key}')"
                   class="text-xs text-fg-muted hover:text-danger px-2 py-1 rounded
                          border border-border-subtle hover:border-danger/50 shrink-0"
                   title="${window._t('settings_secret_clear')}">
             ${window._t('settings_secret_clear')}
           </button>`
        : '';
      return `
        <div class="flex items-center gap-2">
          <input id="${this.inputId(def)}" type="password" autocomplete="off" spellcheck="false" value=""
                 placeholder="${placeholder}"
                 @change="updateSecret('${def.key}', $event.target.value)"
                 class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm
                        focus:outline-none focus:border-accent w-72 md:w-80 max-w-full">
          ${clearBtn}
        </div>`;
    },

    inputId(def) {
      return `setting-${def.key}`;
    },

    renderInput(def) {
      const val = this.values[def.key];
      const escLabel = String(val ?? '').replace(/"/g, '&quot;');
      if (def.secret) return this.renderSecretInput(def);
      if (def.type === 'bool') {
        return `
          <label class="relative inline-flex items-center cursor-pointer">
            <input id="${this.inputId(def)}" type="checkbox" class="sr-only peer" ${val ? 'checked' : ''}
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
        return `<select id="${this.inputId(def)}" @change="update('${def.key}', $event.target.value)"
                        class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm
                               focus:outline-none focus:border-accent min-w-[220px]">${opts}</select>`;
      }
      if (def.type === 'str') {
        return `<input id="${this.inputId(def)}" type="text" value="${escLabel}"
                       @change="update('${def.key}', $event.target.value)"
                       class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm
                              focus:outline-none focus:border-accent w-72 md:w-80 max-w-full">`;
      }
      if (def.type === 'path') {
        return `<input id="${this.inputId(def)}" type="text" value="${escLabel}" placeholder=""
                       @change="update('${def.key}', $event.target.value)"
                       class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm
                              font-mono focus:outline-none focus:border-accent w-72 md:w-80 max-w-full">`;
      }
      if (def.type === 'float' || def.type === 'int') {
        const step = def.type === 'int' ? '1' : '0.01';
        const min = def.min_value ?? '';
        const max = def.max_value ?? '';
        const parseFn = def.type === 'int' ? 'parseInt' : 'parseFloat';
        return `<input id="${this.inputId(def)}" type="number" value="${escLabel}" step="${step}"
                       ${min !== '' ? `min="${min}"` : ''} ${max !== '' ? `max="${max}"` : ''}
                       @change="update('${def.key}', ${parseFn}($event.target.value))"
                       class="bg-bg-subtle border border-border-subtle rounded-md px-3 py-1.5 text-sm text-right
                              focus:outline-none focus:border-accent w-32">`;
      }
      return '';
    },

    renderRow(def) {
      const desc = def.description || '';
      const isDefault = def.secret
        ? !this.isSecretSet(def.key)
        : this.values[def.key] === def.default;
      const defaultBadge = def.secret
        ? (this.isSecretSet(def.key)
            ? `<div class="text-[11px] text-accent/80 mt-1">${window._t('settings_secret_stored')}</div>`
            : '')
        : (!isDefault
            ? `<div class="text-[11px] text-accent/80 mt-1">≠ ${window._t('settings_default_value')} (${String(def.default)})</div>`
            : '');
      return `
        <div class="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-2 md:gap-4 items-center">
          <div class="min-w-0">
            <label for="${this.inputId(def)}" class="text-sm font-medium">${def.label}</label>
            <div class="text-xs text-fg-muted mt-0.5">${desc}</div>
            ${defaultBadge}
          </div>
          <div>${this.renderInput(def)}</div>
        </div>`;
    },
  };
}

function bulkData() {
  return {
    status: { printings: 0, unique_cards: 0, progress: {} },
    checking: false,
    starting: false,
    _pollTimer: null,

    async init() {
      await this.refresh();
      if (this.active) this._startPolling();
    },

    get progress() { return this.status.progress || {}; },
    get lastSync() {
      const s = (this.status.syncs || []).find(x => x.kind === 'default_cards');
      return s && s.updated_at ? new Date(s.updated_at).toLocaleDateString() : '';
    },
    get active()   { return !!this.progress.active; },
    get percent()  { return Math.round(this.progress.percent || 0); },

    get eta() {
      const s = this.progress.eta_seconds;
      if (!s || s <= 0) return '';
      const m = Math.floor(s / 60);
      return m > 0 ? `~${m} min` : `~${Math.round(s)} s`;
    },

    async refresh() {
      try {
        const r = await fetch('/api/bulk/status');
        if (r.ok) this.status = await r.json();
      } catch (e) {}
    },

    async start(force = false) {
      this.starting = true;
      try {
        const r = await fetch(`/api/bulk/sync?force=${force ? 'true' : 'false'}`, {
          method: 'POST',
        });
        if (!r.ok) {
          throw new Error(await window.apiErrorDetail(r));
        }
        window.toast(window._t('bulk_started'), window._t('bulk_started_desc'), 'info');
        this._startPolling();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.starting = false;
      }
    },

    async cancel() {
      try {
        await fetch('/api/bulk/cancel', { method: 'POST' });
        window.toast(window._t('bulk_cancelled'), window._t('bulk_cancelled_desc'), 'info');
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      }
      await this.refresh();
    },

    _startPolling() {
      if (this._pollTimer) return;
      this._pollTimer = setInterval(async () => {
        await this.refresh();
        if (!this.active) {
          clearInterval(this._pollTimer);
          this._pollTimer = null;
          if (this.progress.phase === 'done') {
            window.toast(window._t('bulk_done'), window._t('bulk_done_desc'), 'success');
          } else if (this.progress.error && !this.progress.cancelled) {
            window.toast(window._t('common_error'), this.progress.error, 'error');
          }
        }
      }, 1500);
    },

    destroy() {
      if (this._pollTimer) clearInterval(this._pollTimer);
    },
  };
}

function autofillStatus() {
  return {
    status: {available: false, exe_path: null, source: 'not_found', hint: null},
    async load() {
      try {
        const r = await fetch('/api/mpc-autofill/status');
        if (r.ok) this.status = await r.json();
      } catch (e) {}
      this.$nextTick(() => window.icons && window.icons());
    }
  };
}

function artSourcesPanel() {
  return {
    sources: [],
    loading: true,
    showAdd: false,
    saving: false,
    restoring: false,
    catalogSize: 67,
    stats: {total_files: 0, sources_indexed: 0},
    indexingIds: [],
    hasApiKey: false,
    _pollTimer: null,
    batchProgress: null,
    _batchPollTimer: null,
    draft: {name: '', url: '', description: '', tags: '', pinned: false, source_type: ''},
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
      } catch (e) {}
      finally {
        this.loading = false;
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    async indexOne(s) {
      if (this.indexingIds.includes(s.id)) return;
      if (this.batchProgress) {
        window.toast(window._t('settings_batch_running'), window._t('settings_batch_wait'), 'warning');
        return;
      }
      this.indexingIds.push(s.id);
      try {
        const wait = !this.hasApiKey;
        const r = await fetch(`/api/art-sources/${s.id}/index?wait=${wait}`, {method: 'POST'});
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        if (wait) {
          const data = await r.json();
          if (data.error) {
            window.toast(window._t('common_error'), data.error.substring(0, 100), 'error');
          } else {
            window.toast(window._t('settings_indexed_files') + ' ' + s.name,
              window._t('settings_indexed_summary')
                .replace('{added}', data.files_added)
                .replace('{updated}', data.files_updated)
                .replace('{folders}', data.folders_visited),
              'success');
          }
        } else {
          window.toast(window._t('settings_indexing_progress'), s.name, 'info');
          this._startPolling();
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        if (!this.hasApiKey) {
          this.indexingIds = this.indexingIds.filter(id => id !== s.id);
          await this.load();
        }
      }
    },

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

    async _startBatch(sourceIds, mode) {
      if (this.batchProgress && !this.batchProgress.all_done && !this.batchProgress.cancelled) {
        window.toast(window._t('settings_batch_running'), window._t('settings_batch_wait'), 'warning');
        return;
      }
      try {
        const r = await fetch('/api/drives/index-batch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ source_ids: sourceIds, mode }),
        });
        if (!r.ok) {
          throw new Error(await window.apiErrorDetail(r));
        }
        const data = await r.json();
        if (data.queued === 0) {
          window.toast(window._t('settings_all_indexed'), '', 'info');
          return;
        }
        this._startBatchPolling();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      }
    },

    async indexPinned() {
      const pinned = this.sources.filter(s => s.pinned);
      if (pinned.length === 0) {
        window.toast(window._t('settings_no_pinned'), window._t('settings_no_pinned_desc'), 'info');
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
        ? window._t('settings_index_pending_confirm').replace('{n}', pendingCount).replace('{skipped}', total - pendingCount)
        : window._t('settings_index_all_confirm').replace('{n}', total);
      if (!await window.confirmDialog(window._t('settings_index_all_btn'), msg,
        { icon: 'refresh-cw', confirmLabel: window._t('settings_index_all_btn') })) return;
      await this._startBatch(allIds, 'pending');
    },

    _startBatchPolling() {
      if (this._batchPollTimer) return;
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

        if (data.all_done || data.cancelled) {
          clearInterval(this._batchPollTimer);
          this._batchPollTimer = null;
          await this.load();
        }
      } catch (e) {}
    },

    async cancelBatch() {
      try {
        await fetch('/api/drives/index-cancel', {method: 'POST'});
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      }
    },

    async closeBatchProgress() {
      this.batchProgress = null;
      clearInterval(this._batchPollTimer);
      this._batchPollTimer = null;
      try {
        await fetch('/api/drives/index-clear', {method: 'POST'});
      } catch (e) {}
      await this.load();
    },

    batchProgressTitle() {
      if (!this.batchProgress) return '';
      const p = this.batchProgress;
      if (p.cancelled) return window._t('settings_index_cancelled').replace('{done}', p.done_count).replace('{total}', p.total);
      if (p.all_done) return window._t('settings_index_done').replace('{n}', p.done_count);
      return window._t('settings_index_running').replace('{current}', p.done_count + 1).replace('{total}', p.total);
    },

    formatEta(seconds) {
      if (!seconds || seconds < 0) return '';
      if (seconds < 60) return `${Math.round(seconds)}s`;
      const m = Math.floor(seconds / 60);
      const s = Math.round(seconds % 60);
      return s > 0 ? `${m}m ${s}s` : `${m}m`;
    },

    async restore() {
      this.restoring = true;
      try {
        const r = await fetch('/api/art-sources/restore-catalog', {method: 'POST'});
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const d = await r.json();
        if (d.added === 0) {
          window.toast(window._t('settings_restore_none'),
            window._t('settings_restore_total').replace('{n}', d.total_curated), 'info');
        } else {
          window.toast(window._t('settings_restore_added').replace('{n}', d.added),
            window._t('settings_restore_total').replace('{n}', d.total_curated), 'success');
        }
        await this.load();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
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
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        window.toast(window._t('settings_source_added'), this.draft.name, 'success');
        this.draft = {name: '', url: '', description: '', tags: '', pinned: false, source_type: ''};
        this.validation = {checked: false, valid: false, detected_type: '', canonical_url: '', label: '', error: ''};
        this.showAdd = false;
        await this.load();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.saving = false;
      }
    },

    urlPlaceholder() {
      switch (this.draft.source_type) {
        case 'gdrive':       return 'https://drive.google.com/drive/folders/…';
        case 'gdrive-file':  return 'https://drive.google.com/file/d/…';
        case 'local-folder': return 'C:/mtg-art/  ·  /home/user/mtg-art  ·  file://…';
        case 'http-listing': return 'https://example.com/manifest.json';
        case 's3':           return 's3://bucket/prefix  ·  https://bucket.s3.amazonaws.com/';
        default:             return window._t('settings_source_placeholder_auto');
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
          this.validation = {checked: true, valid: false, detected_type: '', canonical_url: '', label: '', error: window._t('js_error_server')};
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
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        await this.load();
      } catch (e) { window.toast(window._t('common_error'), e.message, 'error'); }
    },

    async remove(s) {
      if (!await window.confirmDialog(
        window._t('common_delete'),
        `"${s.name}"`,
        { danger: true, icon: 'trash-2', confirmLabel: window._t('common_delete') })) return;
      try {
        const r = await fetch(`/api/art-sources/${s.id}`, {method: 'DELETE'});
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        window.toast(window._t('settings_source_removed'), s.name, 'success');
        await this.load();
      } catch (e) { window.toast(window._t('common_error'), e.message, 'error'); }
    }
  };
}

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
      } catch (e) {}
      this.$nextTick(() => window.icons && window.icons());
    },
    async viewTail() {
      try {
        const r = await fetch('/api/debug/log/tail?n=500');
        this.tailText = await r.text();
        this.showTail = true;
        this.$nextTick(() => window.icons && window.icons());
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      }
    },
    async copyTail() {
      try {
        await navigator.clipboard.writeText(this.tailText);
        this.copied = true;
        setTimeout(() => this.copied = false, 2000);
      } catch (e) {
        window.toast(window._t('js_error_clipboard'), e.message, 'error');
      }
    }
  };
}

window.artSourcesPanel = artSourcesPanel
window.autofillStatus = autofillStatus
window.bulkData = bulkData
window.debugLogPanel = debugLogPanel
window.settingsShell = settingsShell
