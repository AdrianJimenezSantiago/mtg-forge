/**
 * Colección por expansión
 *
 * Extraído de `templates/collection.html`, donde vivía como un bloque `<script>`
 * de 149 líneas. La lógica es idéntica: solo ha cambiado de fichero.
 *
 * Las funciones que Alpine necesita resolver desde los atributos `x-data` del
 * HTML se publican en `window` al final del módulo. Es deliberado: Alpine
 * evalúa `x-data` como una expresión en el ámbito global, así que un `export`
 * por sí solo no basta.
 *
 * Regenerar con:  python scripts/extract_inline_js.py
 */
function collectionApp() {
  return {
    // ── State ──
    sets: [],
    loadingSets: true,
    setSearch: '',
    selectedSet: null,
    cards: [],
    loadingCards: false,
    cardFilter: 'all',
    globalOwned: 0,
    setsStarted: 0,
    setOwnedCount: 0,

    // ── Sizing & pagination ──
    cardWidth: 210,    // px — default: readable size
    perPage: 60,       // cards per page — recalculated dynamically
    currentPage: 1,

    // ── Computed ──
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

    // ── Actions ──
    goPage(p) {
      if (p < 1 || p > this.totalPages) return;
      this.currentPage = p;
      // Scroll card grid back to top
      const el = document.getElementById('card-grid-scroll');
      if (el) el.scrollTop = 0;
      this.$nextTick(() => window.icons && window.icons());
    },

    async loadSets() {
      this.loadingSets = true;
      try {
        const r = await fetch('/api/collection/sets');
        if (!r.ok) throw new Error('Failed to load sets');
        this.sets = await r.json();
        this.globalOwned = this.sets.reduce((sum, s) => sum + s.owned_count, 0);
        this.setsStarted = this.sets.filter(s => s.owned_count > 0).length;
      } catch (e) {
        console.error('Failed to load sets:', e);
        window.toast && window.toast('Error', e.message, 'error');
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
        if (!r.ok) throw new Error('Failed to load cards');
        this.cards = await r.json();
        this.setOwnedCount = this.cards.filter(c => c.owned).length;
      } catch (e) {
        console.error('Failed to load set cards:', e);
        window.toast && window.toast('Error', e.message, 'error');
      } finally {
        this.loadingCards = false;
        this.$nextTick(() => window.icons && window.icons());
      }
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
        if (!r.ok) throw new Error('Toggle failed');
        const data = await r.json();
        this.setOwnedCount = data.set_owned_count;
        if (this.selectedSet) {
          this.selectedSet.owned_count = data.set_owned_count;
          const idx = this.sets.findIndex(s => s.code === this.selectedSet.code);
          if (idx >= 0) this.sets[idx].owned_count = data.set_owned_count;
        }
        // Recompute global
        this.globalOwned = this.sets.reduce((sum, s) => sum + s.owned_count, 0);
        this.setsStarted = this.sets.filter(s => s.owned_count > 0).length;
      } catch (e) {
        card.owned = wasOwned;
        this.setOwnedCount += wasOwned ? 1 : -1;
        this.globalOwned += wasOwned ? 1 : -1;
        console.error('Toggle failed:', e);
      }
    },
  };
}


// --- Puente con Alpine -------------------------------------
// Alpine resuelve las expresiones de `x-data` contra el ámbito
// global, así que estas funciones tienen que estar en `window`.
window.collectionApp = collectionApp
