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

window.deckListActions = deckListActions
window.importPanel = importPanel
