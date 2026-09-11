/**
 * Historial y línea de tiempo
 *
 * Extraído de `templates/history.html`, donde vivía como un bloque `<script>`
 * de 328 líneas. La lógica es idéntica: solo ha cambiado de fichero.
 *
 * Las funciones que Alpine necesita resolver desde los atributos `x-data` del
 * HTML se publican en `window` al final del módulo. Es deliberado: Alpine
 * evalúa `x-data` como una expresión en el ámbito global, así que un `export`
 * por sí solo no basta.
 *
 * Regenerar con:  python scripts/extract_inline_js.py
 */
// ============================================================================
// PÁGINA DE HISTORIAL
// ============================================================================
// Toda la vista es un componente Alpine. Los datos se cargan vía fetch a los
// tres endpoints: /api/decks/_/with-activity, /api/runs y /api/decks/{id}/activity.
//
// El renderizado del timeline usa una tabla de metadata (KIND_META) por tipo
// con icono, tono (color) y renderer opcional. Los eventos sin renderer caen
// al summary pre-computado por el backend — así añadir un tipo nuevo solo
// requiere una entrada en KIND_META y un caso en el backend.
// ============================================================================

// Escape de HTML — usado en TODOS los renderers para no inyectar el payload
// crudo (viene del usuario indirectamente: nombres de mazo, de carta, etc.)
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Metadata por tipo de evento. Añadir uno nuevo: agrega entrada aquí + emite
// desde el backend con el mismo string. Sin renderer → usa event.summary.
function buildKindMeta() {
  const _t = window._t || ((k) => k);
  return {
  deck_created:         { icon: 'sparkles',        tone: 'accent',  label: _t('history_kind_created'),
    render: (ev) => {
      const p = ev.payload || {};
      const srcMap = { moxfield: 'Moxfield', text: _t('history_filter_meta'), manual: 'manual' };
      const src = srcMap[p.source] || p.source || 'manual';
      let msg = `${_t('history_kind_created')}: <b>${esc(src)}</b> · <b>${p.card_count || 0}</b> ${_t('history_cards_count')}`;
      if (p.unresolved_count) msg += ` <span class="text-warning">(${p.unresolved_count} unresolved)</span>`;
      return msg;
    }
  },
  deck_renamed:         { icon: 'pencil',          tone: 'info',    label: _t('history_kind_renamed'),
    render: (ev) => {
      const p = ev.payload || {};
      return `${_t('history_kind_renamed')}: <span class="text-fg-muted">${esc(p.old_name || '')}</span> → <b>${esc(p.new_name || '')}</b>`;
    }
  },
  deck_localized:       { icon: 'languages',       tone: 'info',    label: _t('history_kind_localized'),
    render: (ev) => {
      const p = ev.payload || {};
      let msg = `${_t('history_kind_localized')}: <b>${esc(p.lang || '?')}</b> · ${p.localized || 0} ${_t('history_cards_count')}`;
      if (p.unchanged) msg += `, ${p.unchanged} unchanged`;
      if (p.unavailable && p.unavailable.length) {
        msg += ` · <span class="text-warning">${p.unavailable.length} unavailable</span>`;
      }
      if (p.skipped_custom) msg += ` · ${p.skipped_custom} custom`;
      return msg;
    }
  },
  card_added:           { icon: 'plus-circle',     tone: 'success', label: _t('history_kind_added'),
    render: (ev) => {
      const p = ev.payload || {};
      const stacked = p.stacked ? ' <span class="text-fg-faint">(stacked)</span>' : '';
      return `${_t('history_kind_added')} <b>${p.quantity || 1}×</b> ${esc(ev.card_name || 'card')} → <b>${esc(p.role || 'mainboard')}</b>${stacked}`;
    }
  },
  card_removed:         { icon: 'minus-circle',    tone: 'danger',  label: _t('history_kind_removed'),
    render: (ev) => {
      const p = ev.payload || {};
      return `${_t('history_kind_removed')} <b>${p.quantity || 1}×</b> ${esc(ev.card_name || 'card')} ← <b>${esc(p.role || 'mainboard')}</b>`;
    }
  },
  card_moved:           { icon: 'arrow-right-left',tone: 'info',    label: _t('history_kind_moved'),
    render: (ev) => {
      const p = ev.payload || {};
      return `${_t('history_kind_moved')} <b>${esc(ev.card_name || 'card')}</b>: <span class="text-fg-muted">${esc(p.from_role || '?')}</span> → <b>${esc(p.to_role || '?')}</b>`;
    }
  },
  card_qty_changed:     { icon: 'hash',            tone: 'info',    label: _t('history_kind_qty'),
    render: (ev) => {
      const p = ev.payload || {};
      return `${_t('history_kind_qty')} <b>${esc(ev.card_name || 'card')}</b>: <span class="text-fg-muted">${p.old_qty || 0}×</span> → <b>${p.new_qty || 0}×</b>`;
    }
  },
  card_include_toggled: { icon: 'eye',             tone: 'neutral', label: _t('history_kind_include'),
    render: (ev) => {
      const p = ev.payload || {};
      const verb = p.new_include ? 'Restored' : 'Excluded from XML';
      return `${verb}: <b>${esc(ev.card_name || 'card')}</b>`;
    }
  },
  card_art_changed:     { icon: 'image',           tone: 'accent',  label: _t('history_kind_art'),
    render: (ev) => {
      const p = ev.payload || {};
      if (p.kind === 'custom') {
        let msg = `${_t('history_kind_art')} <b>${esc(ev.card_name || 'card')}</b> → custom`;
        if (p.custom_variant) msg += ` <span class="text-fg-muted">"${esc(p.custom_variant)}"</span>`;
        if (p.custom_filename) msg += ` <span class="text-fg-faint text-[11px]">(${esc(p.custom_filename)})</span>`;
        return msg;
      }
      const fromLoc = p.old_set ? `${esc((p.old_set||'').toUpperCase())} ${esc(p.old_number || '')}` : '?';
      const toLoc   = p.new_set ? `${esc((p.new_set||'').toUpperCase())} ${esc(p.new_number || '')}` : '?';
      const remember = p.remember_globally ? ` <span class="text-accent/70 text-[11px]">(${_t('deck_art_remembered')})</span>` : '';
      return `${_t('history_kind_art')} <b>${esc(ev.card_name || 'card')}</b>: <span class="text-fg-muted font-mono">${fromLoc}</span> → <b class="font-mono">${toLoc}</b>${remember}`;
    }
  },
  role_cleared:         { icon: 'trash-2',         tone: 'danger',  label: _t('history_kind_cleared'),
    render: (ev) => {
      const p = ev.payload || {};
      const sample = (p.card_names_sample || []).slice(0, 5).map(esc).join(', ');
      const more = p.truncated ? ` +${(p.deleted || 0) - (p.card_names_sample || []).length} more` : '';
      let msg = `${_t('history_kind_cleared')}: <b>${esc(p.role || '?')}</b> — <b>${p.deleted || 0}</b> ${_t('history_cards_count')}`;
      if (sample) msg += ` <span class="text-fg-faint text-[11px]">(${sample}${more})</span>`;
      return msg;
    }
  },
  related_added:        { icon: 'plus-square',     tone: 'success', label: _t('history_kind_related'),
    render: (ev) => {
      const p = ev.payload || {};
      const trigger = p.trigger_card ? ` for <b>${esc(p.trigger_card)}</b>` : '';
      const kind = p.kind === 'tokens' ? 'tokens' : 'related cards';
      return `${_t('history_kind_added')} <b>${p.count || 0}</b> ${kind}${trigger}`;
    }
  },
  xml_generated:        { icon: 'file-code',       tone: 'accent',  label: 'XML',
    render: (ev) => {
      const p = ev.payload || {};
      const foil = p.foil ? ' foil' : '';
      return `XML: <b>${p.total_cards || 0}</b> ${_t('history_cards_count')} · <b>${esc(p.cardstock || '?')}</b>${foil} · <b>${(p.estimated_cost_eur || 0).toFixed(2)} €</b> <span class="text-fg-faint text-[11px]">(${esc(p.xml_filename || '')})</span>`;
    }
  },
  pdf_generated:        { icon: 'file-text',       tone: 'accent',  label: 'PDF',
    render: (ev) => {
      const p = ev.payload || {};
      const opts = [
        p.page_size ? p.page_size.toUpperCase() : null,
        p.cut_marks ? 'cut marks' : null,
        p.include_backs ? 'with backs' : null,
      ].filter(Boolean).join(' · ');
      return `PDF: <b>${p.total_slots || 0}</b> ${_t('history_cards_count')} in <b>${p.total_pages || 0}</b> pages <span class="text-fg-muted">(${esc(opts)})</span> <span class="text-fg-faint text-[11px]">(${esc(p.pdf_filename || '')})</span>`;
    }
  },
  };
}

