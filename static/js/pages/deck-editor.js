const ROW_CHUNK = 30;

function deckEditor(deckId) {
  return {
    deckId,
    rowLimit: ROW_CHUNK,
    deck: null,
    cards: [],
    estimate: null,
    filter: '',
    selectedCardId: null,
    allArts: [],
    loadingPrints: false,
    building: false,
    rescanning: false,

    buildProgress: null,
    _buildPollTimer: null,

    cardstock: '(S30) Standard Smooth',
    foil: false,

    buildingPdf: false,

    exportingDecklist: false,
    decklistFormat: 'with_set',
    decklistHeaders: true,

    localizing: false,
    localizeLang: 'es',
    supportedLangs: {en: 'English', es: 'Español'},

    editingName: false,
    editNameValue: '',

    showAddCard: false,
    newCardName: '',
    newCardQty: 1,
    newCardRole: 'mainboard',
    addingCard: false,

    autoResults: [],
    autoOpen: false,
    autoIndex: -1,
    autoAbort: null,

    sortMode: localStorage.getItem('deckPref_sortMode') || 'name',

    groupMode: localStorage.getItem('deckPref_groupMode') || 'type',

    layoutMode: localStorage.getItem('deckPref_layoutMode') || 'list',

    showAddUrlForm: false,
    urlInput: '',
    urlFace: 'front',
    urlVariant: '',
    addingUrl: false,

    activeArtFilters: {full: false, textless: false, borderless: false, promo: false},

    expandedGroups: {commander: true, mainboard: true},

    backModal: null,

    notesOpen: false,
    notesDraft: '',
    notesSaving: false,
    notesSavedAt: 0,

    artPickerOpen: false,
    pickerCard: null,
    pickerLoading: false,
    pickerAllArts: [],
    pickerVisibleCount: 60,
    PICKER_PAGE_SIZE: 60,
    DRIVE_PAGE_SIZE: 250,
    pickerStreaming: false,
    pickerFacets: {},
    _pickerAbort: null,
    pickerAddUrl: '',
    uploadDropCardId: null,
    pickerDragOver: false,
    pickerFilters: {
      q: '',
      source: 'all',
      set: '',
      artTypes: [],
      rarities: [],
      sort: 'recent',
      driveTagsInclude: [],
      driveTagsExclude: [],
      driveExpansionCode: '',
      dedupSimilar: false,
    },
    _driveFiltersDebounce: null,
    _pickerCache: {},
    driveSearchState: {loading: false, error: null, hits: null, total: null, capped: false},

    preload: {
      total: 0,
      done: 0,
      inProgress: false,
      pollTimer: null,
    },

    tokensOpen: false,
    tokensLoading: false,
    tokensData: {tokens: [], total_unique: 0, already_in_deck: 0, missing: 0},
    tokensAdding: false,

    printRunsOpen: false,
    printRunsLoading: false,
    printRunsBuilding: false,
    printRunsMode: 'greedy',
    printRunsMaxTier: '',
    printRunsData: null,

    similarOpen: false,
    similarLoading: false,
    similarSourceArt: null,
    similarData: null,

    artistApplyOpen: false,
    artistApplyLoading: false,
    artistApplyApplying: false,
    artistApplySource: null,
    artistApplyArtist: '',
    artistApplyData: null,
    artistApplyChecked: {},

    statsOpen: false,
    statsReady: false,
    stats: {
      totalCards: 0,
      uniqueCards: 0,
      landCount: 0, basicLandCount: 0, nonBasicLandCount: 0,
      nonLandCount: 0, landPct: 0, avgCmc: null,
      curve: {columns: [], max: 0, avgPos: null, gridlines: []},
      colors: {total: 0, segments: []},
      rarity: {total: 0, segments: []},
      types: [],
      keywords: [],
      roles: [],
    },
    statsHover: {chart: null, key: null},

    addingRelated: null,

    lastXmlFilename: null,
    launchingAutofill: false,
    autofillStatus: {available: false, exe_path: null, source: 'not_found', hint: null},

    artSources: [],

    driveHits: [],
    driveSearching: false,
    driveIndexStats: {total_files: 0, sources_indexed: 0},
    addingFromDrive: null,

    get selectedCard() { return this.cards.find(c => c.id === this.selectedCardId); },
    get filteredCards() {
      let arr = this.cards;
      if (this.filter) {
        const q = this.filter.toLowerCase();
        arr = arr.filter(c =>
          c.name.toLowerCase().includes(q) ||
          (c.type_line || '').toLowerCase().includes(q) ||
          (c.colors || []).join('').toLowerCase().includes(q)
        );
      }
      return arr;
    },
    _sortCards(cards) {
      const clone = [...cards];
      switch (this.sortMode) {
        case 'cmc':
          clone.sort((a, b) => (a.cmc || 0) - (b.cmc || 0) || a.name.localeCompare(b.name));
          break;
        case 'type':
          clone.sort((a, b) => {
            const ta = _mainType(a.type_line || '');
            const tb = _mainType(b.type_line || '');
            return ta.localeCompare(tb) || a.name.localeCompare(b.name);
          });
          break;
        case 'color': {
          const orderMap = {W: 1, U: 2, B: 3, R: 4, G: 5};
          clone.sort((a, b) => {
            const ca = (a.colors || []).map(c => orderMap[c] || 99).sort().join('');
            const cb = (b.colors || []).map(c => orderMap[c] || 99).sort().join('');
            return (ca || 'z').localeCompare(cb || 'z') || a.name.localeCompare(b.name);
          });
          break;
        }
        default:
          clone.sort((a, b) => a.name.localeCompare(b.name));
      }
      return clone;
    },
    get cardGroups() {
      const config = [
        {role: 'commander', label: window._t('deck_role_commander'), dotClass: 'bg-accent'},
        {role: 'mainboard', label: window._t('deck_role_mainboard'), dotClass: 'bg-blue-400'},
        {role: 'companion', label: window._t('deck_role_companion'), dotClass: 'bg-purple-400'},
        {role: 'sideboard', label: window._t('deck_role_sideboard'), dotClass: 'bg-fg-muted'},
        {role: 'tokens', label: window._t('deck_role_tokens'), dotClass: 'bg-emerald-400'},
        {role: 'maybeboard', label: window._t('deck_role_maybeboard'), dotClass: 'bg-fg-faint'},
      ];
      const source = this.filteredCards;
      const out = [];
      for (const cfg of config) {
        const inGroup = source.filter(c => c.role === cfg.role);
        if (inGroup.length === 0) continue;
        out.push({
          role: cfg.role,
          label: cfg.label,
          dotClass: cfg.dotClass,
          cards: this._sortCards(inGroup),
          total: inGroup.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
        });
      }
      const known = new Set(config.map(c => c.role));
      const rest = source.filter(c => !known.has(c.role));
      if (rest.length > 0) {
        const byRole = {};
        rest.forEach(c => { (byRole[c.role] = byRole[c.role] || []).push(c); });
        for (const [role, cs] of Object.entries(byRole)) {
          out.push({
            role, label: role, dotClass: 'bg-fg-faint',
            cards: this._sortCards(cs),
            total: cs.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
          });
        }
      }
      return out;
    },

    get cardGroupsByType() {
      const source = this.filteredCards;
      const typeConfig = [
        {type: 'Creature',     label: window._t('stats_type_Creature'), dotClass: 'bg-emerald-400'},
        {type: 'Planeswalker', label: window._t('stats_type_Planeswalker'), dotClass: 'bg-purple-400'},
        {type: 'Sorcery',      label: window._t('stats_type_Sorcery'), dotClass: 'bg-rose-400'},
        {type: 'Instant',      label: window._t('stats_type_Instant'), dotClass: 'bg-sky-400'},
        {type: 'Enchantment',  label: window._t('stats_type_Enchantment'), dotClass: 'bg-amber-300'},
        {type: 'Artifact',     label: window._t('stats_type_Artifact'), dotClass: 'bg-slate-300'},
        {type: 'Battle',       label: window._t('stats_type_Battle'), dotClass: 'bg-red-500'},
        {type: 'Land',         label: window._t('stats_type_Land'), dotClass: 'bg-lime-500'},
      ];

      const mainboardCards = source.filter(c => c.role === 'mainboard');
      const otherRoles = source.filter(c => c.role !== 'mainboard');

      const out = [];

      const commanders = otherRoles.filter(c => c.role === 'commander');
      if (commanders.length > 0) {
        out.push({
          role: 'commander',
          label: window._t('deck_role_commander'),
          dotClass: 'bg-accent',
          cards: this._sortCards(commanders),
          total: commanders.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
        });
      }

      for (const cfg of typeConfig) {
        const inType = mainboardCards.filter(c => {
          const t = (c.type_line || '').split('—')[0];
          const re = new RegExp('\\b' + cfg.type + '\\b', 'i');
          if (!re.test(t)) return false;
          for (const prev of typeConfig) {
            if (prev.type === cfg.type) break;
            if (new RegExp('\\b' + prev.type + '\\b', 'i').test(t)) return false;
          }
          return true;
        });
        if (inType.length === 0) continue;
        out.push({
          role: 'type:' + cfg.type,
          label: cfg.label,
          dotClass: cfg.dotClass,
          cards: this._sortCards(inType),
          total: inType.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
        });
      }

      const uncategorized = mainboardCards.filter(c => {
        const t = (c.type_line || '').split('—')[0];
        return !typeConfig.some(cfg => new RegExp('\\b' + cfg.type + '\\b', 'i').test(t));
      });
      if (uncategorized.length > 0) {
        out.push({
          role: 'type:Other',
          label: window._t('stats_type_Other'),
          dotClass: 'bg-fg-faint',
          cards: this._sortCards(uncategorized),
          total: uncategorized.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
        });
      }

      const restConfig = [
        {role: 'companion',  label: window._t('deck_role_companion'),  dotClass: 'bg-purple-400'},
        {role: 'sideboard',  label: window._t('deck_role_sideboard'),  dotClass: 'bg-fg-muted'},
        {role: 'tokens',     label: window._t('deck_role_tokens'),     dotClass: 'bg-emerald-400'},
        {role: 'maybeboard', label: window._t('deck_role_maybeboard'), dotClass: 'bg-fg-faint'},
      ];
      for (const cfg of restConfig) {
        const inGroup = otherRoles.filter(c => c.role === cfg.role);
        if (inGroup.length === 0) continue;
        out.push({
          role: cfg.role,
          label: cfg.label,
          dotClass: cfg.dotClass,
          cards: this._sortCards(inGroup),
          total: inGroup.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
        });
      }
      return out;
    },

    get displayGroups() {
      return this.groupMode === 'type' ? this.cardGroupsByType : this.cardGroups;
    },
    visibleRows(group) {
      if (this.rowLimit === Infinity) return group.cards;
      let before = 0;
      for (const g of this.displayGroups) {
        if (g.role === group.role) break;
        before += g.cards.length;
      }
      const room = Math.max(0, this.rowLimit - before);
      return group.cards.length > room ? group.cards.slice(0, room) : group.cards;
    },
    _growRows() {
      if (this.rowLimit >= this.cards.length) {
        this.rowLimit = Infinity;
        return;
      }
      requestAnimationFrame(() => setTimeout(() => {
        this.rowLimit += ROW_CHUNK;
        this._growRows();
      }));
    },
    get totalCards() { return this.cards.reduce((n, c) => n + (c.include ? c.quantity : 0), 0); },
    get uniqueCards() { return this.cards.filter(c => c.include).length; },
    get customArts() { return this.allArts.filter(a => a.kind === 'custom'); },
    get officialArts() { return this.allArts.filter(a => a.kind === 'scryfall'); },
    get filteredOfficial() {
      let arr = this.officialArts;
      const f = this.activeArtFilters;
      if (f.full) arr = arr.filter(p => p.full_art);
      if (f.textless) arr = arr.filter(p => p.textless);
      if (f.borderless) arr = arr.filter(p => p.border_color === 'borderless');
      if (f.promo) arr = arr.filter(p => p.promo);
      return arr;
    },

    async load() {
      const r = await fetch(`/api/decks/${this.deckId}`);
      const d = await r.json();
      this.deck = d;
      this.cards = d.cards;
      this.notesDraft = d.notes || '';
      this.notesOpen = !!this.notesDraft;
      this._growRows();
      this.loadEstimate();

      this.$watch('sortMode',   v => localStorage.setItem('deckPref_sortMode', v));
      this.$watch('groupMode',  v => localStorage.setItem('deckPref_groupMode', v));
      this.$watch('layoutMode', v => localStorage.setItem('deckPref_layoutMode', v));

      if (!this._settingsLoaded) {
        try {
          const s = await fetch('/api/settings/');
          if (s.ok) {
            const data = await s.json();
            if (data.values.default_cardstock) this.cardstock = data.values.default_cardstock;
            if (typeof data.values.foil_default === 'boolean') this.foil = data.values.foil_default;
            if (data.values.prefer_full_art) this.activeArtFilters.full = true;
            if (data.values.prefer_borderless) this.activeArtFilters.borderless = true;
            if (data.values.preferred_language) this.localizeLang = data.values.preferred_language;
          }
        } catch (e) {}
        try {
          const lr = await fetch('/api/decks/_/supported-langs');
          if (lr.ok) this.supportedLangs = await lr.json();
        } catch (e) {}
        try {
          const st = await fetch('/api/mpc-autofill/status');
          if (st.ok) this.autofillStatus = await st.json();
        } catch (e) {}
        try {
          const as = await fetch('/api/art-sources/');
          if (as.ok) this.artSources = await as.json();
        } catch (e) {}
        try {
          const ds = await fetch('/api/drives/stats');
          if (ds.ok) this.driveIndexStats = await ds.json();
        } catch (e) {}
        this._settingsLoaded = true;
      }
      if (!this._preloadStarted) {
        this._preloadStarted = true;
        this._startPreload();
      }
    },
    async refreshCards() {
      try {
        const d = await window.api.decks.get(this.deckId);
        this.deck = d;
        this.cards = d.cards;
        this._pickerCache = {};
        this._growRows();
        this.loadEstimate();
        if (this.selectedCard) await this.selectCard(this.selectedCard);
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      }
    },
    async loadEstimate() {
      const r = await fetch(`/api/decks/${this.deckId}/estimate`);
      this.estimate = await r.json();
    },

    async selectCard(card) {
      this.selectedCardId = card.id;
      this.loadingPrints = true;
      this.allArts = [];
      this.showAddUrlForm = false;
      this.driveHits = [];
      try {
        this.allArts = await window.api.cards.allPrints(this.deckId, card.id);
      } catch (e) {
        window.toast(window._t('deck_error_load_arts'), e.message, 'error');
      } finally {
        this.loadingPrints = false;
      }
      if (this.driveIndexStats.total_files > 0) {
        this.searchDrives(card.name);
      }
    },

    async openArtPicker(card) {
      this.pickerCard = card;
      this.selectedCardId = card.id;
      this.artPickerOpen = true;
      this.pickerVisibleCount = 60;
      this.driveHits = [];
      this.driveSearchState = {loading: false, error: null, hits: null, total: null, capped: false};

      try {
        const ds = await fetch('/api/drives/stats');
        if (ds.ok) this.driveIndexStats = await ds.json();
      } catch (e) {}

      const cached = this._pickerCache[card.id];
      if (cached) {
        this.pickerAllArts = cached;
        this.allArts = cached.filter(a => a.kind !== 'drive');
        this.pickerLoading = false;
        this.$nextTick(() => window.icons?.());
        if (this.driveIndexStats.total_files > 0) {
          this._loadDrivesForPicker(card.name);
        }
        return;
      }

      this.pickerLoading = true;
      this.pickerAllArts = [];
      this._pickerAbort?.abort();
      this._pickerAbort = new AbortController();
      const signal = this._pickerAbort.signal;

      try {
        const page = await this._fetchPrintsPage(card.id, 0, signal);
        const first = [...page.custom, ...page.items];
        this.allArts = first;
        this.pickerAllArts = first.map((a, i) => this._preprocessArt(a, i, 'local'));
        this.pickerFacets = page.facets || {};
        this.pickerLoading = false;
        this.$nextTick(() => window.icons?.());

        if (page.has_more) {
          this._streamRemainingPrints(card.id, page, signal);
        } else {
          this._pickerCache[card.id] = this.pickerAllArts;
        }
      } catch (e) {
        if (e.name !== 'AbortError') {
          window.toast(window._t('common_error'), e.detail || e.message, 'error');
        }
        this.pickerLoading = false;
      }
      if (this.driveIndexStats.total_files > 0) {
        this._loadDrivesForPicker(card.name);
      }
      this.$nextTick(() => window.icons?.());
    },

    async _fetchPrintsPage(cardId, offset, signal) {
      return window.api.cards.prints(this.deckId, cardId, {
        offset,
        limit: this.PICKER_PAGE_SIZE,
        sort: 'released_desc',
        signal,
      });
    },

    async _streamRemainingPrints(cardId, firstPage, signal) {
      this.pickerStreaming = true;
      let offset = firstPage.offset + firstPage.limit;
      const total = firstPage.total;
      try {
        while (offset < total && !signal.aborted) {
          const page = await this._fetchPrintsPage(cardId, offset, signal);
          if (signal.aborted) return;
          const start = this.pickerAllArts.length;
          const mapped = page.items.map((a, i) =>
            this._preprocessArt(a, start + i, 'local')
          );
          this.pickerAllArts.push(...mapped);
          this.allArts.push(...page.items);
          offset += page.limit;
          if (!page.has_more) break;
        }
        if (!signal.aborted) {
          this._pickerCache[cardId] = this.pickerAllArts;
        }
      } catch (e) {
        if (e.name !== 'AbortError') {
          console.warn('Streaming de impresiones interrumpido:', e);
        }
      } finally {
        this.pickerStreaming = false;
      }
    },

    async _loadDrivesForPicker(cardName) {
      this._driveAbort?.abort();
      const ctrl = new AbortController();
      this._driveAbort = ctrl;
      const cardId = this.pickerCard ? this.pickerCard.id : null;

      this.driveSearchState = {loading: true, error: null, hits: null, total: null, capped: false};
      const params = new URLSearchParams({q: cardName, limit: String(this.DRIVE_PAGE_SIZE)});
      const inc = this.pickerFilters.driveTagsInclude || [];
      const exc = this.pickerFilters.driveTagsExclude || [];
      if (inc.length) params.set('tags_include', inc.join(','));
      if (exc.length) params.set('tags_exclude', exc.join(','));
      const expCode = (this.pickerFilters.driveExpansionCode || '').trim().toLowerCase();
      if (expCode) params.set('expansion_code', expCode);

      let hits = [];
      let total = null;
      let capped = false;
      try {
        while (true) {
          params.set('offset', String(hits.length));
          const r = await fetch(`/api/drives/search?${params.toString()}`, {signal: ctrl.signal});
          if (ctrl.signal.aborted) return;
          if (!r.ok) {
            if (!hits.length) {
              this.driveSearchState = {loading: false, error: `HTTP ${r.status}`, hits: 0, total: 0, capped: false};
              return;
            }
            this.driveSearchState = {...this.driveSearchState, loading: false, error: `HTTP ${r.status}`};
            return;
          }
          const page = await r.json();
          if (ctrl.signal.aborted) return;
          if (total === null) {
            const header = parseInt(r.headers.get('X-Total-Count') || '', 10);
            total = Number.isFinite(header) ? header : page.length;
            capped = r.headers.get('X-Total-Capped') === '1';
          }
          hits = hits.concat(page);
          const done = page.length < this.DRIVE_PAGE_SIZE || hits.length >= total;
          this._applyDriveHits(hits, {total, capped, loading: !done});
          if (done) break;
        }
        if (this.pickerCard && this.pickerCard.id === cardId) {
          this._pickerCache[cardId] = this.pickerAllArts;
        }
      } catch (e) {
        if (e.name === 'AbortError') return;
        this.driveSearchState = {
          ...this.driveSearchState, loading: false, error: e.message || 'Error',
          hits: this.driveSearchState.hits || 0,
        };
      }
    },

    _applyDriveHits(hits, {total, capped, loading}) {
      this.driveHits = hits;
      this.driveSearchState = {loading, error: null, hits: hits.length, total, capped};
      const driveArts = this._driveHitsToArts(hits);
      const nonDrive = this.pickerAllArts.filter(a => a.__source !== 'drives');
      this.pickerAllArts = [...nonDrive, ...driveArts];
    },

    _driveHitsToArts(hits) {
      return hits.map((h, i) => this._preprocessArt({
        kind: 'drive',
        image_small: h.thumb_url,
        download_url: h.download_url,
        filename: h.filename,
        set_name: h.source_name,
        collector_number: h.folder_path || '',
        artist: null,
        released_at: null,
        rarity: '',
        full_art:   !!h.is_full_art,
        textless:   !!h.is_textless,
        promo:      !!h.is_promo,
        border_color: h.is_borderless ? 'borderless' : '',
        frame:      h.is_retro ? '1997' : '',
        is_extended:  !!h.is_extended,
        is_showcase:  !!h.is_showcase,
        is_alt_art:   !!h.is_alt_art,
        drive_tags:   h.tags || [],
        canonical_set: h.expansion_code || null,
        canonical_num: h.collector_number || null,
        image_hash: h.image_hash || null,
        face: 'front',
        score: h.score,
        _drive_hit: h,
      }, i, 'drives'));
    },

    onDriveFiltersChanged() {
      if (!this.pickerCard) return;
      clearTimeout(this._driveFiltersDebounce);
      this._driveFiltersDebounce = setTimeout(() => {
        if (this.pickerCard && this._pickerCache[this.pickerCard.id]) {
          this._pickerCache[this.pickerCard.id] =
            this._pickerCache[this.pickerCard.id].filter(a => a.__source !== 'drives');
        }
        this._loadDrivesForPicker(this.pickerCard.name);
      }, 250);
    },

    async _startPreload() {
      try {
        const r = await fetch(`/api/decks/${this.deckId}/preload-prints`, {method: 'POST'});
        if (!r.ok) return;
        const st = await r.json();
        this.preload.total = st.total || 0;
        this.preload.done = st.done || 0;
        this.preload.inProgress = st.in_progress;
        if (this.preload.total === 0) return;
        this._pollPreload();
      } catch (e) {}
    },

    _pollPreload() {
      if (this.preload.pollTimer) return;
      this.preload.pollTimer = setInterval(async () => {
        try {
          const r = await fetch(`/api/decks/${this.deckId}/preload-progress`);
          if (!r.ok) return;
          const st = await r.json();
          this.preload.total = st.total || 0;
          this.preload.done = st.done || 0;
          this.preload.inProgress = st.in_progress;
          if (!st.in_progress) {
            clearInterval(this.preload.pollTimer);
            this.preload.pollTimer = null;
          }
        } catch (e) {}
      }, 2000);
    },

    _preprocessArt(art, idx, kindHint) {
      art.__thumb = art.thumb_url || art.image_small;
      const source =
        art.kind === 'custom' ? 'custom' :
        art.kind === 'drive'  ? 'drives' :
        'scryfall';

      let label, sublabel, tooltip;
      if (source === 'custom') {
        label = art.variant_label || 'custom';
        sublabel = art.filename || '';
        tooltip = art.filename || window._t('deck_badge_custom');
      } else if (source === 'drives') {
        if (art.canonical_set) {
          label = art.canonical_set.toUpperCase()
                + (art.canonical_num ? ' · #' + art.canonical_num : '');
          sublabel = art.set_name || art.filename || '';
          tooltip = `${art.canonical_set.toUpperCase()} #${art.canonical_num || '?'} · `
                  + `${art.set_name} · ${art.filename}`;
        } else {
          label = art.set_name || 'Drive';
          sublabel = art.filename || '';
          tooltip = `${art.set_name} · ${art.filename}`;
        }
      } else {
        label = (art.set_code || '?').toUpperCase() + ' · ' + (art.collector_number || '?');
        const year = art.released_at ? art.released_at.slice(0, 4) : '';
        sublabel = art.artist ? `${art.artist}${year ? ' · ' + year : ''}` : year;
        tooltip = `${art.set_name || art.set_code} #${art.collector_number} · ${art.artist || window._t('js_artist_unknown')}${year ? ' (' + year + ')' : ''}`;
      }

      const badges = [];
      if (source === 'scryfall') {
        if (art.border_color === 'borderless') badges.push('BRDLESS');
        if (art.full_art)                      badges.push('FULL');
        if (art.textless)                      badges.push('TEXT-');
        if (art.frame === '1997')              badges.push('RETRO');
        if (art.promo)                         badges.push('PROMO');
      } else if (source === 'drives') {
        if (art.border_color === 'borderless') badges.push('BRDLESS');
        if (art.full_art)                      badges.push('FULL');
        if (art.textless)                      badges.push('TEXT-');
        if (art.frame === '1997')              badges.push('RETRO');
        if (art.promo)                         badges.push('PROMO');
        if (art.is_extended)                   badges.push('EXT');
        if (art.is_showcase)                   badges.push('SHOW');
        if (art.is_alt_art)                    badges.push('ALT');
      }

      return {
        ...art,
        __key: `${source}-${art.scryfall_id || art.custom_art_id || art.file_id || idx}`,
        __source: source,
        __label: label,
        __sublabel: sublabel,
        __tooltip: tooltip,
        __badges: badges,
        __year: art.released_at ? parseInt(art.released_at.slice(0, 4), 10) : 0,
      };
    },

    closeArtPicker() {
      this._pickerAbort?.abort();
      this._pickerAbort = null;
      this._driveAbort?.abort();
      this._driveAbort = null;
      this.pickerStreaming = false;
      this.artPickerOpen = false;
      this.pickerCard = null;
      this.pickerAllArts = [];
      this.pickerVisibleCount = 60;
    },

    resetPickerFilters() {
      const hadDriveFilters =
        (this.pickerFilters.driveTagsInclude || []).length > 0 ||
        (this.pickerFilters.driveTagsExclude || []).length > 0 ||
        (this.pickerFilters.driveExpansionCode || '').length > 0;
      this.pickerFilters = {
        q: '', source: 'all', set: '', artTypes: [], rarities: [], sort: 'recent',
        driveTagsInclude: [], driveTagsExclude: [], driveExpansionCode: '',
        dedupSimilar: false,
      };
      this.pickerVisibleCount = 60;
      if (hadDriveFilters && this.pickerCard) {
        this.onDriveFiltersChanged();
      }
    },

    get pickerFilteredArts() {
      let arts = this.pickerAllArts;

      if (this.pickerFilters.source !== 'all') {
        arts = arts.filter(a => a.__source === this.pickerFilters.source);
      }

      const q = this.pickerFilters.q.trim().toLowerCase();
      if (q) {
        arts = arts.filter(a => {
          const hay = [
            a.set_code, a.set_name, a.collector_number,
            a.artist, a.filename, a.variant_label,
          ].filter(Boolean).join(' ').toLowerCase();
          return hay.includes(q);
        });
      }

      if (this.pickerFilters.set) {
        arts = arts.filter(a => a.__source !== 'scryfall' || a.set_code === this.pickerFilters.set);
      }

      if (this.pickerFilters.artTypes.length > 0) {
        arts = arts.filter(a => {
          if (a.__source !== 'scryfall') return false;
          return this.pickerFilters.artTypes.some(t => {
            if (t === 'borderless') return a.border_color === 'borderless';
            if (t === 'full_art')   return a.full_art;
            if (t === 'textless')   return a.textless;
            if (t === 'retro')      return a.frame === '1997';
            if (t === 'promo')      return a.promo;
            return false;
          });
        });
      }

      if (this.pickerFilters.rarities.length > 0) {
        arts = arts.filter(a => {
          if (a.__source !== 'scryfall') return false;
          return this.pickerFilters.rarities.includes(a.rarity);
        });
      }

      const sorted = [...arts];
      const cmp = {
        recent:  (a, b) => (b.__year || 0) - (a.__year || 0),
        old:     (a, b) => (a.__year || 9999) - (b.__year || 9999),
        set:     (a, b) => (a.set_code || 'zzz').localeCompare(b.set_code || 'zzz'),
        artist:  (a, b) => (a.artist || 'zzz').localeCompare(b.artist || 'zzz'),
      }[this.pickerFilters.sort] || ((a, b) => 0);
      sorted.sort((a, b) => {
        const rank = { custom: 0, drives: 1, scryfall: 2 };
        const dr = (rank[a.__source] ?? 3) - (rank[b.__source] ?? 3);
        if (dr !== 0) return dr;
        if (a.is_chosen !== b.is_chosen) return a.is_chosen ? -1 : 1;
        return cmp(a, b);
      });

      if (this.pickerFilters.dedupSimilar) {
        const seen = new Map();
        const deduped = [];
        for (const a of sorted) {
          const h = a.image_hash;
          if (a.__source !== 'drives' || !h) {
            deduped.push(a);
            continue;
          }
          if (seen.has(h)) {
            const winner = deduped[seen.get(h)];
            winner.__dup_count = (winner.__dup_count || 0) + 1;
            continue;
          }
          seen.set(h, deduped.length);
          deduped.push(a);
        }
        return deduped;
      }

      return sorted;
    },

    get pickerVisibleArts() {
      return this.pickerFilteredArts.slice(0, this.pickerVisibleCount);
    },

    get pickerFilteredCount() { return this.pickerFilteredArts.length; },
    get pickerTotalCount()    { return this.pickerAllArts.length; },

    get coverUrl() {
      const id = this.deck && this.deck.cover_card_id;
      if (!id) return null;
      const card = (this.cards || []).find(c => c.id === id);
      return (card && card.thumbnail_url) || null;
    },

    get pickerCounts() {
      const c = { all: this.pickerAllArts.length, custom: 0, drives: 0, scryfall: 0 };
      for (const a of this.pickerAllArts) c[a.__source] = (c[a.__source] || 0) + 1;
      return c;
    },

    get pickerAvailableSets() {
      const seen = new Map();
      for (const a of this.pickerAllArts) {
        if (a.__source === 'scryfall' && a.set_code && !seen.has(a.set_code)) {
          seen.set(a.set_code, { code: a.set_code, name: a.set_name || a.set_code });
        }
      }
      return [...seen.values()].sort((a, b) => a.code.localeCompare(b.code));
    },

    onPickerScroll(e) {
      const el = e.target;
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 300) {
        if (this.pickerVisibleCount < this.pickerFilteredArts.length) {
          this.pickerVisibleCount = Math.min(this.pickerVisibleCount + 60, this.pickerFilteredArts.length);
        }
      }
    },

    async pickArt(art, remember) {
      if (art.__source === 'custom') {
        await this.chooseCustom({custom_art_id: art.custom_art_id, face: art.face});
        window.toast(window._t('deck_art_updated_toast'), art.filename || 'Custom art', 'success');
        this.closeArtPicker();
      } else if (art.__source === 'drives') {
        try {
          const r = await fetch('/api/custom-art/from-url', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              url: art.download_url,
              card_name: this.pickerCard.name,
              face: 'front',
              variant: art.set_name,
            })
          });
          if (!r.ok) throw new Error(await window.apiErrorDetail(r));
          const data = await r.json();
          await this.chooseCustom({custom_art_id: data.id, face: 'front'});
          window.toast(`${window._T.deck_added_from} ${art.set_name}`, art.filename, 'success');
          this.closeArtPicker();
        } catch (e) {
          window.toast(window._t('deck_error_adding_art'), e.message, 'error');
        }
      } else {
        await this.chooseArt(art.scryfall_id, remember);
        window.toast(
          remember ? window._t('deck_art_remembered') : window._t('deck_art_updated_toast'),
          `${art.set_code?.toUpperCase() || ''} #${art.collector_number || ''}`,
          'success',
        );
        this.closeArtPicker();
      }
    },

    uploadFaces(card) {
      const faces = [{value: 'front', label: `${window._t('art_upload_face_front')} — ${card.name}`}];
      if (card.is_dfc || card.back_name) {
        faces.push({value: 'back', label: `${window._t('art_upload_face_back')} — ${card.back_name || card.name}`});
      }
      return faces;
    },

    async uploadArtFor(card, file = null, face = 'front') {
      if (!card) return;
      this.selectedCardId = card.id;
      const result = await window.artUpload.open({
        cardName: card.name,
        face,
        faces: this.uploadFaces(card),
        currentImages: {front: card.thumbnail_url, back: card.back_thumbnail_url},
        deckId: this.deckId,
        deckCardId: card.id,
        file,
      });
      if (!result || !result.card) return;
      this._updateCard(result.card);
      delete this._pickerCache[card.id];
      if (this.artPickerOpen && this.pickerCard?.id === card.id) this.closeArtPicker();
      this.loadEstimate();
    },

    onRowDragOver(ev, card) {
      if (!window.artUpload.hasFile(ev.dataTransfer)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'copy';
      this.uploadDropCardId = card.id;
    },
    onRowDragLeave(ev, card) {
      if (ev.currentTarget.contains(ev.relatedTarget)) return;
      if (this.uploadDropCardId === card.id) this.uploadDropCardId = null;
    },
    onRowDrop(ev, card) {
      const file = window.artUpload.fileFrom(ev.dataTransfer);
      this.uploadDropCardId = null;
      if (!file) return;
      ev.preventDefault();
      this.uploadArtFor(card, file);
    },

    onPickerDragOver(ev) {
      if (!window.artUpload.hasFile(ev.dataTransfer)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'copy';
      this.pickerDragOver = true;
    },
    onPickerDragLeave(ev) {
      if (ev.currentTarget.contains(ev.relatedTarget)) return;
      this.pickerDragOver = false;
    },
    onPickerPaste(ev) {
      if (Alpine.store('artUpload').open || !this.pickerCard) return;
      const target = ev.target;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return;
      const file = window.artUpload.fileFrom(ev.clipboardData);
      if (!file) return;
      ev.preventDefault();
      this.uploadArtFor(this.pickerCard, file);
    },
    onPickerDrop(ev) {
      this.pickerDragOver = false;
      const file = window.artUpload.fileFrom(ev.dataTransfer);
      if (!file || !this.pickerCard) return;
      ev.preventDefault();
      this.uploadArtFor(this.pickerCard, file);
    },

    async addCustomFromUrl() {
      if (!this.pickerAddUrl.trim() || !this.pickerCard) return;
      this.addingUrl = true;
      try {
        const r = await fetch('/api/custom-art/from-url', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            url: this.pickerAddUrl.trim(),
            card_name: this.pickerCard.name,
            face: 'front',
          })
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const data = await r.json();
        window.toast(window._t('deck_custom_added'), data.filename || 'Custom art', 'success');
        this.pickerAddUrl = '';
        await this.openArtPicker(this.pickerCard);
      } catch (e) {
        window.toast(window._t('deck_error_download'), e.message, 'error');
      } finally {
        this.addingUrl = false;
      }
    },

    async searchDrives(query) {
      if (!query) return;
      this.driveSearching = true;
      try {
        const r = await fetch(`/api/drives/search?q=${encodeURIComponent(query)}&limit=20`);
        if (r.ok) this.driveHits = await r.json();
      } catch (e) {
        this.driveHits = [];
      } finally {
        this.driveSearching = false;
      }
    },

    async useDriveArt(hit, face) {
      if (!this.selectedCard) return;
      this.addingFromDrive = hit.file_id;
      try {
        const r = await fetch('/api/custom-art/from-url', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            url: hit.download_url,
            card_name: this.selectedCard.name,
            face: face,
            variant: hit.source_name,
          })
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const data = await r.json();
        await this.chooseCustom({custom_art_id: data.id, face: face});
        await this.selectCard(this.selectedCard);
        window.toast(
          `${window._T.deck_added_from} ${hit.source_name}`,
          `${hit.filename} — ${face}`,
          'success',
        );
      } catch (e) {
        window.toast(window._t('deck_error_adding_art'), e.message, 'error');
      } finally {
        this.addingFromDrive = null;
      }
    },
    toggleArtFilter(k) { this.activeArtFilters[k] = !this.activeArtFilters[k]; },

    isGroupExpanded(role) {
      const v = this.expandedGroups[role];
      if (v !== undefined) return v;
      if (role === 'commander' || role === 'mainboard') return true;
      if (role.startsWith('type:')) return true;
      return false;
    },
    toggleGroup(role) {
      this.expandedGroups[role] = !this.isGroupExpanded(role);
    },
    showBackFor(card) {
      if (!card.back_thumbnail_url) return;
      this.backModal = {
        name: card.back_name || (card.name + ' (reverso)'),
        url: card.back_thumbnail_url,
        cardName: card.name,
      };
    },
    async addRelated(card) {
      if (!card.related_parts || card.related_parts.length === 0) return;
      this.addingRelated = card.id;
      try {
        const r = await fetch(`/api/decks/${this.deckId}/cards/${card.id}/add-related`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const added = await r.json();
        if (added.length === 0) {
          window.toast(window._t('deck_no_related'), window._t('deck_related_already'), 'info');
        } else {
          const names = added.map(c => c.name).join(', ');
          window.toast(window._t('deck_related_added').replace('{n}', added.length), names, 'success');
          this.expandedGroups.tokens = true;
        }
        await this.load();
      } catch (e) {
        window.toast(window._t('deck_error_adding'), e.message, 'error');
      } finally {
        this.addingRelated = null;
      }
    },
    async chooseArt(scryfall_id, remember) {
      const card = this.selectedCard;
      if (!card) return;
      const r = await fetch(`/api/decks/${this.deckId}/cards/change-art`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({deck_card_id: card.id, scryfall_id, face: 'front', remember_globally: remember})
      });
      if (!r.ok) { window.toast(window._t('common_error'), window._t('deck_error_art'), 'error'); return; }
      const updated = await r.json();
      this._updateCard(updated);
      this.allArts = this.allArts.map(a => ({...a, is_chosen: a.kind === 'scryfall' && a.scryfall_id === scryfall_id}));
      delete this._pickerCache[card.id];
      if (remember) window.toast(window._t('deck_art_remembered'), card.name, 'success');
    },
    async chooseCustom(art) {
      const card = this.selectedCard;
      if (!card) return;
      const r = await fetch(`/api/decks/${this.deckId}/cards/change-art`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({deck_card_id: card.id, custom_art_id: art.custom_art_id, face: art.face})
      });
      if (!r.ok) { window.toast(window._t('common_error'), window._t('deck_error_custom_art'), 'error'); return; }
      const updated = await r.json();
      this._updateCard(updated);
      this.allArts = this.allArts.map(a => {
        if (a.kind === 'custom') return {...a, is_chosen: a.custom_art_id === art.custom_art_id && a.face === art.face};
        if (art.face === 'front') return {...a, is_chosen: false};
        return a;
      });
      delete this._pickerCache[card.id];
    },
    async addFromUrl() {
      const card = this.selectedCard;
      if (!card || !this.urlInput) return;
      this.addingUrl = true;
      try {
        const r = await fetch('/api/custom-art/from-url', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url: this.urlInput, card_name: card.name, face: this.urlFace, variant: this.urlVariant || null})
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        window.toast(window._t('deck_custom_added'), card.name, 'success');
        this.urlInput = ''; this.urlVariant = ''; this.showAddUrlForm = false;
        await this.selectCard(card);
        await this.load();
      } catch (e) {
        window.toast(window._t('deck_error_download'), e.message, 'error');
      } finally {
        this.addingUrl = false;
      }
    },
    async deleteCustom(art) {
      if (!await window.confirmDialog(
        window._t('deck_delete_art_title'),
        `"${art.variant_label || art.filename}"`,
        { danger: true, icon: 'trash-2', confirmLabel: window._t('common_delete') })) return;
      const r = await fetch(`/api/custom-art/${art.custom_art_id}`, {method: 'DELETE'});
      if (!r.ok) {
        window.toast(window._t('common_error'), await window.artUpload.errorDetail(r), 'error');
        return;
      }
      window.toast(window._t('deck_custom_deleted'), art.filename, 'success');
      const card = this.selectedCard;
      await this.selectCard(card);
      await this.load();
    },
    async rescanCustom() {
      this.rescanning = true;
      try {
        const r = await fetch('/api/custom-art/rescan', {method: 'POST'});
        const data = await r.json();
        window.toast(window._t('deck_rescanned'), `${data.total} ${window._T.deck_files_count} (+${data.added} / -${data.removed})`, 'success');
        if (this.selectedCard) await this.selectCard(this.selectedCard);
        await this.load();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.rescanning = false;
      }
    },

    async _refreshValidation() {
      try {
        const r = await fetch(`/api/decks/${this.deckId}/validation`);
        if (r.ok && this.deck) {
          this.deck.validation = await r.json();
        }
      } catch (e) {}
    },

    async changeQty(card, newQty) {
      if (newQty < 1) return;
      const r = await fetch(`/api/decks/${this.deckId}/cards/${card.id}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({quantity: newQty})
      });
      if (r.ok) {
        const updated = await r.json();
        this._updateCard(updated);
        this._refreshValidation();
        this.loadEstimate();
      }
    },
    async toggleInclude(card) {
      const r = await fetch(`/api/decks/${this.deckId}/cards/${card.id}/toggle`, {method: 'POST'});
      if (r.ok) {
        const updated = await r.json();
        this._updateCard(updated);
        this._refreshValidation();
        this.loadEstimate();
      }
    },
    async deleteCard(card) {
      if (!await window.confirmDialog(
        window._t('deck_card_delete'),
        `"${card.name}"`,
        { danger: true, icon: 'trash-2', confirmLabel: window._t('common_delete') })) return;
      const r = await fetch(`/api/decks/${this.deckId}/cards/${card.id}`, {method: 'DELETE'});
      if (r.ok) {
        window.toast(window._t('deck_card_deleted'), card.name, 'success');
        if (this.selectedCardId === card.id) { this.selectedCardId = null; this.allArts = []; }
        await this.load();
      } else {
        window.toast(window._t('common_error'), window._t('deck_error_delete'), 'error');
      }
    },

    async moveCardTo(card, newRole) {
      if (!newRole || card.role === newRole) return;
      const r = await fetch(`/api/decks/${this.deckId}/cards/${card.id}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({role: newRole})
      });
      if (r.ok) {
        this.expandedGroups[newRole] = true;
        const updated = await r.json();
        this._updateCard(updated);
        this._refreshValidation();
        window.toast(window._t('deck_kind_moved'), `${card.name} → ${this.roleLabel(newRole)}`, 'success');
      } else {
        window.toast(window._t('common_error'), window._t('deck_error_move'), 'error');
      }
    },

    roleLabel(role) {
      const g = this.cardGroups.find(g => g.role === role);
      return g ? g.label : role;
    },

    async clearRole(role, total) {
      if (!total) return;
      const label = this.roleLabel(role);
      if (!await window.confirmDialog(
        `${window._T.deck_group_clear} ${label}`,
        `${label}: ${total} ${window._T.history_cards_count}`,
        { danger: true, icon: 'trash-2', confirmLabel: window._t('deck_group_clear') })) return;
      const r = await fetch(`/api/decks/${this.deckId}/role/${role}`, {method: 'DELETE'});
      if (r.ok) {
        const data = await r.json();
        window.toast(`${label} ${window._T.deck_section_cleared}`, `${data.deleted} ${window._T.deck_cards_deleted}`, 'success');
        if (this.selectedCardId) { this.selectedCardId = null; this.allArts = []; }
        await this.load();
      } else {
        window.toast(window._t('common_error'), window._t('deck_error_clear'), 'error');
      }
    },
    async addCard() {
      if (!this.newCardName) return;
      this.addingCard = true;
      try {
        const r = await fetch(`/api/decks/${this.deckId}/cards`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name: this.newCardName.trim(),
            quantity: this.newCardQty,
            role: this.newCardRole,
          })
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        window.toast(window._t('history_kind_added'), this.newCardName, 'success');
        this.newCardName = '';
        this.autoResults = [];
        this.autoOpen = false;
        await this.load();
        this.$nextTick(() => this.$refs.newCardInput && this.$refs.newCardInput.focus());
      } catch (e) {
        window.toast(window._t('deck_error_adding'), e.message, 'error');
      } finally {
        this.addingCard = false;
      }
    },

    async autocomplete() {
      const q = (this.newCardName || '').trim();
      if (q.length < 2) { this.autoResults = []; this.autoOpen = false; return; }
      try {
        if (this.autoAbort) this.autoAbort.abort();
        this.autoAbort = new AbortController();
        const r = await fetch(`/api/decks/_/autocomplete?q=${encodeURIComponent(q)}`,
                              {signal: this.autoAbort.signal});
        if (!r.ok) return;
        this.autoResults = await r.json();
        this.autoIndex = this.autoResults.length ? 0 : -1;
        this.autoOpen = this.autoResults.length > 0;
      } catch (e) {
        if (e.name !== 'AbortError') console.error(e);
      }
    },
    autoMove(delta) {
      if (!this.autoOpen || !this.autoResults.length) return;
      this.autoIndex = (this.autoIndex + delta + this.autoResults.length) % this.autoResults.length;
    },
    autoAccept() {
      if (this.autoOpen && this.autoIndex >= 0 && this.autoResults[this.autoIndex]) {
        this.pickAutocomplete(this.autoResults[this.autoIndex]);
      } else {
        this.addCard();
      }
    },
    pickAutocomplete(name) {
      this.newCardName = name;
      this.autoResults = [];
      this.autoOpen = false;
      this.$nextTick(() => this.$refs.newCardInput && this.$refs.newCardInput.focus());
    },

    startRename() {
      this.editNameValue = this.deck ? this.deck.name : '';
      this.editingName = true;
      this.$nextTick(() => this.$refs.nameInput && this.$refs.nameInput.focus());
    },
    async saveRename() {
      if (!this.editingName) return;
      const v = this.editNameValue.trim();
      this.editingName = false;
      if (!v || v === this.deck.name) return;
      const r = await fetch(`/api/decks/${this.deckId}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: v})
      });
      if (r.ok) {
        this.deck = await r.json();
        document.title = v + ' · MPC Forge';
        window.toast(window._t('history_kind_renamed'), v, 'success');
      } else {
        window.toast(window._t('common_error'), window._t('js_error_rename'), 'error');
      }
    },
    async saveNotes() {
      const notes = this.notesDraft;
      if (!this.deck || notes === (this.deck.notes || '')) return;
      this.notesSaving = true;
      try {
        const updated = await window.api.decks.update(this.deckId, { notes });
        this.deck.notes = updated.notes;
        this.notesSavedAt = Date.now();
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error');
      } finally {
        this.notesSaving = false;
      }
    },
    async deleteDeck() {
      if (!await window.confirmDialog(
        window._t('deck_delete'),
        window._t('js_delete_deck_confirm').replace('{name}', this.deck.name),
        { danger: true, icon: 'trash-2', confirmLabel: window._t('common_delete') })) return;
      const r = await fetch(`/api/decks/${this.deckId}`, {method: 'DELETE'});
      if (r.ok) { window.location.href = '/decks'; }
      else window.toast(window._t('common_error'), window._t('deck_error_delete'), 'error');
    },

    async openTokens() {
      this.tokensOpen = true;
      this.tokensLoading = true;
      this.tokensData = {tokens: [], total_unique: 0, already_in_deck: 0, missing: 0};
      try {
        const r = await fetch(`/api/decks/${this.deckId}/tokens-analysis`);
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        this.tokensData = await r.json();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
        this.closeTokens();
      } finally {
        this.tokensLoading = false;
        this.$nextTick(() => window.icons?.());
      }
    },

    closeTokens() {
      this.tokensOpen = false;
    },

    async openPrintRuns() {
      this.printRunsOpen = true;
      this.printRunsData = null;
      await this.loadPrintRuns();
      this.$nextTick(() => window.icons?.());
    },

    async loadPrintRuns() {
      this.printRunsLoading = true;
      try {
        const params = new URLSearchParams();
        if (this.printRunsMode === 'optimized') {
          params.set('optimize', 'true');
        } else if (this.printRunsMaxTier) {
          params.set('max_tier', this.printRunsMaxTier);
        }
        const url = `/api/decks/${this.deckId}/print-runs/preview` +
                    (params.toString() ? '?' + params.toString() : '');
        const r = await fetch(url);
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        this.printRunsData = await r.json();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
        this.printRunsData = null;
      } finally {
        this.printRunsLoading = false;
        this.$nextTick(() => window.icons?.());
      }
    },

    async buildSplitXml() {
      if (this.printRunsBuilding || !this.printRunsData) return;
      this.printRunsBuilding = true;
      try {
        const payload = {
          cardstock: null,
          foil: false,
          max_tier: this.printRunsMode === 'greedy' && this.printRunsMaxTier
            ? parseInt(this.printRunsMaxTier, 10)
            : null,
          create_runs: true,
        };
        const r = await fetch(`/api/decks/${this.deckId}/build-split-xml`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const data = await r.json();
        window.toast(window._t('deck_split_xml_done').replace('{n}', data.total_runs),
                     window._t('deck_split_xml_done_desc').replace('{cards}', data.total_cards),
                     'success');
        this.closePrintRuns();
        if (typeof this.loadBuildHistory === 'function') {
          this.loadBuildHistory();
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.printRunsBuilding = false;
      }
    },

    closePrintRuns() {
      this.printRunsOpen = false;
      this.printRunsData = null;
    },

    async openSimilar(art) {
      if (!art.image_hash) {
        window.toast(window._t('deck_no_phash'), window._t('deck_no_phash_desc'), 'info');
        return;
      }
      this.similarOpen = true;
      this.similarSourceArt = art;
      this.similarLoading = true;
      this.similarData = null;
      try {
        const r = await fetch(`/api/drives/phash/similar/${encodeURIComponent(art.file_id)}?threshold=8&limit=50`);
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        this.similarData = await r.json();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.similarLoading = false;
        this.$nextTick(() => window.icons?.());
      }
    },

    closeSimilar() {
      this.similarOpen = false;
      this.similarData = null;
      this.similarSourceArt = null;
    },

    async openArtistApply(art) {
      if (!art.artist) {
        window.toast(window._t('deck_no_artist'), window._t('deck_no_artist_desc'), 'info');
        return;
      }
      this.artistApplyOpen = true;
      this.artistApplySource = art;
      this.artistApplyArtist = art.artist;
      this.artistApplyLoading = true;
      this.artistApplyData = null;
      this.artistApplyChecked = {};
      try {
        const r = await fetch(`/api/decks/${this.deckId}/recommend-by-artist`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({artist: art.artist, role: 'all'}),
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        this.artistApplyData = await r.json();
        for (const m of (this.artistApplyData.matched || [])) {
          this.artistApplyChecked[m.oracle_id] = true;
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.artistApplyLoading = false;
        this.$nextTick(() => window.icons?.());
      }
    },

    closeArtistApply() {
      this.artistApplyOpen = false;
      this.artistApplyData = null;
      this.artistApplySource = null;
    },

    artistApplySelectedCount() {
      if (!this.artistApplyData) return 0;
      return (this.artistApplyData.matched || [])
        .filter(m => this.artistApplyChecked[m.oracle_id]).length;
    },

    async applyArtistToSelected() {
      if (!this.artistApplyData || this.artistApplyApplying) return;
      const matched = (this.artistApplyData.matched || [])
        .filter(m => this.artistApplyChecked[m.oracle_id]);
      if (matched.length === 0) {
        window.toast(window._t('deck_artist_none_selected'), window._t('deck_artist_none_selected_desc'), 'info');
        return;
      }
      this.artistApplyApplying = true;
      let ok = 0, fail = 0;
      try {
        for (const m of matched) {
          try {
            const r = await fetch(`/api/decks/${this.deckId}/cards/change-art`, {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({
                oracle_id: m.oracle_id,
                scryfall_id: m.scryfall_id,
              }),
            });
            if (r.ok) ok++; else fail++;
          } catch { fail++; }
        }
        window.toast(window._t('deck_artist_applied').replace('{n}', ok),
                     fail > 0 ? window._t('deck_artist_failed').replace('{n}', fail) : '',
                     fail > 0 ? 'warning' : 'success');
        this.closeArtistApply();
        if (typeof this.reload === 'function') this.reload();
        else location.reload();
      } finally {
        this.artistApplyApplying = false;
      }
    },

    async addOneToken(token) {
      if (token.in_deck || this.tokensAdding) return;
      this.tokensAdding = true;
      try {
        const r = await fetch(`/api/decks/${this.deckId}/tokens-add-many`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({scryfall_ids: [token.scryfall_id]})
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const added = await r.json();
        if (added.length > 0) {
          window.toast(window._t('deck_token_added'), token.name, 'success');
          await this.load();
          await this.openTokens();
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.tokensAdding = false;
      }
    },

    async addAllMissingTokens() {
      const missing = this.tokensData.tokens.filter(t => !t.in_deck).map(t => t.scryfall_id);
      if (missing.length === 0 || this.tokensAdding) return;
      this.tokensAdding = true;
      try {
        const r = await fetch(`/api/decks/${this.deckId}/tokens-add-many`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({scryfall_ids: missing})
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const added = await r.json();
        window.toast(
          window._t('deck_tokens_added').replace('{n}', added.length),
          added.map(c => c.name).slice(0, 3).join(', ') + (added.length > 3 ? '…' : ''),
          added.length ? 'success' : 'info',
        );
        await this.load();
        await this.openTokens();
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.tokensAdding = false;
      }
    },

    async openStats() {
      this.computeStats();
      this.statsHover = {chart: null, key: null};
      this.statsReady = false;
      this.statsOpen = true;
      this.$nextTick(() => {
        window.icons?.();
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (this.statsOpen) this.statsReady = true;
        }));
      });
    },

    closeStats() {
      this.statsOpen = false;
      this.statsReady = false;
    },

    tf(key, params = {}) {
      let text = window._t(key);
      for (const [k, v] of Object.entries(params)) {
        text = text.split(`{${k}}`).join(String(v));
      }
      return text;
    },

    computeStats() {
      const relevantRoles = new Set(['commander', 'mainboard']);
      const cards = (this.cards || []).filter(c =>
        c.include && relevantRoles.has(c.role)
      );

      const CMC_BUCKETS = ['0', '1', '2', '3', '4', '5', '6', '7+'];
      const ROLE_ORDER = ['ramp', 'draw', 'removal', 'wipe', 'counter', 'tutor', 'protection', 'recursion'];
      const cmcBucket = (cmc) => cmc >= 7 ? '7+' : String(Math.floor(cmc || 0));

      const CURVE_COLORS = ['W', 'U', 'B', 'R', 'G', 'M', 'C'];
      const curveByColor = {};
      for (const col of CURVE_COLORS) {
        curveByColor[col] = Object.fromEntries(CMC_BUCKETS.map(b => [b, 0]));
      }

      let landCount = 0;
      let basicLandCount = 0;
      let nonBasicLandCount = 0;
      let totalCmcNonLand = 0;
      let nonLandCount = 0;

      const colors = { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 };
      const types = {};
      const rarity = {};
      const keywords = {};
      const roles = {};

      const MAIN_TYPES = [
        'Creature', 'Instant', 'Sorcery', 'Enchantment',
        'Artifact', 'Planeswalker', 'Battle', 'Land',
      ];

      for (const c of cards) {
        const qty = c.quantity || 1;
        const typeLine = c.type_line || '';
        const isLand = /\bLand\b/i.test(typeLine);
        const isBasic = /\bBasic\b/i.test(typeLine);

        if (isLand) {
          landCount += qty;
          if (isBasic) basicLandCount += qty;
          else nonBasicLandCount += qty;
        } else {
          const bucket = cmcBucket(c.cmc || 0);
          totalCmcNonLand += (c.cmc || 0) * qty;
          nonLandCount += qty;
          const ci = c.color_identity || [];
          const bucketColor = ci.length === 0 ? 'C' : (ci.length === 1 ? ci[0] : 'M');
          curveByColor[bucketColor][bucket] += qty;
        }

        if (!isLand && c.mana_cost) {
          const pips = this._extractPips(c.mana_cost);
          let hasColor = false;
          for (const p of pips) {
            if (colors[p] !== undefined && p !== 'C') {
              colors[p] += qty;
              hasColor = true;
            }
          }
          if (!hasColor) colors.C += qty;
        }

        let mainType = 'Other';
        for (const t of MAIN_TYPES) {
          if (new RegExp('\\b' + t + '\\b', 'i').test(typeLine)) {
            mainType = t; break;
          }
        }
        types[mainType] = (types[mainType] || 0) + qty;

        const r = c.rarity || 'unknown';
        rarity[r] = (rarity[r] || 0) + qty;

        for (const kw of (c.keywords || [])) {
          if (!kw) continue;
          keywords[kw] = (keywords[kw] || 0) + 1;
        }

        for (const role of (c.roles || [])) {
          roles[role] = (roles[role] || 0) + qty;
        }
      }

      const totalCards = cards.reduce((sum, c) => sum + (c.quantity || 1), 0);
      const avgCmc = nonLandCount > 0 ? totalCmcNonLand / nonLandCount : null;
      const landPct = totalCards > 0 ? Math.round(100 * landCount / totalCards) : 0;

      const columns = CMC_BUCKETS.map((bucket, i) => {
        const segments = CURVE_COLORS
          .map(key => ({key, count: curveByColor[key][bucket]}))
          .filter(sg => sg.count > 0);
        const total = segments.reduce((sum, sg) => sum + sg.count, 0);
        return {bucket, i, total, segments};
      });
      const curveMax = Math.max(0, ...columns.map(col => col.total));
      const step = curveMax <= 4 ? 1 : curveMax <= 10 ? 2 : curveMax <= 25 ? 5 : 10;
      const scaleMax = Math.max(step, Math.ceil(curveMax / step) * step);
      for (const col of columns) {
        col.pct = scaleMax ? (100 * col.total / scaleMax) : 0;
        for (const sg of col.segments) sg.pct = col.total ? (100 * sg.count / col.total) : 0;
      }
      const gridlines = [];
      for (let v = step; v <= scaleMax; v += step) gridlines.push({v, pct: 100 * v / scaleMax});
      const avgPos = avgCmc === null ? null
        : 100 * (Math.min(avgCmc, 7) + 0.5) / CMC_BUCKETS.length;

      const COLOR_ORDER = ['W', 'U', 'B', 'R', 'G', 'C'];
      const colorTotal = COLOR_ORDER.reduce((sum, k) => sum + colors[k], 0);
      let cursor = 0;
      const colorSegments = COLOR_ORDER
        .filter(k => colors[k] > 0)
        .map((key, i) => {
          const pct = 100 * colors[key] / colorTotal;
          const seg = {key, i, count: colors[key], pct, start: cursor,
                       len: Math.max(0.01, pct - (colorTotal && pct < 100 ? 0.8 : 0))};
          cursor += pct;
          return seg;
        });

      const RARITY_ORDER = ['common', 'uncommon', 'rare', 'mythic', 'special', 'bonus', 'unknown'];
      const rarityTotal = RARITY_ORDER.reduce((sum, k) => sum + (rarity[k] || 0), 0);
      const raritySegments = RARITY_ORDER
        .filter(k => rarity[k] > 0)
        .map((key, i) => ({key, i, count: rarity[key], pct: 100 * rarity[key] / rarityTotal}));

      const typeRows = Object.entries(types).sort((a, b) => b[1] - a[1]);
      const typeMax = typeRows.length ? typeRows[0][1] : 0;
      const typeList = typeRows.map(([key, count], i) => ({
        key, i, count,
        pct: typeMax ? 100 * count / typeMax : 0,
        share: totalCards ? Math.round(100 * count / totalCards) : 0,
      }));

      const kwRows = Object.entries(keywords).sort((a, b) => b[1] - a[1]).slice(0, 12);
      const kwMax = kwRows.length ? kwRows[0][1] : 0;
      const keywordList = kwRows.map(([name, count], i) => ({
        name, count, i, weight: kwMax ? count / kwMax : 0,
      }));

      const roleMax = Math.max(0, ...Object.values(roles));
      const roleList = ROLE_ORDER.filter(key => roles[key]).map((key, i) => ({
        key, i, count: roles[key], pct: roleMax ? 100 * roles[key] / roleMax : 0,
      }));

      this.stats = {
        totalCards, uniqueCards: cards.length,
        landCount, basicLandCount, nonBasicLandCount, nonLandCount, landPct, avgCmc,
        curve: {columns, max: curveMax, scaleMax, avgPos, gridlines},
        colors: {total: colorTotal, segments: colorSegments},
        rarity: {total: rarityTotal, segments: raritySegments},
        types: typeList,
        keywords: keywordList,
        roles: roleList,
      };
    },

    curveCaption() {
      const h = this.statsHover;
      if (h.chart !== 'curve') return window._t('stats_curve_hint');
      const col = this.stats.curve.columns.find(c => c.bucket === h.key);
      if (!col) return window._t('stats_curve_hint');
      const cards = col.total === 1 ? window._t('nav_card_one') : window._t('nav_cards');
      const head = this.tf('stats_curve_caption', {cmc: col.bucket, n: col.total, cards});
      if (!col.segments.length) return head;
      const parts = col.segments.map(sg => `${sg.count} ${window._t('stats_color_' + sg.key).toLowerCase()}`);
      return `${head} · ${parts.join(', ')}`;
    },

    statsDim(chart, key) {
      return this.statsHover.chart === chart && this.statsHover.key !== key;
    },

    colorSeg(key) {
      return this.stats.colors.segments.find(sg => sg.key === key) || null;
    },

    donutCenter() {
      const h = this.statsHover;
      const seg = h.chart === 'colors' ? this.colorSeg(h.key) : null;
      if (seg) return {value: seg.count, label: window._t('stats_color_' + seg.key)};
      return {value: this.stats.colors.total, label: window._t('stats_colors_center')};
    },

    typeIcon(key) {
      const icons = {
        Creature: 'creature', Instant: 'instant', Sorcery: 'sorcery',
        Enchantment: 'enchantment', Artifact: 'artifact', Planeswalker: 'planeswalker',
        Battle: 'battle', Land: 'land',
      };
      return icons[key] || 'multiple';
    },

    _extractPips(manaCost) {
      const pips = [];
      const re = /\{([^}]+)\}/g;
      let m;
      while ((m = re.exec(manaCost)) !== null) {
        const sym = m[1];
        for (const ch of sym.split('/')) {
          if (['W', 'U', 'B', 'R', 'G'].includes(ch.toUpperCase())) {
            pips.push(ch.toUpperCase());
          }
        }
      }
      return pips;
    },

    _updateCard(updated) {
      const idx = this.cards.findIndex(c => c.id === updated.id);
      if (idx >= 0) this.cards[idx] = updated;
    },

    _startBuildPolling() {
      if (this._buildPollTimer) return;
      this._buildPollTimer = setInterval(async () => {
        try {
          const r = await fetch(`/api/decks/${this.deckId}/build-progress`);
          if (!r.ok) return;
          const p = await r.json();
          if (p.active) this.buildProgress = p;
          if (p.done) this._stopBuildPolling();
        } catch (e) {}
      }, 300);
    },
    _stopBuildPolling() {
      if (this._buildPollTimer) {
        clearInterval(this._buildPollTimer);
        this._buildPollTimer = null;
      }
    },

    async buildXml() {
      this.building = true;
      this.buildProgress = null;
      this._startBuildPolling();
      try {
        const r = await fetch(`/api/decks/${this.deckId}/build-xml`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({create_run: true, cardstock: this.cardstock, foil: this.foil})
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const data = await r.json();
        const filename = data.xml_path.split(/[\\/]/).pop();
        this.lastXmlFilename = filename;
        window.toast(window._t('deck_xml_done'), `${data.total_cards} ${window._T.history_cards_count} · ${data.estimated_cost_eur.toFixed(2)} €`, 'success');
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this._stopBuildPolling();
        this.buildProgress = null;
        this.building = false;
      }
    },

    downloadXml() {
      if (!this.lastXmlFilename) return;
      window.saveFile(`/api/exports/${this.lastXmlFilename}`, window.saveFileName(this.deck?.name, 'xml'));
    },

    async launchAutofill() {
      if (!this.lastXmlFilename) return;
      this.launchingAutofill = true;
      try {
        const r = await fetch('/api/mpc-autofill/launch', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({xml_filename: this.lastXmlFilename})
        });
        const data = await r.json();
        if (!r.ok) throw new Error((data.detail && data.detail.message) || data.detail || `HTTP ${r.status}`);
        window.toast(window._t('deck_autofill_launched'), `PID ${data.pid}`, 'success');
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.launchingAutofill = false;
      }
    },

    async buildPdf() {
      this.buildingPdf = true;
      this.buildProgress = null;
      let opts = {};
      try {
        const raw = localStorage.getItem(`pdfStudio.opts.v2.${this.deckId}`);
        if (raw) opts = JSON.parse(raw).opts || {};
      } catch (e) {}
      try {
        let data = null;
        await window.saveFile(async () => {
          this._startBuildPolling();
          const r = await fetch(`/api/decks/${this.deckId}/build-pdf`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(opts),
          });
          if (!r.ok) throw new Error(await window.apiErrorDetail(r));
          data = await r.json();
          return `/api/exports/${data.filename}`;
        }, window.saveFileName(this.deck?.name, 'pdf'));
        if (data) {
          window.toast(window._t('pdf_done'), `${data.total_slots} ${window._T.history_cards_count} · ${data.total_pages} ${window._t('common_pages')}`, 'success');
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this._stopBuildPolling();
        this.buildProgress = null;
        this.buildingPdf = false;
      }
    },

    async localizeDeck() {
      if (!this.localizeLang) return;
      this.localizing = true;
      try {
        const r = await fetch(`/api/decks/${this.deckId}/localize`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({lang: this.localizeLang}),
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const data = await r.json();
        const langLabel = this.supportedLangs[data.lang] || data.lang;

        await this.load();

        let msg = window._t('deck_lang_summary')
          .replace('{changed}', data.localized)
          .replace('{unchanged}', data.unchanged);
        if (data.skipped_custom > 0) {
          msg += ` · ${window._t('deck_lang_custom_kept').replace('{n}', data.skipped_custom)}`;
        }
        if (data.unavailable && data.unavailable.length > 0) {
          const shown = data.unavailable.slice(0, 4).join(', ');
          const more = data.unavailable.length > 4
            ? ' ' + window._t('deck_lang_more').replace('{n}', data.unavailable.length - 4)
            : '';
          msg += '\n' + window._t('deck_lang_unavailable')
            .replace('{lang}', langLabel)
            .replace('{cards}', shown + more);
          console.log('[localize] Cartas sin arte en', langLabel, ':', data.unavailable);
        }
        Alpine.store('ui').success(window._t('deck_lang_done').replace('{lang}', langLabel), msg);
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.localizing = false;
      }
    },

    async _fetchDecklist() {
      const params = new URLSearchParams({
        format: this.decklistFormat,
        include_headers: this.decklistHeaders ? 'true' : 'false',
      });
      const r = await fetch(`/api/decks/${this.deckId}/decklist?${params.toString()}`);
      if (!r.ok) {
        throw new Error(await window.apiErrorDetail(r));
      }
      return await r.json();
    },
    async copyDecklist() {
      this.exportingDecklist = true;
      try {
        const data = await this._fetchDecklist();
        try {
          await navigator.clipboard.writeText(data.text);
        } catch (clipErr) {
          const ta = document.createElement('textarea');
          ta.value = data.text;
          ta.style.position = 'fixed';
          ta.style.left = '-9999px';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        window.toast(
          window._t('deck_decklist_copied'),
          `${data.total_cards} ${window._T.history_cards_count} · ${data.format}`,
          'success',
        );
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.exportingDecklist = false;
      }
    },
    async downloadDecklist() {
      this.exportingDecklist = true;
      try {
        const params = new URLSearchParams({
          format: this.decklistFormat,
          include_headers: this.decklistHeaders ? 'true' : 'false',
        });
        await window.saveFile(
          `/api/decks/${this.deckId}/decklist.txt?${params.toString()}`,
          window.saveFileName(`${this.deck?.name || 'deck'} - ${this.decklistFormat}`, 'txt'),
        );
      } catch (e) {
        window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.exportingDecklist = false;
      }
    }
  }
}

function _mainType(typeLine) {
  const p = (typeLine || '').split('—')[0].trim();
  const priority = ['Creature', 'Planeswalker', 'Battle', 'Instant', 'Sorcery',
                    'Enchantment', 'Artifact', 'Land'];
  for (const t of priority) {
    if (p.includes(t)) return String(priority.indexOf(t)).padStart(2, '0') + '-' + t;
  }
  return '99-' + p;
}

function shortType(typeLine) {
  if (!typeLine) return '';
  return typeLine
    .replace(/^Legendary\s+/, '')
    .replace(/^Basic\s+/, '')
    .replace(/^Snow\s+/, '')
    .replace(/^Elite\s+/, '')
    .replace(/^Host\s+/, '');
}

function rarityBorderClass(rarity) {
  const map = {
    mythic:   'border-l-2 border-l-orange-500/60',
    rare:     'border-l-2 border-l-amber-400/55',
    uncommon: 'border-l-2 border-l-slate-400/45',
    common:   'border-l-2 border-l-slate-600/25',
    special:  'border-l-2 border-l-purple-500/55',
    bonus:    'border-l-2 border-l-purple-500/55',
  };
  return map[rarity] || '';
}

function rarityChipClass(rarity) {
  const map = {
    mythic:   'bg-orange-500/20 text-orange-300 border border-orange-500/40',
    rare:     'bg-amber-400/20 text-amber-300 border border-amber-400/40',
    uncommon: 'bg-slate-300/15 text-slate-300 border border-slate-400/35',
    common:   'bg-slate-600/15 text-slate-400 border border-slate-500/25',
    special:  'bg-purple-500/20 text-purple-300 border border-purple-500/40',
    bonus:    'bg-purple-500/20 text-purple-300 border border-purple-500/40',
  };
  return map[rarity] || 'bg-slate-600/15 text-slate-400 border border-slate-500/25';
}

function rarityLetter(rarity) {
  const map = { mythic: 'M', rare: 'R', uncommon: 'U', common: 'C', special: 'S', bonus: 'B' };
  return map[rarity] || '?';
}

function renderManaCost(manaCost) {
  if (!manaCost) return '';
  const symbols = manaCost.match(/\{[^}]+\}/g) || [];
  const parts = symbols.map(sym => {
    const inner = sym.slice(1, -1).toLowerCase().replace(/[^a-z0-9]/g, '');
    return `<i class="ms ms-${inner} ms-cost"></i>`;
  });
  return `<span class="mana-cost">${parts.join('')}</span>`;
}

window._mainType = _mainType
window.deckEditor = deckEditor
window.rarityBorderClass = rarityBorderClass
window.rarityChipClass = rarityChipClass
window.rarityLetter = rarityLetter
window.renderManaCost = renderManaCost
window.shortType = shortType
