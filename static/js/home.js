/**
 * Pantalla de inicio
 *
 * Extraído de `templates/index.html`, donde vivía como un bloque `<script>`
 * de 205 líneas. La lógica es idéntica: solo ha cambiado de fichero.
 *
 * Las funciones que Alpine necesita resolver desde los atributos `x-data` del
 * HTML se publican en `window` al final del módulo. Es deliberado: Alpine
 * evalúa `x-data` como una expresión en el ámbito global, así que un `export`
 * por sí solo no basta.
 *
 * Regenerar con:  python scripts/extract_inline_js.py
 */
function importPanel() {
  return {
    // ---- URL import unificado (Moxfield, Archidekt, TappedOut, MTGGoldfish, Scryfall, CubeCobra) ----
    urlInput: '',
    includeExtrasUrl: false,
    supportedSites: [],           // [{key, name, example_url, host_names}]
    urlPlaceholder: 'https://www.moxfield.com/decks/XXXXXXXXXX',

    // ---- Plain text ----
    deckName: '',
    deckText: '',
    includeExtrasText: false,

    loading: false,

    // Reporte de cartas no importadas — se muestra en un modal tras el import
    // cuando hay al menos una entrada que Scryfall no resolvió.
    unresolvedModal: {
      open: false,
      deckId: null,
      deckName: '',
      unresolved: [],  // [{name, quantity, raw_line, set, number, reason}]
      resolvedCount: 0,
      totalEntries: 0,
    },

    async loadSites() {
      try {
        const r = await fetch('/api/decks/import/supported-sites');
        if (!r.ok) return;
        this.supportedSites = await r.json();
        // Rotamos el placeholder entre ejemplos cada vez que se carga la home.
        if (this.supportedSites.length > 0) {
          const pick = this.supportedSites[Math.floor(Math.random() * this.supportedSites.length)];
          if (pick.example_url) this.urlPlaceholder = pick.example_url;
        }
      } catch (e) { /* silent — el import sigue funcionando aunque no pintemos chips */ }
    },

    // Getter: nombre del sitio detectado en el input, o null si no matchea.
    // Detección puramente local (mismo criterio que resolve_site en backend),
    // sirve solo para dar feedback visual antes del submit.
    get urlSiteHint() {
      const raw = (this.urlInput || '').trim();
      if (!raw) return null;
      let host;
      try {
        host = new URL(raw).hostname.toLowerCase();
      } catch (_) { return null; }
      const stripped = host.startsWith('www.') ? host.slice(4) : host;
      for (const site of this.supportedSites) {
        for (const h of (site.host_names || [])) {
          const hh = h.toLowerCase();
          const hStripped = hh.startsWith('www.') ? hh.slice(4) : hh;
          if (host === hh || stripped === hStripped) return site.name;
        }
      }
      return null;
    },

    async _handleImportResult(data) {
      // data = ImportResult (schema del backend). Si hay no-resueltas, abre
      // el modal para que el usuario las revise antes de navegar al mazo.
      // Si todo salió bien, navega directamente.
      if (data.unresolved && data.unresolved.length > 0) {
        this.unresolvedModal = {
          open: true,
          deckId: data.deck.id,
          deckName: data.deck.name,
          unresolved: data.unresolved,
          resolvedCount: data.resolved_count,
          totalEntries: data.total_entries,
        };
        this.loading = false;
      } else {
        window.location.href = `/decks/${data.deck.id}`;
      }
    },

    async importUrl() {
      if (!this.urlInput.trim()) return;
      this.loading = true;
      try {
        const r = await fetch('/api/decks/import/url', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            url: this.urlInput.trim(),
            include_extras: this.includeExtrasUrl,
          })
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const data = await r.json();
        await this._handleImportResult(data);
      } catch (e) {
        Alpine.store('ui').error(window._t('js_error_importing'), e.message);
        this.loading = false;
      }
    },
    async importText() {
      if (!this.deckName.trim() || !this.deckText.trim()) return;
      this.loading = true;
      try {
        const r = await fetch('/api/decks/import/text', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name: this.deckName, text: this.deckText, include_extras: this.includeExtrasText,
          })
        });
        if (!r.ok) throw new Error((await r.json()).detail || 'Error');
        const data = await r.json();
        await this._handleImportResult(data);
      } catch (e) {
        Alpine.store('ui').error(window._t('js_error_creating'), e.message);
        this.loading = false;
      }
    },

    _unresolvedAsText() {
      // Reconstruye una lista pegable con la línea original si la teníamos
      // (import de texto), o "N Nombre" si venía de una URL (Moxfield,
      // Archidekt, etc.).
      return this.unresolvedModal.unresolved.map(u => {
        if (u.raw_line) return u.raw_line;
        return `${u.quantity} ${u.name}`;
      }).join('\n');
    },

    async copyUnresolved() {
      const text = this._unresolvedAsText();
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      Alpine.store('ui').success(window._t('js_copied'), `${this.unresolvedModal.unresolved.length} ${window._t('js_entries_clipboard')}`);
    },

    continueToDeck() {
      if (this.unresolvedModal.deckId) {
        window.location.href = `/decks/${this.unresolvedModal.deckId}`;
      }
    },

    closeUnresolvedAndReload() {
      // El mazo se ha creado igual (con las cartas que sí resolvieron), así que
      // refrescamos la lista principal para que aparezca. El usuario puede
      // entrar a editarlo cuando quiera.
      this.unresolvedModal.open = false;
      window.location.reload();
    },
  }
}

function deckListActions() {
  return {
    async rename(id, oldName) {
      const newName = prompt(window._t('js_new_deck_name_prompt'), oldName);
      if (!newName || newName.trim() === oldName) return;
      const r = await fetch(`/api/decks/${id}`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: newName.trim()})
      });
      if (r.ok) location.reload();
      else Alpine.store('ui').error(window._t('common_error'), window._t('js_error_rename'));
    },
    async duplicate(id, originalName) {
      const suggested = `${originalName} ${window._t('js_deck_copy_suffix')}`;
      const newName = prompt(window._t('js_duplicate_prompt'), suggested);
      if (newName === null) return;
      const trimmed = (newName || '').trim();
      const r = await fetch(`/api/decks/${id}/duplicate`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: trimmed || null})
      });
      if (r.ok) {
        const data = await r.json();
        window.toast(window._t('js_deck_duplicated'), `"${data.name}" — ${data.cards.length} ${window._t('js_deck_created_cards')}`);
        window.location.href = `/decks/${data.id}`;
      } else {
        const err = await r.json().catch(() => ({}));
        Alpine.store('ui').error(window._t('common_error'), err.detail || window._t('js_error_duplicating'));
      }
    },
    async del(id, name) {
      const msg = window._t('js_delete_deck_confirm').replace('{name}', name);
      if (!await window.confirmDialog(
        window._t('home_deck_delete'),
        msg,
        { danger: true, icon: 'trash-2', confirmLabel: window._t('common_delete') })) return;
      const r = await fetch(`/api/decks/${id}`, {method: 'DELETE'});
      if (r.ok) location.reload();
      else Alpine.store('ui').error(window._t('common_error'), window._t('js_error_deleting'));
    }
  }
}


// --- Puente con Alpine -------------------------------------
// Alpine resuelve las expresiones de `x-data` contra el ámbito
// global, así que estas funciones tienen que estar en `window`.
window.deckListActions = deckListActions
window.importPanel = importPanel
