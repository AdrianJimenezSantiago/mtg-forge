function globalCardSearch() {
  return {
    query: '',
    loading: false,
    results: [],
    totalGroups: 0,
    totalInstances: 0,
    totalDecks: 0,
    open: false,
    _lastQuery: '',
    pos: { top: 0, left: 0 },

    init() {
      const nav = this.$el.closest('nav');
      if (nav) nav.addEventListener('scroll', () => { if (this.open) this.place(); }, { passive: true });
    },

    place() {
      const input = this.$refs.input;
      if (!input) return;
      const r = input.getBoundingClientRect();
      const aside = input.closest('aside');
      const left = (aside ? aside.getBoundingClientRect().right : r.right) + 8;
      const maxTop = window.innerHeight * 0.30 - 8;
      this.pos = { top: Math.round(Math.max(8, Math.min(r.top - 28, maxTop))), left: Math.round(left) };
    },

    async search() {
      const q = this.query.trim();
      if (q.length < 2) {
        this.results = [];
        this.totalGroups = 0;
        this.totalInstances = 0;
        this.totalDecks = 0;
        this.open = false;
        return;
      }
      this._lastQuery = q;
      this.loading = true;
      try {
        const r = await fetch(`/api/decks/_/search-cards?q=${encodeURIComponent(q)}`);
        if (!r.ok) throw new Error('Error del servidor');
        const data = await r.json();
        if (q !== this._lastQuery) return;
        this.results = data.groups || [];
        this.totalGroups = data.total_groups || 0;
        this.totalInstances = data.total_instances || 0;
        const uniqueDecks = new Set();
        for (const g of this.results) {
          for (const inst of g.instances) uniqueDecks.add(inst.deck_id);
        }
        this.totalDecks = uniqueDecks.size;
        this.place();
        this.open = true;
        this.$nextTick(() => window.icons && window.icons());
      } catch (e) {
        window.toast && window.toast('Error buscando', e.message);
      } finally {
        this.loading = false;
      }
    },

    clearSearch() {
      this.query = '';
      this.results = [];
      this.totalGroups = 0;
      this.totalInstances = 0;
      this.totalDecks = 0;
      this.open = false;
    },
  };
}

function sidebarData() {
  const CACHE_KEY = 'mpc-sidebar';
  let cached = null;
  try { cached = JSON.parse(sessionStorage.getItem(CACHE_KEY) || 'null'); } catch (_) {}
  return {
    stats: cached && cached.stats ? cached.stats : null,
    recentDecks: cached && Array.isArray(cached.recentDecks) ? cached.recentDecks : [],
    async load() {
      try {
        const [statsR, decksR] = await Promise.all([
          fetch('/api/collection/sidebar-stats'),
          fetch('/api/collection/recent-decks'),
        ]);
        if (statsR.ok) this.stats = await statsR.json();
        if (decksR.ok) this.recentDecks = await decksR.json();
        try {
          sessionStorage.setItem(CACHE_KEY, JSON.stringify({
            stats: this.stats, recentDecks: this.recentDecks,
          }));
        } catch (_) {}
        this.$nextTick(() => window.icons && window.icons());
      } catch (e) {
        console.warn('Sidebar data load failed:', e);
      }
    }
  };
}

window.globalCardSearch = globalCardSearch;
window.sidebarData = sidebarData;
