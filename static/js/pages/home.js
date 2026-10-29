function importPanel() {
  return {
    urlInput: '',
    includeExtrasUrl: false,
    supportedSites: [],
    urlPlaceholder: 'https://www.moxfield.com/decks/XXXXXXXXXX',

    deckName: '',
    deckText: '',
    includeExtrasText: false,

    loading: false,

    unresolvedModal: {
      open: false,
      deckId: null,
      deckName: '',
      unresolved: [],
      resolvedCount: 0,
      totalEntries: 0,
    },

    async loadSites() {
      try {
        const r = await fetch('/api/decks/import/supported-sites');
        if (!r.ok) return;
        this.supportedSites = await r.json();
        if (this.supportedSites.length > 0) {
          const pick = this.supportedSites[Math.floor(Math.random() * this.supportedSites.length)];
          if (pick.example_url) this.urlPlaceholder = pick.example_url;
        }
      } catch (e) {}
    },

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
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
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
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const data = await r.json();
        await this._handleImportResult(data);
      } catch (e) {
        Alpine.store('ui').error(window._t('js_error_creating'), e.message);
        this.loading = false;
      }
    },

    _unresolvedAsText() {
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

    async acceptSuggestion(index, name) {
      const entry = this.unresolvedModal.unresolved[index];
      if (!entry || entry.adding) return;
      entry.adding = true;
      try {
        const r = await fetch(`/api/decks/${this.unresolvedModal.deckId}/cards`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, quantity: entry.quantity, role: entry.role || 'mainboard' }),
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        this.unresolvedModal.unresolved.splice(index, 1);
        this.unresolvedModal.resolvedCount += entry.quantity;
        Alpine.store('ui').success(window._t('home_unresolved_added'), name);
        if (!this.unresolvedModal.unresolved.length) this.continueToDeck();
      } catch (e) {
        entry.adding = false;
        Alpine.store('ui').error(window._t('common_error'), e.message);
      }
    },

    continueToDeck() {
      if (this.unresolvedModal.deckId) {
        window.location.href = `/decks/${this.unresolvedModal.deckId}`;
      }
    },

    closeUnresolvedAndReload() {
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
        body: JSON.stringify({name: trimmed || suggested})
      });
      if (r.ok) {
        const data = await r.json();
        window.toast(window._t('js_deck_duplicated'), `"${data.name}" — ${data.cards.length} ${window._t('js_deck_created_cards')}`, 'success');
        window.location.href = `/decks/${data.id}`;
      } else {
        Alpine.store('ui').error(window._t('js_error_duplicating'), await window.apiErrorDetail(r));
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

const LIBRARY_SORT_KEY = 'deckLibrary.sort'

function deckLibrary() {
  return {
    ...deckListActions(),
    decks: [],
    q: '',
    format: '',
    tag: '',
    colors: [],
    sort: 'updated',
    selecting: false,
    selected: [],

    init() {
      try { this.decks = JSON.parse(this.$el.dataset.decks || '[]') } catch (_) { this.decks = [] }
      try { this.sort = localStorage.getItem(LIBRARY_SORT_KEY) || 'updated' } catch (_) {}
      this.$watch('sort', (v) => { try { localStorage.setItem(LIBRARY_SORT_KEY, v) } catch (_) {} })
      window.addEventListener('keydown', (e) => {
        if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return
        const tag = (e.target && e.target.tagName) || ''
        if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (e.target && e.target.isContentEditable)) return
        e.preventDefault()
        this.$refs.search?.focus()
      })
    },

    get total() { return this.decks.length },
    get formats() { return [...new Set(this.decks.map((d) => d.format).filter(Boolean))].sort() },
    get tags() {
      const seen = new Map()
      for (const d of this.decks) for (const t of d.tags) {
        if (!seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t)
      }
      return [...seen.values()].sort((a, b) => a.localeCompare(b))
    },
    get hasFilters() { return !!(this.q.trim() || this.format || this.tag || this.colors.length) },

    _matches(d) {
      const q = this.q.trim().toLowerCase()
      if (q) {
        const hay = [d.name, d.commander, d.format, ...d.tags].join(' ').toLowerCase()
        if (!q.split(/\s+/).every((word) => hay.includes(word.replace(/^#/, '')))) return false
      }
      if (this.format && d.format !== this.format) return false
      if (this.tag && !d.tags.some((t) => t.toLowerCase() === this.tag.toLowerCase())) return false
      if (this.colors.length && !this.colors.every((c) => d.colors.includes(c))) return false
      return true
    },

    get visibleIds() {
      return this.decks.filter((d) => this._matches(d)).map((d) => d.id)
    },
    get visibleCount() { return this.visibleIds.length },
    get order() {
      const list = [...this.decks]
      const byName = (a, b) => a.name.localeCompare(b.name)
      const sorters = {
        updated: (a, b) => b.updated.localeCompare(a.updated),
        name: byName,
        cards: (a, b) => (b.cards - a.cards) || byName(a, b),
        format: (a, b) => a.format.localeCompare(b.format) || byName(a, b),
      }
      list.sort(sorters[this.sort] || sorters.updated)
      return new Map(list.map((d, i) => [d.id, i]))
    },
    isVisible(id) { return this.visibleIds.includes(id) },
    orderStyle(id) { return `order: ${this.order.get(id) ?? 0}` },

    toggleColor(c) {
      this.colors = this.colors.includes(c) ? this.colors.filter((x) => x !== c) : [...this.colors, c]
    },
    clearFilters() { this.q = ''; this.format = ''; this.tag = ''; this.colors = [] },

    toggleSelecting() { this.selecting = !this.selecting; if (!this.selecting) this.selected = [] },
    isSelected(id) { return this.selected.includes(id) },
    toggleSelected(id) {
      this.selected = this.isSelected(id) ? this.selected.filter((x) => x !== id) : [...this.selected, id]
    },
    selectAllVisible() { this.selected = [...this.visibleIds] },

    _deck(id) { return this.decks.find((d) => d.id === id) },

    async _saveTags(id, tags) {
      const updated = await window.api.decks.update(id, { tags })
      const d = this._deck(id)
      if (d) d.tags = updated.tags
      return updated.tags
    },

    async editTags(id) {
      const d = this._deck(id)
      const answer = prompt(window._t('library_edit_tags_prompt'), (d?.tags || []).join(', '))
      if (answer === null) return
      const tags = answer.split(',').map((t) => t.trim()).filter(Boolean)
      try {
        await this._saveTags(id, tags)
        window.location.reload()
      } catch (e) {
        Alpine.store('ui').error(window._t('common_error'), e.detail || e.message)
      }
    },

    async bulkTag() {
      const answer = prompt(window._t('library_bulk_tag_prompt').replace('{n}', this.selected.length), '')
      const tag = (answer || '').trim()
      if (!tag) return
      try {
        for (const id of this.selected) {
          const d = this._deck(id)
          await this._saveTags(id, [...(d?.tags || []), tag])
        }
        window.location.reload()
      } catch (e) {
        Alpine.store('ui').error(window._t('common_error'), e.detail || e.message)
      }
    },

    async bulkDelete() {
      const n = this.selected.length
      if (!n) return
      const ok = await window.confirmDialog(
        window._t('library_bulk_delete_title'),
        window._t('library_bulk_delete_confirm').replace('{n}', n),
        { danger: true, icon: 'trash-2', confirmLabel: window._t('common_delete') })
      if (!ok) return
      try {
        for (const id of this.selected) await window.api.decks.remove(id)
      } catch (e) {
        Alpine.store('ui').error(window._t('common_error'), e.detail || e.message)
      }
      window.location.reload()
    },
  }
}

window.deckLibrary = deckLibrary
window.deckListActions = deckListActions
window.importPanel = importPanel
