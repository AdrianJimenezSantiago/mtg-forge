function collectionApp() {
  return {
    sets: [],
    loadingSets: true,
    setSearch: '',
    selectedSet: null,
    cards: [],
    loadingCards: false,
    cardFilter: 'all',
    globalOwned: 0,
    setsStarted: 0,
    totalCopies: 0,
    importing: false,
    importMode: 'merge',
    setOwnedCount: 0,

    cardWidth: 210,
    perPage: 60,
    currentPage: 1,

    get filteredSets() {
      const q = this.setSearch.toLowerCase().trim();
      if (!q) return this.sets;
      return this.sets.filter(s =>
        s.name.toLowerCase().includes(q) ||
        s.code.toLowerCase().includes(q)
      );
    },

    get filteredCards() {
      if (this.cardFilter === 'owned')   return this.cards.filter(c => c.owned);
      if (this.cardFilter === 'missing') return this.cards.filter(c => !c.owned);
      return this.cards;
    },

    get totalPages() {
      return Math.max(1, Math.ceil(this.filteredCards.length / this.perPage));
    },

    get paginatedCards() {
      const start = (this.currentPage - 1) * this.perPage;
      return this.filteredCards.slice(start, start + this.perPage);
    },

    get pageNumbers() {
      const total = this.totalPages;
      const cur = this.currentPage;
      if (total <= 7) return Array.from({length: total}, (_, i) => i + 1);
      const pages = [];
      pages.push(1);
      if (cur > 3) pages.push('...');
      for (let i = Math.max(2, cur - 1); i <= Math.min(total - 1, cur + 1); i++) {
        pages.push(i);
      }
      if (cur < total - 2) pages.push('...');
      pages.push(total);
      return pages;
    },

    goPage(p) {
      if (p < 1 || p > this.totalPages) return;
      this.currentPage = p;
      const el = document.getElementById('card-grid-scroll');
      if (el) el.scrollTop = 0;
      this.$nextTick(() => window.icons && window.icons());
    },

    async loadSets() {
      this.loadingSets = true;
      this.loadStats();
      try {
        const r = await fetch('/api/collection/sets');
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        this.sets = await r.json();
        this.globalOwned = this.sets.reduce((sum, s) => sum + s.owned_count, 0);
        this.setsStarted = this.sets.filter(s => s.owned_count > 0).length;
      } catch (e) {
        console.error('Failed to load sets:', e);
        window.toast && window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.loadingSets = false;
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    async selectSet(set) {
      this.selectedSet = set;
      this.setOwnedCount = set.owned_count;
      this.cardFilter = 'all';
      this.currentPage = 1;
      this.loadingCards = true;
      this.cards = [];
      try {
        const r = await fetch(`/api/collection/sets/${set.code}/cards`);
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        this.cards = await r.json();
        this.setOwnedCount = this.cards.filter(c => c.owned).length;
      } catch (e) {
        console.error('Failed to load set cards:', e);
        window.toast && window.toast(window._t('common_error'), e.message, 'error');
      } finally {
        this.loadingCards = false;
        this.$nextTick(() => window.icons && window.icons());
      }
    },

    async loadStats() {
      try {
        const stats = await window.api.collection.stats();
        this.totalCopies = stats.total_copies;
      } catch (_) {}
    },

    _cardPayload(card) {
      return {
        scryfall_id: card.scryfall_id,
        oracle_id: card.oracle_id,
        name: card.name,
        set_code: this.selectedSet.code,
        set_name: this.selectedSet.name,
        collector_number: card.collector_number,
        rarity: card.rarity,
        image_small: card.image_small,
      };
    },

    _applySetCount(count) {
      this.setOwnedCount = count;
      if (this.selectedSet) {
        this.selectedSet.owned_count = count;
        const idx = this.sets.findIndex(s => s.code === this.selectedSet.code);
        if (idx >= 0) this.sets[idx].owned_count = count;
      }
      this.globalOwned = this.sets.reduce((sum, s) => sum + s.owned_count, 0);
      this.setsStarted = this.sets.filter(s => s.owned_count > 0).length;
    },

    async changeQuantity(card, kind, delta) {
      const next = { quantity: card.quantity || 0, foil_quantity: card.foil_quantity || 0 };
      next[kind] = Math.max(0, next[kind] + delta);
      try {
        const data = await window.apiRequest('POST', '/api/collection/set-quantity', {
          body: { ...this._cardPayload(card), ...next },
        });
        card.quantity = data.quantity;
        card.foil_quantity = data.foil_quantity;
        card.owned = data.owned;
        this._applySetCount(data.set_owned_count);
        this.loadStats();
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error');
      }
    },

    async importCsv(ev) {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (!file) return;
      const replace = this.importMode === 'replace';
      if (replace && !await window.confirmDialog(window._t('coll_import_mode_title'),
        window._t('coll_import_mode_body'),
        { danger: true, icon: 'upload', confirmLabel: window._t('coll_import_replace') })) return;
      this.importing = true;
      try {
        const body = new FormData();
        body.append('file', file);
        body.append('mode', replace ? 'replace' : 'merge');
        const r = await fetch('/api/collection/import', { method: 'POST', body });
        if (!r.ok) throw new window.ApiError(r.status, await window.apiErrorDetail(r), r.url);
        const rep = await r.json();
        let msg = window._t('coll_import_done_body')
          .replace('{cards}', rep.cards).replace('{printings}', rep.printings)
          .replace('{added}', rep.added).replace('{updated}', rep.updated);
        if (rep.unresolved.length) {
          msg += ' ' + window._t('coll_import_unresolved').replace('{n}', rep.unresolved.length)
            + ' ' + rep.unresolved.slice(0, 5).map(u => u.name).join(', ');
        }
        window.toast(window._t('coll_import_done'), msg, rep.unresolved.length ? 'warning' : 'success');
        await this.loadSets();
        this.loadStats();
        if (this.selectedSet) await this.selectSet(this.sets.find(s => s.code === this.selectedSet.code) || this.selectedSet);
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error');
      } finally {
        this.importing = false;
      }
    },

    exportCsv() {
      window.saveFile('/api/collection/export.csv', 'mpc-forge-collection.csv');
    },

    async toggleCard(card) {
      const wasOwned = card.owned;
      card.owned = !card.owned;
      this.setOwnedCount += card.owned ? 1 : -1;
      this.globalOwned += card.owned ? 1 : -1;

      try {
        const r = await fetch('/api/collection/toggle-owned', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            scryfall_id: card.scryfall_id,
            oracle_id: card.oracle_id,
            name: card.name,
            set_code: this.selectedSet.code,
            set_name: this.selectedSet.name,
            collector_number: card.collector_number,
            rarity: card.rarity,
            image_small: card.image_small,
          }),
        });
        if (!r.ok) throw new Error(await window.apiErrorDetail(r));
        const data = await r.json();
        card.quantity = data.owned ? 1 : 0;
        card.foil_quantity = 0;
        this._applySetCount(data.set_owned_count);
        this.loadStats();
      } catch (e) {
        card.owned = wasOwned;
        this.setOwnedCount += wasOwned ? 1 : -1;
        this.globalOwned += wasOwned ? 1 : -1;
        console.error('Toggle failed:', e);
      }
    },
  };
}

window.collectionApp = collectionApp
