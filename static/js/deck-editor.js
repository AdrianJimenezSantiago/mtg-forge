/**
 * Editor de mazos
 *
 * Extraído de `templates/deck.html`, donde vivía como un bloque `<script>`
 * de 2323 líneas. La lógica es idéntica: solo ha cambiado de fichero.
 *
 * Las funciones que Alpine necesita resolver desde los atributos `x-data` del
 * HTML se publican en `window` al final del módulo. Es deliberado: Alpine
 * evalúa `x-data` como una expresión en el ámbito global, así que un `export`
 * por sí solo no basta.
 *
 * Regenerar con:  python scripts/extract_inline_js.py
 */
function deckEditor(deckId) {
  return {
    deckId,
    deck: null,
    cards: [],
    estimate: null,
    filter: '',
    selectedCardId: null,
    allArts: [],
    loadingPrints: false,
    building: false,
    rescanning: false,

    // Progreso real del build (XML o PDF). Se puebla con polling a
    // /build-progress mientras el POST está pendiente. Al terminar, el POST
    // resuelve y esto vuelve a null.
    buildProgress: null,        // { current, total, current_name, percent, eta_seconds, kind }
    _buildPollTimer: null,      // handle del setInterval de polling

    // Stock / foil
    cardstock: '(S30) Standard Smooth',
    foil: false,

    // PDF: los ajustes viven en el PDF Studio (localStorage por-mazo). Aquí
    // solo mantenemos el flag "estoy generando" para el quick-build del botón
    // secundario y para pintar la barra de progreso.
    buildingPdf: false,

    // Decklist export options
    exportingDecklist: false,
    decklistFormat: 'with_set',  // 'simple' | 'with_set' | 'arena'
    decklistHeaders: true,

    // Localización (idioma del arte)
    localizing: false,
    localizeLang: 'es',                       // se pisa con el default de settings al cargar
    supportedLangs: {en: 'English', es: 'Español'},  // se rellena vía /_/supported-langs

    // Renombrar
    editingName: false,
    editNameValue: '',

    // Añadir carta
    showAddCard: false,
    newCardName: '',
    newCardQty: 1,
    newCardRole: 'mainboard',
    addingCard: false,

    // Autocompletado
    autoResults: [],
    autoOpen: false,
    autoIndex: -1,
    autoAbort: null,

    // Ordenación — persistido en localStorage
    sortMode: localStorage.getItem('deckPref_sortMode') || 'name',  // name | cmc | type | color

    // Modo de agrupación de la lista de cartas:
    //   'role' → grupos por Commander/Mainboard/Sideboard/Tokens/Maybeboard.
    //   'type' → mainboard subdividido por tipo (Creatures/Sorceries/Instants/…),
    //            estilo Moxfield. Commander y roles no-mainboard se mantienen como
    //            grupos aparte al final.
    // Default: 'type' (más útil para ver la composición del mazo de un vistazo).
    groupMode: localStorage.getItem('deckPref_groupMode') || 'type',

    // Layout de las columnas:
    //   'list'    → una sola columna larga.
    //   'columns' → multi-columna CSS masonry (secciones distribuidas por
    //               columnas al estilo Moxfield).
    // Default: 'list' (más legible, especialmente con agrupación por tipo).
    layoutMode: localStorage.getItem('deckPref_layoutMode') || 'list',

    // Add URL custom
    showAddUrlForm: false,
    urlInput: '',
    urlFace: 'front',
    urlVariant: '',
    addingUrl: false,

    activeArtFilters: {full: false, textless: false, borderless: false, promo: false},
    artFilters: [
      {key: 'full', label: 'Full art'},
      {key: 'textless', label: 'Sin texto'},
      {key: 'borderless', label: 'Sin borde'},
      {key: 'promo', label: 'Promo'},
    ],

    // Grupos expandidos: por defecto solo commander + mainboard.
    // El resto (sideboard, tokens, maybeboard, companion) empiezan cerrados.
    expandedGroups: {commander: true, mainboard: true},

    // Modal de reverso (DFC/meld/battle)
    backModal: null,  // {name, url} o null

    // -- Selector de arte (modal grande) --
    artPickerOpen: false,
    pickerCard: null,             // referencia a la carta actual (para header y actions)
    pickerLoading: false,
    pickerAllArts: [],            // todas las opciones cargadas (custom + drives + oficiales)
    pickerVisibleCount: 60,       // cuántas tarjetas se han renderizado ya
    // Tamaño de página del endpoint. 60 llena la rejilla visible con margen
    // en cualquier tamaño de ventana razonable.
    PICKER_PAGE_SIZE: 60,
    // true mientras llegan las páginas 2..N en segundo plano. La interfaz es
    // usable durante todo ese tiempo; solo se muestra un indicador discreto.
    pickerStreaming: false,
    // Contadores del servidor, calculados sobre el conjunto completo aunque
    // el usuario ya haya filtrado.
    pickerFacets: {},
    // AbortController de la apertura en curso.
    _pickerAbort: null,
    pickerAddUrl: '',             // input del "añadir por URL" dentro del modal
    pickerFilters: {
      q: '',
      source: 'all',              // 'all' | 'custom' | 'drives' | 'scryfall'
      set: '',                    // set_code o '' = todos
      artTypes: [],               // ['borderless', 'full_art', 'textless', 'retro', 'promo']
      rarities: [],               // ['mythic', 'rare', 'uncommon', 'common', 'special']
      sort: 'recent',             // 'recent' | 'old' | 'set' | 'artist'
      // Filtros específicos de drives (tags extraídos por gdrive_indexer.extract_tags).
      // Se envían al backend como query strings ?tags_include=…&tags_exclude=…
      // y se aplican en SQL usando índices sobre columnas is_full_art, etc.
      driveTagsInclude: [],       // el arte DEBE tener todos los seleccionados
      driveTagsExclude: [],       // el arte NO DEBE tener ninguno
      // Filtro por set canónico [SET NUM] (Fase 2 · T5). Case-insensitive.
      // Se aplica en backend como IndexedArt.expansion_code == ?
      driveExpansionCode: '',
      // Extras · F2/T8: colapsar artes duplicados por pHash (cross-drive).
      // Cuando true, los artes de drives distintos con la misma imagen
      // aparecen como una única tarjeta con badge "+N iguales".
      dedupSimilar: false,
    },
    // Debounce para re-cargar drives cuando el usuario toca los filtros.
    // Sin esto, cada click dispararía un fetch inmediato → lag y requests
    // redundantes. Ver onDriveFiltersChanged() más abajo.
    _driveFiltersDebounce: null,
    // Cache in-memory de resultados del picker por card_id → {arts, ts}
    // Al elegir un arte (chooseArt/chooseCustom) se invalida esa entrada.
    // Se resetea al cambiar de mazo (la instancia Alpine es por-vista de mazo).
    _pickerCache: {},
    // Estado del buscador de drives dentro del picker. Se pobla en
    // _loadDrivesForPicker() y lo consume el pequeño indicador visible en
    // el header del picker para explicar al usuario si hubo error, si no
    // hay drives indexados, o cuántos resultados salieron. Sin esto, un
    // fetch fallido o un índice vacío parecen "no hay arte" sin más pista.
    driveSearchState: {loading: false, error: null, hits: null},

    // -- Precarga de prints en background --
    preload: {
      total: 0,
      done: 0,
      inProgress: false,
      pollTimer: null,
    },

    // -- Modal de tokens del mazo --
    tokensOpen: false,
    tokensLoading: false,
    tokensData: {tokens: [], total_unique: 0, already_in_deck: 0, missing: 0},
    tokensAdding: false,

    // -- Modal de Print Runs (Extras · F3/T10) --
    printRunsOpen: false,
    printRunsLoading: false,
    printRunsBuilding: false,
    printRunsMode: 'greedy',       // 'greedy' | 'optimized'
    printRunsMaxTier: '',          // '' | '108' | '180' | …
    printRunsData: null,           // {total_runs, runs: [...], ...}

    // -- Modal "Ver similares" (pHash · Extras F2/T8) --
    similarOpen: false,
    similarLoading: false,
    similarSourceArt: null,        // el arte de referencia
    similarData: null,             // {reference_hash, similar: [...]}

    // -- Modal "Aplicar arte de X a N cartas" (Extras F3/T11) --
    artistApplyOpen: false,
    artistApplyLoading: false,
    artistApplyApplying: false,
    artistApplySource: null,
    artistApplyArtist: '',
    artistApplyData: null,         // {matched, unmatched_count, skipped_count, total_deck_uniques}
    artistApplyChecked: {},        // {oracle_id: bool}

    // -- Modal de estadísticas --
    statsOpen: false,
    stats: {
      totalCards: 0,
      summary: [],       // [{label, value, icon, hint}]
      curve: {},         // {cmc: count}
      colors: {},        // {W: n, U: n, B: n, R: n, G: n, C: n}
      types: {},         // {Creature: n, ...}
    },
    _chartInstances: [], // Chart.js instances a destruir al cerrar

    // Estado de "añadiendo relacionadas" para deshabilitar botón
    addingRelated: null,  // card.id o null

    // Post-XML: enlace de descarga + lanzamiento a MPC Autofill
    lastXmlFilename: null,
    launchingAutofill: false,
    autofillStatus: {available: false, exe_path: null, source: 'not_found', hint: null},

    // Fuentes de arte comunitarias (Google Drives)
    artSources: [],

    // Búsqueda en drives comunitarios (fuzzy)
    driveHits: [],
    driveSearching: false,
    driveIndexStats: {total_files: 0, sources_indexed: 0},
    addingFromDrive: null,   // file_id del que estamos añadiendo, para deshabilitar botón

    // ---- Getters ----
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
    // Grupos por rol, con orden y labels ES
    get cardGroups() {
      const config = [
        {role: 'commander', label: 'Comandante', dotClass: 'bg-accent'},
        {role: 'mainboard', label: 'Mazo', dotClass: 'bg-blue-400'},
        {role: 'companion', label: 'Compañero', dotClass: 'bg-purple-400'},
        {role: 'sideboard', label: 'Sideboard', dotClass: 'bg-fg-muted'},
        {role: 'tokens', label: 'Tokens', dotClass: 'bg-emerald-400'},
        {role: 'maybeboard', label: 'Maybeboard', dotClass: 'bg-fg-faint'},
      ];
      // Comandante + mazo se muestran juntos como una sola sección "Mazo (100)"
      // Nota: los mostramos separados con sus contadores propios, pero el usuario
      // puede leer la validación 100/100 en el header. Aquí siguen agrupados por rol.
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
      // Cualquier rol no mapeado va al final agrupado
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

    // Grupos por TIPO (estilo Moxfield). Divide el mainboard en subsecciones
    // Creatures / Sorceries / Instants / Artifacts / Enchantments / Planeswalkers /
    // Battles / Lands. Los roles fuera del mainboard (commander, sideboard, tokens,
    // maybeboard, companion) siguen apareciendo como grupos aparte al final para
    // que el usuario los siga viendo.
    //
    // Convención Moxfield: el "role" del grupo aquí es 'type:<Type>' para poder
    // usarlo como key en expandedGroups sin colisionar con los roles reales.
    get cardGroupsByType() {
      const source = this.filteredCards;
      // Orden y config visual por tipo. dotClass sigue la paleta del proyecto.
      const typeConfig = [
        {type: 'Creature',     label: 'Criaturas',     dotClass: 'bg-emerald-400'},
        {type: 'Planeswalker', label: 'Planeswalkers', dotClass: 'bg-purple-400'},
        {type: 'Sorcery',      label: 'Conjuros',      dotClass: 'bg-rose-400'},
        {type: 'Instant',      label: 'Instantáneos',  dotClass: 'bg-sky-400'},
        {type: 'Enchantment',  label: 'Encantamientos',dotClass: 'bg-amber-300'},
        {type: 'Artifact',     label: 'Artefactos',    dotClass: 'bg-slate-300'},
        {type: 'Battle',       label: 'Batallas',      dotClass: 'bg-red-500'},
        {type: 'Land',         label: 'Tierras',       dotClass: 'bg-lime-500'},
      ];

      // Separa mainboard del resto — solo el mainboard se divide por tipo.
      const mainboardCards = source.filter(c => c.role === 'mainboard');
      const otherRoles = source.filter(c => c.role !== 'mainboard');

      const out = [];

      // Commander SIEMPRE arriba del todo (patrón Moxfield).
      const commanders = otherRoles.filter(c => c.role === 'commander');
      if (commanders.length > 0) {
        out.push({
          role: 'commander',
          label: 'Comandante',
          dotClass: 'bg-accent',
          cards: this._sortCards(commanders),
          total: commanders.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
        });
      }

      // Subgrupos del mainboard por tipo — solo si hay cartas de ese tipo.
      for (const cfg of typeConfig) {
        const inType = mainboardCards.filter(c => {
          const t = (c.type_line || '').split('—')[0];
          const re = new RegExp('\\b' + cfg.type + '\\b', 'i');
          // Priorizamos el primer tipo detectado; ver _mainType. Como usamos
          // el mismo orden, la primera coincidencia en typeConfig gana.
          if (!re.test(t)) return false;
          // Evitar contar dos veces (una carta con "Artifact Creature" solo
          // aparece como Creature, no como Artifact). Comprobamos que ningún
          // tipo de mayor prioridad matchea antes.
          for (const prev of typeConfig) {
            if (prev.type === cfg.type) break;
            if (new RegExp('\\b' + prev.type + '\\b', 'i').test(t)) return false;
          }
          return true;
        });
        if (inType.length === 0) continue;
        out.push({
          role: 'type:' + cfg.type,      // key único para expandedGroups
          label: cfg.label,
          dotClass: cfg.dotClass,
          cards: this._sortCards(inType),
          total: inType.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
        });
      }

      // Otras cartas del mainboard cuyo tipo no matchee ninguno (raro).
      const uncategorized = mainboardCards.filter(c => {
        const t = (c.type_line || '').split('—')[0];
        return !typeConfig.some(cfg => new RegExp('\\b' + cfg.type + '\\b', 'i').test(t));
      });
      if (uncategorized.length > 0) {
        out.push({
          role: 'type:Other',
          label: 'Otros',
          dotClass: 'bg-fg-faint',
          cards: this._sortCards(uncategorized),
          total: uncategorized.reduce((n, c) => n + (c.include ? c.quantity : 0), 0),
        });
      }

      // Resto de roles (sideboard, tokens, maybeboard, companion) siguen al final.
      const restConfig = [
        {role: 'companion',  label: 'Compañero',  dotClass: 'bg-purple-400'},
        {role: 'sideboard',  label: 'Sideboard',  dotClass: 'bg-fg-muted'},
        {role: 'tokens',     label: 'Tokens',     dotClass: 'bg-emerald-400'},
        {role: 'maybeboard', label: 'Maybeboard', dotClass: 'bg-fg-faint'},
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

    // Getter unificado — la vista usa este. Elige según groupMode.
    get displayGroups() {
      return this.groupMode === 'type' ? this.cardGroupsByType : this.cardGroups;
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

    // ---- Load ----
    async load() {
      const r = await fetch(`/api/decks/${this.deckId}`);
      const d = await r.json();
      this.deck = d;
      this.cards = d.cards;
      this.loadEstimate();

      // Persistir preferencias de vista del mazo al cambiar
      this.$watch('sortMode',   v => localStorage.setItem('deckPref_sortMode', v));
      this.$watch('groupMode',  v => localStorage.setItem('deckPref_groupMode', v));
      this.$watch('layoutMode', v => localStorage.setItem('deckPref_layoutMode', v));

      // Cargar defaults de settings si aún no lo hemos hecho
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
        } catch (e) { /* no crítico */ }
        // Cargar mapa de idiomas soportados por Scryfall (para el selector)
        try {
          const lr = await fetch('/api/decks/_/supported-langs');
          if (lr.ok) this.supportedLangs = await lr.json();
        } catch (e) { /* no crítico */ }
        // Detectar autofill al arrancar (para mostrar u ocultar el botón)
        try {
          const st = await fetch('/api/mpc-autofill/status');
          if (st.ok) this.autofillStatus = await st.json();
        } catch (e) { /* no crítico */ }
        // Cargar fuentes de arte comunitarias
        try {
          const as = await fetch('/api/art-sources/');
          if (as.ok) this.artSources = await as.json();
        } catch (e) { /* no crítico */ }
        // Cargar estadísticas del índice de drives
        try {
          const ds = await fetch('/api/drives/stats');
          if (ds.ok) this.driveIndexStats = await ds.json();
        } catch (e) { /* no crítico */ }
        this._settingsLoaded = true;
      }
      // Arrancar precarga de prints en background (solo la primera vez que se
      // llama load; no en reloads posteriores dentro del mismo mazo).
      if (!this._preloadStarted) {
        this._preloadStarted = true;
        this._startPreload();
      }
    },
    async loadEstimate() {
      const r = await fetch(`/api/decks/${this.deckId}/estimate`);
      this.estimate = await r.json();
    },

    // ---- Selector arte ----
    async selectCard(card) {
      this.selectedCardId = card.id;
      this.loadingPrints = true;
      this.allArts = [];
      this.showAddUrlForm = false;
      this.driveHits = [];  // limpiar resultados anteriores
      try {
        // Igual que en el mini selector: el endpoint está paginado y aquí se
        // necesita el conjunto completo, así que se pide con `allPrints`.
        this.allArts = await window.api.cards.allPrints(this.deckId, card.id);
      } catch (e) {
        window.toast(window._t('deck_error_load_arts'), e.message);
      } finally {
        this.loadingPrints = false;
      }
      // Buscar en drives en paralelo (no bloquea la UI de oficiales)
      if (this.driveIndexStats.total_files > 0) {
        this.searchDrives(card.name);
      }
    },

    // ================================================================
    // ART PICKER (modal grande de selección)
    // ================================================================

    async openArtPicker(card) {
      this.pickerCard = card;
      this.selectedCardId = card.id;
      this.artPickerOpen = true;
      this.pickerVisibleCount = 60;
      this.driveHits = [];
      // Estado de diagnóstico del buscador de drives (mostrado en la UI del picker)
      this.driveSearchState = {loading: false, error: null, hits: null};

      // Refrescar stats de drives ANTES de decidir si buscar en drives.
      // Sin esto, si el user indexó drives desde Settings mientras el deck
      // view seguía cargado, driveIndexStats se queda a 0 y nunca se buscan
      // artes de drives — bug reportado tras añadir el batch indexing.
      try {
        const ds = await fetch('/api/drives/stats');
        if (ds.ok) this.driveIndexStats = await ds.json();
      } catch (e) { /* no crítico */ }

      // Cache hit: reabrir instantáneo, sin fetch (Plan C)
      const cached = this._pickerCache[card.id];
      if (cached) {
        this.pickerAllArts = cached;
        this.allArts = cached.filter(a => a.kind !== 'drive');   // compat con métodos existentes
        this.pickerLoading = false;
        this.$nextTick(() => window.icons?.());
        // Aún así, refrescamos búsqueda de drives (por si añadiste artes desde otra pestaña)
        if (this.driveIndexStats.total_files > 0) {
          this._loadDrivesForPicker(card.name);
        }
        return;
      }

      // Cache miss: carga paginada.
      //
      // El endpoint devolvía antes las ~900 impresiones de una carta muy
      // reimpresa en una sola respuesta de ~400 KB, y el picker no se pintaba
      // hasta tenerlas todas. Ahora pedimos la primera página (60), pintamos
      // de inmediato, y el resto se va anexando en segundo plano. Los filtros
      // de cliente (set, tipo de arte, rareza) siguen funcionando igual y se
      // vuelven exhaustivos cuando termina el streaming.
      this.pickerLoading = true;
      this.pickerAllArts = [];
      // Un AbortController por apertura: si el usuario cierra el modal o abre
      // otra carta a mitad del streaming, las peticiones en vuelo se cancelan
      // en vez de seguir consumiendo red y escribir sobre el estado nuevo.
      this._pickerAbort?.abort();
      this._pickerAbort = new AbortController();
      const signal = this._pickerAbort.signal;

      try {
        const page = await this._fetchPrintsPage(card.id, 0, signal);
        // Los artes custom del usuario van completos en la primera página.
        const first = [...page.custom, ...page.items];
        this.allArts = first;
        this.pickerAllArts = first.map((a, i) => this._preprocessArt(a, i, 'local'));
        this.pickerFacets = page.facets || {};
        this.pickerLoading = false;   // ← ya se puede pintar
        this.$nextTick(() => window.icons?.());

        // Resto de páginas, sin bloquear la interfaz.
        if (page.has_more) {
          this._streamRemainingPrints(card.id, page, signal);
        } else {
          this._pickerCache[card.id] = this.pickerAllArts;
        }
      } catch (e) {
        if (e.name !== 'AbortError') {
          window.toast(window._t('common_error'), e.detail || e.message);
        }
        this.pickerLoading = false;
      }
      if (this.driveIndexStats.total_files > 0) {
        this._loadDrivesForPicker(card.name);
      }
      this.$nextTick(() => window.icons?.());
    },

    /** Una página del endpoint paginado de impresiones. */
    async _fetchPrintsPage(cardId, offset, signal) {
      return window.api.cards.prints(this.deckId, cardId, {
        offset,
        limit: this.PICKER_PAGE_SIZE,
        sort: 'released_desc',
        signal,
      });
    },

    /**
     * Descarga las páginas restantes y las va anexando.
     *
     * Se hace en serie a propósito: son peticiones a localhost contra datos ya
     * cacheados, y lanzarlas en paralelo solo añadiría contención en SQLite
     * sin mejorar el tiempo percibido, que ya lo resuelve la primera página.
     */
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
          // Solo se cachea el conjunto COMPLETO: guardarlo a medias haría que
          // reabrir la carta mostrase menos opciones de las que hay.
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
      this.driveSearchState = {loading: true, error: null, hits: null};
      try {
        // Construir query string con los filtros de tag activos.
        const params = new URLSearchParams({
          q: cardName,
          limit: '100',
        });
        const inc = this.pickerFilters.driveTagsInclude || [];
        const exc = this.pickerFilters.driveTagsExclude || [];
        if (inc.length) params.set('tags_include', inc.join(','));
        if (exc.length) params.set('tags_exclude', exc.join(','));
        // Extras · F2/T5: filtro por set canónico [SET NUM]
        const expCode = (this.pickerFilters.driveExpansionCode || '').trim().toLowerCase();
        if (expCode) params.set('expansion_code', expCode);

        const r = await fetch(`/api/drives/search?${params.toString()}`);
        if (!r.ok) {
          this.driveSearchState = {loading: false, error: `HTTP ${r.status}`, hits: 0};
          return;
        }
        const hits = await r.json();
        this.driveHits = hits;
        this.driveSearchState = {loading: false, error: null, hits: hits.length};
        // Añadir cada hit al pickerAllArts como si fuera un "arte" más.
        // Pasamos los flags is_* y tags para que _preprocessArt los pueda
        // enseñar como badges en el thumbnail (junto a los ya existentes de
        // Scryfall).
        const driveArts = hits.map((h, i) => this._preprocessArt({
          kind: 'drive',
          image_small: h.thumb_url,
          download_url: h.download_url,
          filename: h.filename,
          set_name: h.source_name,
          collector_number: h.folder_path || '',
          artist: null,
          released_at: null,
          rarity: '',
          // Los flags aquí replican los Scryfall que ya consumen los badges,
          // más los propios de drives que añadimos en preprocess.
          full_art:   !!h.is_full_art,
          textless:   !!h.is_textless,
          promo:      !!h.is_promo,
          border_color: h.is_borderless ? 'borderless' : '',
          frame:      h.is_retro ? '1997' : '',
          is_extended:  !!h.is_extended,
          is_showcase:  !!h.is_showcase,
          is_alt_art:   !!h.is_alt_art,
          drive_tags:   h.tags || [],
          // Metadatos canónicos [SET NUM] extraídos del filename/folder
          // (Fase 2 · T5). Solo presente si el filename usa la convención.
          canonical_set: h.expansion_code || null,
          canonical_num: h.collector_number || null,
          // Extras · F2/T8: pHash (para "Ver similares" y dedup cross-drive).
          image_hash: h.image_hash || null,
          face: 'front',
          score: h.score,
          _drive_hit: h,
        }, i, 'drives'));
        // Fusionar: filtrar drives previos + añadir nuevos
        const nonDrive = this.pickerAllArts.filter(a => a.__source !== 'drives');
        this.pickerAllArts = [...nonDrive, ...driveArts];
        // Actualizar cache
        if (this.pickerCard) {
          this._pickerCache[this.pickerCard.id] = this.pickerAllArts;
        }
      } catch (e) {
        this.driveSearchState = {loading: false, error: e.message || 'Error', hits: 0};
      }
    },

    // Handler cuando el usuario cambia checkboxes de tags de drive.
    // Aplicamos un debounce corto (250ms) para agrupar clicks rápidos, e
    // invalidamos la cache del picker para que el próximo openArtPicker
    // no reuse los drives antiguos.
    onDriveFiltersChanged() {
      if (!this.pickerCard) return;
      clearTimeout(this._driveFiltersDebounce);
      this._driveFiltersDebounce = setTimeout(() => {
        // Invalidar cache antes de refetch para no dejar residuos con los
        // filtros anteriores en la vista.
        if (this.pickerCard && this._pickerCache[this.pickerCard.id]) {
          // Preservar los no-drives (custom + scryfall) en la cache; solo
          // quitamos los drives, que van a re-cargarse con los filtros nuevos.
          this._pickerCache[this.pickerCard.id] =
            this._pickerCache[this.pickerCard.id].filter(a => a.__source !== 'drives');
        }
        this._loadDrivesForPicker(this.pickerCard.name);
      }, 250);
    },

    // ---- Precarga en background (Plan A) ----
    async _startPreload() {
      try {
        const r = await fetch(`/api/decks/${this.deckId}/preload-prints`, {method: 'POST'});
        if (!r.ok) return;
        const st = await r.json();
        this.preload.total = st.total || 0;
        this.preload.done = st.done || 0;
        this.preload.inProgress = st.in_progress;
        if (this.preload.total === 0) return;  // mazo vacío o nada que precargar
        this._pollPreload();
      } catch (e) { /* silent */ }
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
        } catch (e) { /* silent */ }
      }, 2000);
    },

    _preprocessArt(art, idx, kindHint) {
      // Enriquecer cada art con campos derivados para la UI del picker.
      // `__thumb` es lo que pinta la rejilla: miniatura WebP local (~6 KB) si
      // el backend ya tiene el arte descargado, y si no la imagen remota de
      // siempre (~90 KB). El onerror del <img> hace el fallback si la
      // miniatura fallara al generarse.
      art.__thumb = art.thumb_url || art.image_small;
      const source =
        art.kind === 'custom' ? 'custom' :
        art.kind === 'drive'  ? 'drives' :
        'scryfall';

      // Etiquetas visibles en el thumbnail
      let label, sublabel, tooltip;
      if (source === 'custom') {
        label = art.variant_label || 'custom';
        sublabel = art.filename || '';
        tooltip = art.filename || 'Arte custom';
      } else if (source === 'drives') {
        // Si el drive lleva metadatos canónicos [SET NUM] (Fase 2 · T5),
        // mostramos "SET · #NUM" arriba y el nombre del source como sublabel.
        // Consistente con la etiqueta que ya usamos para scryfall.
        if (art.canonical_set) {
          label = art.canonical_set.toUpperCase()
                + (art.canonical_num ? ' · #' + art.canonical_num : '');
          sublabel = art.set_name || art.filename || '';   // set_name = source_name en drives
          tooltip = `${art.canonical_set.toUpperCase()} #${art.canonical_num || '?'} · `
                  + `${art.set_name} · ${art.filename}`;
        } else {
          label = art.set_name || 'Drive';    // en drives, set_name = source_name
          sublabel = art.filename || '';
          tooltip = `${art.set_name} · ${art.filename}`;
        }
      } else {
        label = (art.set_code || '?').toUpperCase() + ' · ' + (art.collector_number || '?');
        const year = art.released_at ? art.released_at.slice(0, 4) : '';
        sublabel = art.artist ? `${art.artist}${year ? ' · ' + year : ''}` : year;
        tooltip = `${art.set_name || art.set_code} #${art.collector_number} · ${art.artist || 'artist unknown'}${year ? ' (' + year + ')' : ''}`;
      }

      // Badges de tipo especial. Scryfall usa border_color/full_art/frame;
      // drives ahora también reflejan sus tags con las mismas etiquetas
      // (BRDLESS, FULL, RETRO, PROMO…) más las que solo aplican a drives
      // (EXT, SHOWCASE, ALT).
      const badges = [];
      if (source === 'scryfall') {
        if (art.border_color === 'borderless') badges.push('BRDLESS');
        if (art.full_art)                      badges.push('FULL');
        if (art.textless)                      badges.push('TEXT-');
        if (art.frame === '1997')              badges.push('RETRO');
        if (art.promo)                         badges.push('PROMO');
      } else if (source === 'drives') {
        // En drives, los flags vienen del extract_tags del backend, no de
        // Scryfall — pero los reutilizamos para consistencia visual.
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
      // Cancelar el streaming en vuelo: si el usuario cierra el modal a mitad
      // de la carga de una carta con 900 impresiones, seguir descargando
      // páginas no aporta nada, y las respuestas tardías escribirían sobre el
      // estado de la siguiente carta que abra.
      this._pickerAbort?.abort();
      this._pickerAbort = null;
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
      // Si los filtros de drive habían cambiado los resultados servidos,
      // hay que refetch: los que la query trajo bajo esos filtros ya no
      // representan el conjunto completo. Si no había filtros activos, no
      // gastamos una round-trip.
      if (hadDriveFilters && this.pickerCard) {
        this.onDriveFiltersChanged();
      }
    },

    // Cuando cambian filtros, resetear el contador incremental
    get pickerFilteredArts() {
      let arts = this.pickerAllArts;

      // Filtro por fuente
      if (this.pickerFilters.source !== 'all') {
        arts = arts.filter(a => a.__source === this.pickerFilters.source);
      }

      // Búsqueda por texto libre (set name, artist, collector, filename)
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

      // Set (solo aplica a scryfall)
      if (this.pickerFilters.set) {
        arts = arts.filter(a => a.__source !== 'scryfall' || a.set_code === this.pickerFilters.set);
      }

      // Tipos de arte (solo scryfall)
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

      // Rareza (solo scryfall)
      if (this.pickerFilters.rarities.length > 0) {
        arts = arts.filter(a => {
          if (a.__source !== 'scryfall') return false;
          return this.pickerFilters.rarities.includes(a.rarity);
        });
      }

      // Ordenación
      const sorted = [...arts];
      const cmp = {
        recent:  (a, b) => (b.__year || 0) - (a.__year || 0),
        old:     (a, b) => (a.__year || 9999) - (b.__year || 9999),
        set:     (a, b) => (a.set_code || 'zzz').localeCompare(b.set_code || 'zzz'),
        artist:  (a, b) => (a.artist || 'zzz').localeCompare(b.artist || 'zzz'),
      }[this.pickerFilters.sort] || ((a, b) => 0);
      // Custom y Drives siempre primero (independiente del sort)
      sorted.sort((a, b) => {
        const rank = { custom: 0, drives: 1, scryfall: 2 };
        const dr = (rank[a.__source] ?? 3) - (rank[b.__source] ?? 3);
        if (dr !== 0) return dr;
        // Elegido primero dentro de cada bucket
        if (a.is_chosen !== b.is_chosen) return a.is_chosen ? -1 : 1;
        return cmp(a, b);
      });

      // Extras · F2/T8: dedup perceptual por image_hash cuando el usuario
      // lo activa. Colapsa artes de drives distintos con el mismo pHash
      // (misma imagen alojada en varios sitios) en una sola tarjeta que
      // muestra "+N iguales" como badge — reduce el ruido visual del
      // picker cuando hay 5 drives con el mismo pool.
      //
      // Se aplica DESPUÉS del sort para preservar cuál "sale ganador" (el
      // primero en el orden actual). Solo dedupea entre `drives` — custom
      // y scryfall no llevan pHash.
      if (this.pickerFilters.dedupSimilar) {
        const seen = new Map();  // image_hash → índice del "ganador"
        const deduped = [];
        for (const a of sorted) {
          const h = a.image_hash;
          if (a.__source !== 'drives' || !h) {
            deduped.push(a);
            continue;
          }
          if (seen.has(h)) {
            // Ya hay un ganador — solo incrementamos el contador de duplicados
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
      // Cargar más cuando queden 300px al final
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 300) {
        if (this.pickerVisibleCount < this.pickerFilteredArts.length) {
          this.pickerVisibleCount = Math.min(this.pickerVisibleCount + 60, this.pickerFilteredArts.length);
        }
      }
    },

    // Elegir un arte: single click = solo este mazo; doble click = recordar globalmente
    async pickArt(art, remember) {
      if (art.__source === 'custom') {
        await this.chooseCustom({custom_art_id: art.custom_art_id, face: art.face});
        window.toast(window._t('deck_art_updated_toast'), art.filename || 'Custom art');
        this.closeArtPicker();
      } else if (art.__source === 'drives') {
        // Descargar el arte del drive y asignarlo (como useDriveArt)
        try {
          const r = await fetch('/api/custom-art/from-url', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              url: art.download_url,
              card_name: this.pickerCard.name,
              face: 'front',
              variant: art.set_name,   // en drives, set_name es el source_name
            })
          });
          if (!r.ok) throw new Error((await r.json()).detail || 'Error');
          const data = await r.json();
          await this.chooseCustom({custom_art_id: data.id, face: 'front'});
          window.toast(`${window._T.deck_added_from} ${art.set_name}`, art.filename);
          this.closeArtPicker();
        } catch (e) {
          window.toast(window._t('deck_error_adding_art'), e.message);
        }
      } else {
        // Oficial de Scryfall
        await this.chooseArt(art.scryfall_id, remember);
        window.toast(
          remember ? window._t('deck_art_remembered') : window._t('deck_art_updated_toast'),
          `${art.set_code?.toUpperCase() || ''} #${art.collector_number || ''}`
        );
        this.closeArtPicker();
      }
    },

    async addCustomFromUrl() {
      // Reutiliza el flujo existente pero desde dentro del picker
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
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const data = await r.json();
        window.toast(window._t('deck_custom_added'), data.filename || 'Custom art');
        this.pickerAddUrl = '';
        // Recargar arts para incluir el nuevo
        await this.openArtPicker(this.pickerCard);
      } catch (e) {
        window.toast(window._t('deck_error_download'), e.message);
      } finally {
        this.addingUrl = false;
      }
    },

    // ---- Búsqueda en drives comunitarios ----
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
        // Reutilizamos el endpoint existente /api/custom-art/from-url
        // que ya sabe descargar URLs de Google Drive gracias al parser.
        const r = await fetch('/api/custom-art/from-url', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            url: hit.download_url,
            card_name: this.selectedCard.name,
            face: face,
            variant: hit.source_name,
          })
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const data = await r.json();
        // Asignar automáticamente al mazo como custom art
        await this.chooseCustom({custom_art_id: data.id, face: face});
        // Refrescar el panel para que aparezca en la sección Custom
        await this.selectCard(this.selectedCard);
        window.toast(
          `${window._T.deck_added_from} ${hit.source_name}`,
          `${hit.filename} — ${face}`
        );
      } catch (e) {
        window.toast(window._t('deck_error_adding_art'), e.message);
      } finally {
        this.addingFromDrive = null;
      }
    },
    toggleArtFilter(k) { this.activeArtFilters[k] = !this.activeArtFilters[k]; },

    // Devuelve si un grupo debe estar expandido.
    //
    // Defaults por tipo de grupo (solo se aplican si el usuario no ha tocado
    // manualmente ese grupo):
    //   - commander, mainboard         → expandido (siempre lo primero)
    //   - type:*                       → expandido (agrupación estilo Moxfield)
    //   - sideboard, tokens, maybeboard, companion → colapsado (info secundaria)
    //
    // Una vez el usuario hace toggleGroup, el valor pasa a estar explícito en
    // expandedGroups y este helper solo devuelve ese valor.
    isGroupExpanded(role) {
      const v = this.expandedGroups[role];
      if (v !== undefined) return v;
      if (role === 'commander' || role === 'mainboard') return true;
      if (role.startsWith('type:')) return true;
      return false;  // sideboard/tokens/maybeboard/companion + roles desconocidos
    },
    toggleGroup(role) {
      // Si nunca se ha tocado, invertimos desde el default calculado.
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
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const added = await r.json();
        if (added.length === 0) {
          window.toast(window._t('deck_no_related'), window._t('deck_related_already'));
        } else {
          const names = added.map(c => c.name).join(', ');
          window.toast(window._t('deck_related_added').replace('{n}', added.length), names);
          // Auto-expandir el grupo tokens para que se vea
          this.expandedGroups.tokens = true;
        }
        await this.load();
      } catch (e) {
        window.toast(window._t('deck_error_adding'), e.message);
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
      if (!r.ok) { window.toast(window._t('common_error'), window._t('deck_error_art')); return; }
      const updated = await r.json();
      this._updateCard(updated);
      this.allArts = this.allArts.map(a => ({...a, is_chosen: a.kind === 'scryfall' && a.scryfall_id === scryfall_id}));
      // Invalidar cache del picker para esta carta — is_chosen ha cambiado
      delete this._pickerCache[card.id];
      if (remember) window.toast(window._t('deck_art_remembered'), card.name);
    },
    async chooseCustom(art) {
      const card = this.selectedCard;
      if (!card) return;
      const r = await fetch(`/api/decks/${this.deckId}/cards/change-art`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({deck_card_id: card.id, custom_art_id: art.custom_art_id, face: art.face})
      });
      if (!r.ok) { window.toast(window._t('common_error'), window._t('deck_error_custom_art')); return; }
      const updated = await r.json();
      this._updateCard(updated);
      this.allArts = this.allArts.map(a => {
        if (a.kind === 'custom') return {...a, is_chosen: a.custom_art_id === art.custom_art_id && a.face === art.face};
        if (art.face === 'front') return {...a, is_chosen: false};
        return a;
      });
      // Invalidar cache del picker
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
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        window.toast(window._t('deck_custom_added'), card.name);
        this.urlInput = ''; this.urlVariant = ''; this.showAddUrlForm = false;
        await this.selectCard(card);
        await this.load();
      } catch (e) {
        window.toast(window._t('deck_error_download'), e.message);
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
      if (r.ok) {
        window.toast(window._t('common_delete'), art.filename);
        const card = this.selectedCard;
        await this.selectCard(card);
        await this.load();
      }
    },
    async rescanCustom() {
      this.rescanning = true;
      try {
        const r = await fetch('/api/custom-art/rescan', {method: 'POST'});
        const data = await r.json();
        window.toast(window._t('deck_rescanned'), `${data.total} ${window._T.deck_files_count} (+${data.added} / -${data.removed})`);
        if (this.selectedCard) await this.selectCard(this.selectedCard);
        await this.load();
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this.rescanning = false;
      }
    },

    // ---- CRUD cartas ----
    // Helper: refresca solo la validación del mazo (contadores, mensaje de
    // completitud). Se llama tras cambios que afectan al total pero no a la
    // lista de cartas — evita el load() completo (5 queries + serialización
    // del mazo entero). ~10x más rápido en mazos grandes.
    async _refreshValidation() {
      try {
        const r = await fetch(`/api/decks/${this.deckId}/validation`);
        if (r.ok && this.deck) {
          this.deck.validation = await r.json();
        }
      } catch (e) { /* silent — no crítico */ }
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
        // En paralelo: refresh de validation + estimate. No bloqueamos con
        // await para que la UI responda inmediatamente al cambio de qty.
        this._refreshValidation();
        this.loadEstimate();
      }
    },
    async toggleInclude(card) {
      const r = await fetch(`/api/decks/${this.deckId}/cards/${card.id}/toggle`, {method: 'POST'});
      if (r.ok) {
        const updated = await r.json();
        this._updateCard(updated);
        // Refresh de validation + estimate en paralelo, sin recargar todo el
        // mazo. Antes: load() → 1 request de deck completo (~50-80KB para
        // 100 cartas). Ahora: 2 requests pequeños ejecutados en paralelo.
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
        window.toast(window._t('deck_card_deleted'), card.name);
        if (this.selectedCardId === card.id) { this.selectedCardId = null; this.allArts = []; }
        await this.load();
      } else {
        window.toast(window._t('common_error'), window._t('deck_error_delete'));
      }
    },

    // Mueve una carta a otra sección (rol). Usado por el menú "Mover a…" en las
    // acciones por carta. Reutiliza el mismo endpoint PATCH que ya acepta `role`.
    async moveCardTo(card, newRole) {
      if (!newRole || card.role === newRole) return;
      const r = await fetch(`/api/decks/${this.deckId}/cards/${card.id}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({role: newRole})
      });
      if (r.ok) {
        // Aseguramos que el grupo destino queda expandido para que el usuario
        // vea inmediatamente dónde ha ido a parar la carta.
        this.expandedGroups[newRole] = true;
        const updated = await r.json();
        // Actualizamos la carta in-place (Alpine reactiva → los cardGroups
        // se recomputan automáticamente porque son un getter). Antes hacíamos
        // load() aquí, ~50-80 KB en un mazo commander lleno.
        this._updateCard(updated);
        this._refreshValidation();
        window.toast(window._t('deck_kind_moved') || card.name, `${card.name} → ${this.roleLabel(newRole)}`);
      } else {
        window.toast(window._t('common_error'), window._t('deck_error_move'));
      }
    },

    // Devuelve la etiqueta legible de un rol (para toasts / confirmDialogs).
    // Fallback al propio rol si es uno desconocido.
    roleLabel(role) {
      const g = this.cardGroups.find(g => g.role === role);
      return g ? g.label : role;
    },

    // Vacía TODAS las cartas de una sección. Confirma primero. Usado por el
    // botón "Vaciar" que aparece en la cabecera de cada grupo cuando tiene
    // al menos una carta.
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
        window.toast(`${label} ${window._T.deck_section_cleared}`, `${data.deleted} ${window._T.deck_cards_deleted}`);
        if (this.selectedCardId) { this.selectedCardId = null; this.allArts = []; }
        await this.load();
      } else {
        window.toast(window._t('common_error'), window._t('deck_error_clear'));
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
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        window.toast(window._t('history_kind_added'), this.newCardName);
        this.newCardName = '';
        this.autoResults = [];
        this.autoOpen = false;
        await this.load();
        // Devolver foco al input para añadir varias en cadena
        this.$nextTick(() => this.$refs.newCardInput && this.$refs.newCardInput.focus());
      } catch (e) {
        window.toast(window._t('deck_error_adding'), e.message);
      } finally {
        this.addingCard = false;
      }
    },

    // ---- Autocompletado Scryfall ----
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
      // Enfocar la cantidad para poder añadir directo
      this.$nextTick(() => this.$refs.newCardInput && this.$refs.newCardInput.focus());
    },

    // ---- CRUD deck ----
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
        window.toast(window._t('history_kind_renamed'), v);
      } else {
        window.toast(window._t('common_error'), window._t('js_error_rename'));
      }
    },
    async deleteDeck() {
      if (!await window.confirmDialog(
        window._t('deck_delete'),
        window._t('js_delete_deck_confirm').replace('{name}', this.deck.name),
        { danger: true, icon: 'trash-2', confirmLabel: window._t('common_delete') })) return;
      const r = await fetch(`/api/decks/${this.deckId}`, {method: 'DELETE'});
      if (r.ok) { window.location.href = '/'; }
      else window.toast(window._t('common_error'), window._t('deck_error_delete'));
    },

    // ================================================================
    // ESTADÍSTICAS DEL MAZO
    // ================================================================

    // ================================================================
    // TOKENS DEL MAZO
    // ================================================================

    async openTokens() {
      this.tokensOpen = true;
      this.tokensLoading = true;
      this.tokensData = {tokens: [], total_unique: 0, already_in_deck: 0, missing: 0};
      try {
        const r = await fetch(`/api/decks/${this.deckId}/tokens-analysis`);
        if (!r.ok) throw new Error('Error analizando tokens');
        this.tokensData = await r.json();
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
        this.closeTokens();
      } finally {
        this.tokensLoading = false;
        this.$nextTick(() => window.icons?.());
      }
    },

    closeTokens() {
      this.tokensOpen = false;
    },

    // ------ Extras · F3/T10: Modal de Print Runs ------

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
        if (!r.ok) throw new Error('Error obteniendo preview');
        this.printRunsData = await r.json();
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
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
        // Nota: reutilizamos max_tier del payload. El "optimized" no viaja
        // al build-split-xml (que ya usa el greedy interno) — si el user
        // quiso optimized, se lo aplicaríamos ahí. Por ahora enviamos el
        // max_tier equivalente y aceptamos la diferencia si existe.
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
        if (!r.ok) throw new Error((await r.json()).detail || 'Error generando XMLs');
        const data = await r.json();
        window.toast(`${data.total_runs} XML${data.total_runs > 1 ? 's' : ''} generados`,
                     `Cartas: ${data.total_cards}. Descargables desde Historial.`);
        // Cerrar el modal y refrescar el historial de builds del mazo.
        this.closePrintRuns();
        // Si tenemos función de refresh de historial (buildHistoryOpen), la invocamos.
        if (typeof this.loadBuildHistory === 'function') {
          this.loadBuildHistory();
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this.printRunsBuilding = false;
      }
    },

    closePrintRuns() {
      this.printRunsOpen = false;
      this.printRunsData = null;
    },

    // ------ Extras · F2/T8: modal "Ver similares" (pHash) ------

    async openSimilar(art) {
      if (!art.image_hash) {
        window.toast('pHash', window._t('common_none'));
        return;
      }
      this.similarOpen = true;
      this.similarSourceArt = art;
      this.similarLoading = true;
      this.similarData = null;
      try {
        const r = await fetch(`/api/drives/phash/similar/${encodeURIComponent(art.file_id)}?threshold=8&limit=50`);
        if (!r.ok) throw new Error('Error buscando similares');
        this.similarData = await r.json();
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
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

    /**
     * El usuario selecciona uno de los similares — lo trata como si hubiera
     * elegido ese arte en el picker principal. Se llama a pickArt() con el
     * arte reconvertido a la estructura del picker.
     */
    async pickSimilar(similar) {
      // Reconstruir un "arte" tal como lo esperaría pickArt().
      const art = {
        kind: 'drive',
        source_id: similar.source_id,
        file_id: similar.file_id,
        image_small: `/api/drives/search`,  // se ignora
        __source: 'drives',
        image_hash: similar.image_hash,
        _drive_hit: similar,
      };
      // Aquí podemos delegar en la lógica del picker o simplemente cerrar.
      // Preferimos: cerrar similar modal y dejar al usuario re-buscar.
      this.closeSimilar();
      window.toast(window._t('common_ok'), `"${similar.filename}" (source ${similar.source_id})`);
    },

    // ------ Extras · F3/T11: "Aplicar arte de X a N cartas" ------

    async openArtistApply(art) {
      if (!art.artist) {
        window.toast(window._t('common_none'), window._t('picker_sort_artist'));
        return;
      }
      this.artistApplyOpen = true;
      this.artistApplySource = art;
      this.artistApplyArtist = art.artist;
      this.artistApplyLoading = true;
      this.artistApplyData = null;
      this.artistApplyChecked = {};   // {oracle_id: true/false}
      try {
        const r = await fetch(`/api/decks/${this.deckId}/recommend-by-artist`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({artist: art.artist, role: 'all'}),
        });
        if (!r.ok) throw new Error('Error consultando el recomendador');
        this.artistApplyData = await r.json();
        // Por defecto todas las coincidencias están marcadas
        for (const m of (this.artistApplyData.matched || [])) {
          this.artistApplyChecked[m.oracle_id] = true;
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
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

    /**
     * Aplica las impresiones seleccionadas del recomendador. Reutiliza el
     * endpoint change-art por-carta iterando (podría optimizarse con un
     * endpoint bulk en el futuro — ver TODO).
     */
    async applyArtistToSelected() {
      if (!this.artistApplyData || this.artistApplyApplying) return;
      const matched = (this.artistApplyData.matched || [])
        .filter(m => this.artistApplyChecked[m.oracle_id]);
      if (matched.length === 0) {
        window.toast(window._t('common_none'), window._t('deck_filter_placeholder'));
        return;
      }
      this.artistApplyApplying = true;
      let ok = 0, fail = 0;
      try {
        for (const m of matched) {
          try {
            // Reutilizamos change-art por oracle → scryfall_id
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
        window.toast(`${ok} ${window._T.history_kind_art}`,
                     fail > 0 ? `${fail} failed` : window._t('deck_art_updated_toast'));
        this.closeArtistApply();
        // Refrescar el mazo — el usuario verá los nuevos artes al recargar
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
        if (!r.ok) throw new Error(window._t('deck_error_adding'));
        const added = await r.json();
        if (added.length > 0) {
          window.toast(window._t('deck_tokens'), token.name);
          // Refrescar mazo y re-analizar (para actualizar el count del token)
          await this.load();
          await this.openTokens();
        }
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
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
        if (!r.ok) throw new Error(window._t('deck_error_adding'));
        const added = await r.json();
        window.toast(
          `${added.length} ${window._T.deck_tokens}`,
          added.map(c => c.name).slice(0, 3).join(', ') + (added.length > 3 ? '…' : '')
        );
        await this.load();
        await this.openTokens();
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this.tokensAdding = false;
      }
    },

    async openStats() {
      // Calculamos primero para tener los datos listos antes de que Alpine
      // pinte el modal. Los charts se dibujan en $nextTick porque los canvas
      // solo existen tras render.
      this.computeStats();
      this.statsOpen = true;
      // Lazy-load Chart.js si no está cargado ya. Ahorra ~80KB del initial
      // load en el 90% de sesiones donde el usuario no abre stats. Cached
      // por el navegador tras la primera vez.
      if (typeof window.Chart === 'undefined') {
        await new Promise((resolve, reject) => {
          const s = document.createElement('script');
          // Servido desde el propio bundle: el modal de stats funciona sin internet.
          s.src = '/static/vendor/chart.umd.js';
          s.crossOrigin = 'anonymous';
          s.onload = resolve;
          s.onerror = () => reject(new Error('No se pudo cargar Chart.js'));
          document.head.appendChild(s);
        }).catch(e => {
          window.toast(window._t('common_error'), e.message);
          this.statsOpen = false;
        });
        if (!this.statsOpen) return;
      }
      this.$nextTick(() => {
        this.renderCharts();
        window.icons?.();  // re-render iconos Lucide dentro del modal
      });
    },

    closeStats() {
      // Destruimos las instancias de Chart.js para no leak memoria si se
      // reabre el modal. Ojo con el orden:
      //   1) Primero destruimos los charts (mientras el canvas aún existe)
      //   2) Después escondemos el modal (x-if quita el canvas del DOM)
      // Si el orden fuese inverso, el canvas ya no existe y .destroy() peta.
      this._chartInstances.forEach(c => {
        try { c.stop(); c.destroy(); } catch (e) {}
      });
      this._chartInstances = [];
      // Doble RAF para asegurar que cualquier frame pendiente de Chart.js
      // se ejecute (o descarte) antes de quitar el canvas del DOM.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        this.statsOpen = false;
      }));
    },

    computeStats() {
      // Filtrar cartas: solo incluidas, y solo del commander + mainboard.
      // Sideboard, tokens, meld_result, maybeboard no cuentan en las stats.
      const relevantRoles = new Set(['commander', 'mainboard']);
      const cards = (this.cards || []).filter(c =>
        c.include && relevantRoles.has(c.role)
      );

      // Buckets de CMC. 7+ agrupa el "topdeck" (convención de deckbuilding).
      const CMC_BUCKETS = ['0', '1', '2', '3', '4', '5', '6', '7+'];
      const cmcBucket = (cmc) => cmc >= 7 ? '7+' : String(Math.floor(cmc || 0));

      // Curva de maná apilada por color dominante. Para cada carta no-tierra
      // elegimos UN color representativo (el primero que aparezca en su
      // color identity) — así una barra apilada suma exactamente el nº de
      // cartas, sin duplicar cartas multicolor.
      const CURVE_COLORS = ['W', 'U', 'B', 'R', 'G', 'M', 'C'];  // M=multicolor, C=incoloro
      const curveByColor = {};
      for (const col of CURVE_COLORS) {
        curveByColor[col] = Object.fromEntries(CMC_BUCKETS.map(b => [b, 0]));
      }

      // Métricas simples
      let landCount = 0;
      let basicLandCount = 0;
      let nonBasicLandCount = 0;
      let totalCmcNonLand = 0;
      let nonLandCount = 0;

      // Pips por color (para chart de colores) — como antes
      const colors = { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 };

      // Tipos y rareza — un contador por categoría
      const types = {};
      const rarity = {};
      // Keywords: {name → count de cartas que la tienen}
      // Contamos cartas únicas, no copias (una carta con quantity=4 cuenta 1)
      const keywords = {};

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
          // Bucket CMC
          const bucket = cmcBucket(c.cmc || 0);
          totalCmcNonLand += (c.cmc || 0) * qty;
          nonLandCount += qty;

          // Color dominante para apilar la barra:
          // - Si tiene 2+ colores en color_identity → 'M' (multicolor)
          // - Si tiene 1 → ese color
          // - Si tiene 0 → 'C' (incoloro)
          const ci = c.color_identity || [];
          let bucketColor;
          if (ci.length === 0) bucketColor = 'C';
          else if (ci.length === 1) bucketColor = ci[0];
          else bucketColor = 'M';
          curveByColor[bucketColor][bucket] += qty;
        }

        // Pips por color (para donut) — como antes, solo no-tierras con manaCost
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

        // Tipo principal
        let mainType = 'Other';
        for (const t of MAIN_TYPES) {
          if (new RegExp('\\b' + t + '\\b', 'i').test(typeLine)) {
            mainType = t; break;
          }
        }
        types[mainType] = (types[mainType] || 0) + qty;

        // Rareza
        const r = c.rarity || 'unknown';
        rarity[r] = (rarity[r] || 0) + qty;

        // Keywords — cuenta cartas únicas (no multiplica por qty)
        for (const kw of (c.keywords || [])) {
          if (!kw) continue;
          keywords[kw] = (keywords[kw] || 0) + 1;
        }
      }

      const totalCards = cards.reduce((s, c) => s + (c.quantity || 1), 0);
      const avgCmc = nonLandCount > 0 ? (totalCmcNonLand / nonLandCount).toFixed(2) : '—';
      const landPct = totalCards > 0 ? Math.round(100 * landCount / totalCards) : 0;

      // Top 12 keywords por número de cartas que las tienen (evita ensuciar
      // con las 30+ keywords que puede tener un mazo grande).
      const topKeywords = Object.entries(keywords)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 12)
        .map(([name, count]) => ({ name, count }));

      const summary = [
        { label: 'Total cartas', icon: 'library',
          value: totalCards, hint: `${cards.length} entradas únicas` },
        { label: 'Tierras', icon: 'trees',
          value: landCount,
          hint: landCount > 0 ? `${basicLandCount} básicas · ${nonBasicLandCount} no-básicas · ${landPct}% mazo` : null },
        { label: 'CMC medio', icon: 'trending-up',
          value: avgCmc, hint: 'sin contar tierras' },
        { label: 'No-tierras', icon: 'sparkles',
          value: nonLandCount, hint: nonLandCount > 0 ? `${100 - landPct}% del mazo` : null },
      ];

      this.stats = {
        totalCards, summary,
        curveByColor, cmcBuckets: CMC_BUCKETS,
        colors, types, rarity, topKeywords,
      };
    },

    // Extrae los símbolos de coste de una manaCost tipo "{2}{W}{U/B}".
    // Devuelve array de códigos: ["W", "U"]. Números y X se ignoran.
    // Híbridos como {U/B} cuentan ambos (aproximación razonable para stats).
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

    renderCharts() {
      // Paleta consistente con el tema arcane. Usamos rgba() para poder
      // aplicar transparencias en fondos de barras.
      const GOLD = '#d4af37';
      const GOLD_SOFT = 'rgba(212, 175, 55, 0.75)';
      const GRID = 'rgba(255, 255, 255, 0.06)';
      const FG_MUTED = '#a8a8b8';
      const FG_FAINT = '#6a6a80';

      // Colores MTG (mismos que en la paleta del tema)
      const MTG_COLORS = {
        W: '#f5f0d8', U: '#5b9bd5', B: '#3a3a4a',
        R: '#d9534f', G: '#5cb85c', C: '#8a8a9a',
      };

      // Chart.js: defaults globales para el tema oscuro
      Chart.defaults.color = FG_MUTED;
      Chart.defaults.font.family = 'ui-sans-serif, system-ui, sans-serif';
      Chart.defaults.font.size = 11;
      // Sin animaciones: evita que un requestAnimationFrame pendiente intente
      // dibujar sobre un canvas ya destruido cuando el usuario cierra el modal
      // rápido (click fuera / ESC) durante la animación inicial de aparición.
      Chart.defaults.animation = false;
      Chart.defaults.animations.colors = false;
      Chart.defaults.animations.x = false;
      Chart.defaults.animations.y = false;
      Chart.defaults.transitions.active.animation.duration = 0;

      const commonAxes = {
        grid: { color: GRID, drawBorder: false },
        ticks: { color: FG_MUTED },
      };

      // --- Curva de maná apilada por color (stacked bar) ---
      // Cada bucket CMC es una barra dividida en segmentos, uno por "color
      // dominante" de las cartas de ese coste (W/U/B/R/G/M/C).
      const CURVE_COLOR_META = [
        { key: 'W', label: 'Blanco',     color: '#f5f0d8' },
        { key: 'U', label: 'Azul',       color: '#5b9bd5' },
        { key: 'B', label: 'Negro',      color: '#4a4a5a' },
        { key: 'R', label: 'Rojo',       color: '#d9534f' },
        { key: 'G', label: 'Verde',      color: '#5cb85c' },
        { key: 'M', label: 'Multicolor', color: GOLD },
        { key: 'C', label: 'Incoloro',   color: '#8a8a9a' },
      ];
      const curveBuckets = this.stats.cmcBuckets;
      const curveDatasets = CURVE_COLOR_META.map(meta => ({
        label: meta.label,
        data: curveBuckets.map(b => this.stats.curveByColor[meta.key][b] || 0),
        backgroundColor: meta.color,
        borderColor: meta.color === '#f5f0d8' ? '#0a0d13' : 'rgba(0,0,0,0.15)',
        borderWidth: 1,
        borderRadius: 2,
        stack: 'curve',
      })).filter(ds => ds.data.some(v => v > 0));  // no pintar datasets vacíos

      this._chartInstances.push(new Chart(this.$refs.chartCurve, {
        type: 'bar',
        data: { labels: curveBuckets, datasets: curveDatasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'bottom',
              labels: { color: FG_MUTED, padding: 8, boxWidth: 12, font: { size: 10 } },
            },
            tooltip: {
              callbacks: {
                footer: (items) => {
                  const total = items.reduce((s, it) => s + it.parsed.y, 0);
                  return `Total CMC ${items[0].label}: ${total}`;
                },
              },
            },
          },
          scales: {
            x: { ...commonAxes, stacked: true, title: { display: true, text: 'CMC', color: FG_FAINT } },
            y: { ...commonAxes, stacked: true, beginAtZero: true, ticks: { ...commonAxes.ticks, stepSize: 1 } },
          },
        },
      }));

      // --- Colores (donut) ---
      const colorEntries = Object.entries(this.stats.colors).filter(([, v]) => v > 0);
      const colorLabels = colorEntries.map(([k]) => ({
        W: 'Blanco', U: 'Azul', B: 'Negro', R: 'Rojo', G: 'Verde', C: 'Incoloro',
      }[k]));
      const colorData = colorEntries.map(([, v]) => v);
      const colorBg = colorEntries.map(([k]) => MTG_COLORS[k]);
      this._chartInstances.push(new Chart(this.$refs.chartColors, {
        type: 'doughnut',
        data: {
          labels: colorLabels,
          datasets: [{
            data: colorData,
            backgroundColor: colorBg,
            borderColor: '#0a0d13',
            borderWidth: 2,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'right',
              labels: { color: FG_MUTED, padding: 8, boxWidth: 12, font: { size: 11 } },
            },
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  const total = colorData.reduce((s, v) => s + v, 0);
                  const pct = total > 0 ? Math.round(100 * ctx.parsed / total) : 0;
                  return `${ctx.label}: ${ctx.parsed} pips (${pct}%)`;
                },
              },
            },
          },
          cutout: '55%',
        },
      }));

      // --- Rareza (donut horizontal ordenado) ---
      // Orden canónico de MTG. Colores estándar del juego para reforzar la
      // convención visual (common=gris, uncommon=plateado, rare=dorado,
      // mythic=naranja-rojo, special=violeta, bonus=cian).
      const RARITY_META = [
        { key: 'common',    label: 'Common',    color: '#8a8a9a' },
        { key: 'uncommon',  label: 'Uncommon',  color: '#c0c8d0' },
        { key: 'rare',      label: 'Rare',      color: '#c9a55a' },
        { key: 'mythic',    label: 'Mythic',    color: '#d97539' },
        { key: 'special',   label: 'Special',   color: '#a06cd5' },
        { key: 'bonus',     label: 'Bonus',     color: '#5bc0be' },
        { key: 'unknown',   label: 'Desconocida', color: '#4a4a5a' },
      ];
      const rarityEntries = RARITY_META
        .map(m => ({ ...m, count: this.stats.rarity[m.key] || 0 }))
        .filter(e => e.count > 0);
      const rarityLabels = rarityEntries.map(e => e.label);
      const rarityData = rarityEntries.map(e => e.count);
      const rarityBg = rarityEntries.map(e => e.color);
      this._chartInstances.push(new Chart(this.$refs.chartRarity, {
        type: 'doughnut',
        data: {
          labels: rarityLabels,
          datasets: [{
            data: rarityData,
            backgroundColor: rarityBg,
            borderColor: '#0a0d13',
            borderWidth: 2,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'right',
              labels: { color: FG_MUTED, padding: 8, boxWidth: 12, font: { size: 11 } },
            },
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  const total = rarityData.reduce((s, v) => s + v, 0);
                  const pct = total > 0 ? Math.round(100 * ctx.parsed / total) : 0;
                  return `${ctx.label}: ${ctx.parsed} (${pct}%)`;
                },
              },
            },
          },
          cutout: '55%',
        },
      }));

      // --- Tipos (horizontal bar, ordenado descendente) ---
      const typeEntries = Object.entries(this.stats.types).sort((a, b) => b[1] - a[1]);
      const typeLabels = typeEntries.map(([k]) => k);
      const typeData = typeEntries.map(([, v]) => v);
      this._chartInstances.push(new Chart(this.$refs.chartTypes, {
        type: 'bar',
        data: {
          labels: typeLabels,
          datasets: [{
            label: 'Cartas',
            data: typeData,
            backgroundColor: GOLD_SOFT,
            borderColor: GOLD,
            borderWidth: 1,
            borderRadius: 4,
          }],
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ...commonAxes, beginAtZero: true, ticks: { ...commonAxes.ticks, stepSize: 1 } },
            y: { ...commonAxes, grid: { display: false } },
          },
        },
      }));
    },

    // ---- Helper ----
    _updateCard(updated) {
      const idx = this.cards.findIndex(c => c.id === updated.id);
      if (idx >= 0) this.cards[idx] = updated;
    },

    // ---- Polling de progreso de build ----
    // Arrancado por buildXml/buildPdf, parado al terminar (o si el usuario
    // navega fuera). Actualiza this.buildProgress con el snapshot del backend.
    _startBuildPolling() {
      if (this._buildPollTimer) return;
      this._buildPollTimer = setInterval(async () => {
        try {
          const r = await fetch(`/api/decks/${this.deckId}/build-progress`);
          if (!r.ok) return;
          const p = await r.json();
          // Si no está activo (build no arrancó aún, o el POST ya vino/limpió),
          // dejamos el estado anterior o lo limpiamos. Aquí solo pintamos
          // mientras haya cambios reales.
          if (p.active) this.buildProgress = p;
          if (p.done) this._stopBuildPolling();
        } catch (e) { /* silent */ }
      }, 300);  // 300ms — perceptible sin saturar
    },
    _stopBuildPolling() {
      if (this._buildPollTimer) {
        clearInterval(this._buildPollTimer);
        this._buildPollTimer = null;
      }
    },

    // ---- XML ----
    async buildXml() {
      this.building = true;
      this.buildProgress = null;
      // Arrancamos el polling ANTES del POST (el backend inicia el tracker
      // en cuanto entra al endpoint). El intervalo captura los ticks
      // conforme resolve_deck_for_xml los va emitiendo.
      this._startBuildPolling();
      try {
        const r = await fetch(`/api/decks/${this.deckId}/build-xml`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({create_run: true, cardstock: this.cardstock, foil: this.foil})
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const data = await r.json();
        const filename = data.xml_path.split(/[\\/]/).pop();
        this.lastXmlFilename = filename;
        window.toast(window._t('deck_xml'), `${data.total_cards} ${window._T.history_cards_count} · ${data.estimated_cost_eur.toFixed(2)} €`);
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this._stopBuildPolling();
        this.buildProgress = null;
        this.building = false;
      }
    },

    downloadXml() {
      if (!this.lastXmlFilename) return;
      window.location.href = `/api/exports/${this.lastXmlFilename}`;
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
        if (!r.ok) throw new Error(data.detail || 'Error');
        window.toast('MPC Autofill', `PID ${data.pid}`);
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this.launchingAutofill = false;
      }
    },

    async buildPdf() {
      // Quick-build: usa los últimos ajustes del PDF Studio (guardados por-mazo
      // en localStorage) si existen. Si el usuario nunca abrió el Studio,
      // envía payload vacío y el backend aplica los defaults del dataclass
      // (A4 3×3 con guías de esquina, sin bleed) — el equivalente al viejo
      // "1-click PDF" pero ya con el modelo nuevo. Para ajustes finos: PDF Studio.
      this.buildingPdf = true;
      this.buildProgress = null;
      this._startBuildPolling();
      let opts = {};
      try {
        const raw = localStorage.getItem(`pdfStudio.opts.${this.deckId}`);
        if (raw) opts = JSON.parse(raw);
      } catch (e) { /* fallback a defaults del backend */ }
      try {
        const r = await fetch(`/api/decks/${this.deckId}/build-pdf`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(opts),
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const data = await r.json();
        window.toast(window._t('deck_pdf_studio'), `${data.total_slots} ${window._T.history_cards_count} · ${data.total_pages} p.`);
        window.location.href = `/api/exports/${data.filename}`;
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this._stopBuildPolling();
        this.buildProgress = null;
        this.buildingPdf = false;
      }
    },

    // ---- Localización (idioma del arte) ----
    async localizeDeck() {
      if (!this.localizeLang) return;
      this.localizing = true;
      try {
        const r = await fetch(`/api/decks/${this.deckId}/localize`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({lang: this.localizeLang}),
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const data = await r.json();
        const langLabel = this.supportedLangs[data.lang] || data.lang;

        // Recargar el mazo para que aparezcan los nuevos artes/thumbnails
        await this.load();

        // Toast con resumen y detalle de las que no se pudieron localizar
        let msg = `${data.localized} cambiadas · ${data.unchanged} ya estaban`;
        if (data.skipped_custom > 0) {
          msg += ` · ${data.skipped_custom} con arte custom respetadas`;
        }
        if (data.unavailable && data.unavailable.length > 0) {
          // Mostramos las primeras 3-4 en el toast; el resto queda en la consola
          const shown = data.unavailable.slice(0, 4).join(', ');
          const more = data.unavailable.length > 4 ? ` (+${data.unavailable.length - 4} más)` : '';
          msg += `\nSin arte en ${langLabel}: ${shown}${more}`;
          console.log('[localize] Cartas sin arte en', langLabel, ':', data.unavailable);
        }
        Alpine.store('ui').success(`Idioma → ${langLabel}`, msg);
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this.localizing = false;
      }
    },

    // ---- Decklist como texto ----
    async _fetchDecklist() {
      // Devuelve {text, format, total_cards, filename} o null si falla.
      const params = new URLSearchParams({
        format: this.decklistFormat,
        include_headers: this.decklistHeaders ? 'true' : 'false',
      });
      const r = await fetch(`/api/decks/${this.deckId}/decklist?${params.toString()}`);
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.detail || 'No se pudo generar la decklist');
      }
      return await r.json();
    },
    async copyDecklist() {
      this.exportingDecklist = true;
      try {
        const data = await this._fetchDecklist();
        // Fallback si el navegador no soporta clipboard API o falla el permiso
        // (ocurre p.ej. si la página no está en https/localhost).
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
          window._t('deck_decklist_copy'),
          `${data.total_cards} ${window._T.history_cards_count} · ${data.format}`
        );
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        this.exportingDecklist = false;
      }
    },
    async downloadDecklist() {
      // No pasamos por _fetchDecklist porque el endpoint de descarga es distinto
      // (streamea el fichero directamente en lugar de devolver JSON).
      this.exportingDecklist = true;
      try {
        const params = new URLSearchParams({
          format: this.decklistFormat,
          include_headers: this.decklistHeaders ? 'true' : 'false',
        });
        window.location.href = `/api/decks/${this.deckId}/decklist.txt?${params.toString()}`;
      } catch (e) {
        window.toast(window._t('common_error'), e.message);
      } finally {
        // El browser lanza la descarga y no bloquea el hilo — reseteamos ya.
        setTimeout(() => { this.exportingDecklist = false; }, 500);
      }
    }
  }
}
// ---- Helpers globales para renderizar cartas ----------------------------

/** Devuelve el "tipo principal" para ordenación (Creature, Land, Instant…) */
function _mainType(typeLine) {
  const p = (typeLine || '').split('—')[0].trim();
  const priority = ['Creature', 'Planeswalker', 'Battle', 'Instant', 'Sorcery',
                    'Enchantment', 'Artifact', 'Land'];
  for (const t of priority) {
    if (p.includes(t)) return String(priority.indexOf(t)).padStart(2, '0') + '-' + t;
  }
  return '99-' + p;
}

/** Trunca el type_line para mostrarlo compacto en la lista. */
function shortType(typeLine) {
  if (!typeLine) return '';
  // "Legendary Creature — Elf Druid" → "Creature — Elf Druid"
  return typeLine
    .replace(/^Legendary\s+/, '')
    .replace(/^Basic\s+/, '')
    .replace(/^Snow\s+/, '')
    .replace(/^Elite\s+/, '')
    .replace(/^Host\s+/, '');
}

/** Borde izquierdo coloreado por rareza en la fila de carta. */
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

/** Clases Tailwind para la píldora de rareza (letra inicial). */
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

/** Letra inicial de rareza para la píldora. */
function rarityLetter(rarity) {
  const map = { mythic: 'M', rare: 'R', uncommon: 'U', common: 'C', special: 'S', bonus: 'B' };
  return map[rarity] || '?';
}

/** Renderiza {2}{B}{B} usando mana-font (símbolos oficiales MTG con fondos coloreados).
 *  {2}{U}{U} → <span class="mana-cost">
 *                <i class="ms ms-2 ms-cost"></i>
 *                <i class="ms ms-u ms-cost"></i>
 *                <i class="ms ms-u ms-cost"></i>
 *              </span>
 */
function renderManaCost(manaCost) {
  if (!manaCost) return '';
  const symbols = manaCost.match(/\{[^}]+\}/g) || [];
  const parts = symbols.map(sym => {
    // "{W/U}" → "wu", "{2/W}" → "2w", "{X}" → "x", "{W}" → "w", "{2}" → "2"
    const inner = sym.slice(1, -1).toLowerCase().replace(/\//g, '');
    return `<i class="ms ms-${inner} ms-cost"></i>`;
  });
  return `<span class="mana-cost">${parts.join('')}</span>`;
}


// --- Puente con Alpine -------------------------------------
// Alpine resuelve las expresiones de `x-data` contra el ámbito
// global, así que estas funciones tienen que estar en `window`.
window._mainType = _mainType
window.deckEditor = deckEditor
window.rarityBorderClass = rarityBorderClass
window.rarityChipClass = rarityChipClass
window.rarityLetter = rarityLetter
window.renderManaCost = renderManaCost
window.shortType = shortType