// FILTER_GROUPS se construye lazy la primera vez que se llama a historyPage()
// para que _t() ya tenga window._T disponible.
function buildFilterGroups() {
  const _t = window._t || ((k) => k);
  return [
    { value: 'all',     label: _t('history_filter_all'),     icon: null,            kinds: null },
    { value: 'cards',   label: _t('history_filter_cards'),   icon: 'square-stack',  kinds: ['card_added','card_removed','card_moved','card_qty_changed','card_include_toggled','related_added','role_cleared'] },
    { value: 'art',     label: _t('history_filter_art'),     icon: 'image',         kinds: ['card_art_changed'] },
    { value: 'lang',    label: _t('history_filter_lang'),    icon: 'languages',     kinds: ['deck_localized'] },
    { value: 'exports', label: _t('history_filter_exports'), icon: 'printer',       kinds: ['xml_generated','pdf_generated'] },
    { value: 'meta',    label: _t('history_filter_meta'),    icon: 'settings',      kinds: ['deck_created','deck_renamed'] },
  ];
}


function historyPage() {
  const KIND_META = buildKindMeta();
  const FILTER_GROUPS = buildFilterGroups();
  return {
    loading: true,
    decks: [],
    runs: [],

    // Modal timeline
    timelineOpen: false,
    timelineDeck: null,
    timelineLoading: false,
    activity: [],
    activeFilter: 'all',

    filterOptions: FILTER_GROUPS.map(g => ({ ...g, count: undefined })),

    // Undo. Cargamos la lista de kinds reversibles al montar (una única vez).
    // Fallback pesimista: si el fetch falla, ninguno es reversible.
    undoableKinds: [],
    undoingEventId: null,

    get totalActivityCount() {
      return this.decks.reduce((sum, d) => sum + (d.activity_count || 0), 0);
    },

    // El filtro se computa client-side sobre el array ya cargado.
    get filteredActivity() {
      const g = FILTER_GROUPS.find(f => f.value === this.activeFilter);
      if (!g || !g.kinds) return this.activity;
      return this.activity.filter(e => g.kinds.includes(e.kind));
    },

    async load() {
      this.loading = true;
      try {
        const [decksR, runsR, undoR] = await Promise.all([
          fetch('/api/decks/_/with-activity'),
          fetch('/api/runs'),
          fetch('/api/decks/_/undoable-kinds'),
        ]);
        this.decks = decksR.ok ? await decksR.json() : [];
        this.runs = runsR.ok ? await runsR.json() : [];
        this.undoableKinds = undoR.ok ? await undoR.json() : [];
      } catch (e) {
        window.toast && window.toast(window._t('history_error_loading'), e.message);
      } finally {
        this.loading = false;
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    async openTimeline(deck) {
      this.timelineDeck = deck;
      this.timelineOpen = true;
      this.timelineLoading = true;
      this.activity = [];
      this.activeFilter = 'all';
      try {
        const r = await fetch(`/api/decks/${deck.id}/activity?limit=500`);
        if (r.ok) this.activity = await r.json();
      } catch (e) {
        window.toast && window.toast(window._t('history_error_timeline'), e.message);
      } finally {
        this.timelineLoading = false;
        // Actualizar counts en las pills de filtro
        this.filterOptions = FILTER_GROUPS.map(g => ({
          ...g,
          count: g.kinds === null ? this.activity.length
                                  : this.activity.filter(e => g.kinds.includes(e.kind)).length,
        }));
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    closeTimeline() {
      this.timelineOpen = false;
      this.timelineDeck = null;
      this.activity = [];
    },

    // ---- Helpers de render ----

    iconForKind(kind) {
      return (KIND_META[kind] && KIND_META[kind].icon) || 'circle';
    },

    toneClass(kind) {
      const meta = KIND_META[kind];
      const tone = meta ? meta.tone : 'neutral';
      const map = {
        accent:  'bg-accent/10 text-accent border-accent/30',
        info:    'bg-info-bg text-info border-info-border',
        success: 'bg-success-bg text-success border-success-border',
        danger:  'bg-danger-bg text-danger border-danger-border',
        warning: 'bg-warning-bg text-warning border-warning-border',
        neutral: 'bg-bg-subtle text-fg-muted border-border-subtle',
      };
      return map[tone] || map.neutral;
    },

    renderEvent(ev) {
      const meta = KIND_META[ev.kind];
      if (meta && meta.render) {
        try {
          return meta.render(ev);
        } catch (e) {
          console.warn('Render failed for', ev.kind, e);
          return esc(ev.summary || ev.kind);
        }
      }
      return esc(ev.summary || ev.kind);
    },

    // ---- Undo ----
    canUndo(ev) {
      if (!this.undoableKinds.includes(ev.kind)) return false;
      if (ev.payload && ev.payload.undone_event_id) return false;
      return true;
    },

    async undoEvent(ev) {
      if (this.undoingEventId) return;
      if (!this.canUndo(ev)) return;
      this.undoingEventId = ev.id;
      try {
        const r = await fetch(
          `/api/decks/${ev.deck_id}/activity/${ev.id}/undo`,
          { method: 'POST' }
        );
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          const detail = data.detail || window._t('history_undo_failed');
          window.toast && window.toast(window._t('history_undo_failed'), detail);
          return;
        }
        window.toast && window.toast(window._t('history_undone'), data.summary);
        if (this.timelineDeck) {
          await this.openTimeline(this.timelineDeck);
        }
        fetch('/api/decks/_/with-activity').then(async r => {
          if (r.ok) this.decks = await r.json();
        });
      } catch (e) {
        window.toast && window.toast(window._t('history_undo_error'), e.message);
      } finally {
        this.undoingEventId = null;
      }
    },

    // Relative time — locale-aware via window._LANG
    relativeTime(iso) {
      if (!iso) return '';
      const then = new Date(iso).getTime();
      const now = Date.now();
      const diff = Math.max(0, Math.round((now - then) / 1000));
      const _t = window._t || ((k) => k);
      if (diff < 60)    return _t('history_relative_min').replace('{n}', diff + 's');
      if (diff < 3600)  return _t('history_relative_min').replace('{n}', Math.round(diff / 60));
      if (diff < 86400) return _t('history_relative_h').replace('{n}', Math.round(diff / 3600));
      if (diff < 604800)return _t('history_relative_d').replace('{n}', Math.round(diff / 86400));
      const locale = window._LANG === 'en' ? 'en-US' : 'es-ES';
      return new Date(iso).toLocaleDateString(locale, { day: '2-digit', month: 'short' });
    },

    formatDate(iso) {
      if (!iso) return '';
      const locale = window._LANG === 'en' ? 'en-US' : 'es-ES';
      return new Date(iso).toLocaleString(locale, { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    },
  };
}


// --- Puente con Alpine -------------------------------------
// Alpine resuelve las expresiones de `x-data` contra el ámbito
// global, así que estas funciones tienen que estar en `window`.
window.buildFilterGroups = buildFilterGroups
window.buildKindMeta = buildKindMeta
window.esc = esc
window.historyPage = historyPage
