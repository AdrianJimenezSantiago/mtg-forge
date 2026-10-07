function proofView(deckId) {
  return {
    deckId,
    deck: null,
    loading: true,
    cardWidth: 100,
    showBacks: false,

    get totalSlots() {
      if (!this.deck) return 0;
      return this.deck.cards.filter(c => c.include).reduce((n, c) => n + c.quantity, 0);
    },

    get groupedSlots() {
      if (!this.deck) return [];
      const labels = {
        commander: window._t('deck_role_commander'),
        mainboard: window._t('deck_role_mainboard'),
        companion: window._t('deck_role_companion'),
        sideboard: window._t('deck_role_sideboard'),
        maybeboard: window._t('deck_role_maybeboard'),
        tokens: window._t('deck_tokens'),
      };
      const order = ['commander', 'mainboard', 'companion', 'sideboard', 'maybeboard', 'tokens'];
      const groups = {};

      for (const c of this.deck.cards) {
        if (!c.include) continue;
        if (!groups[c.role]) groups[c.role] = [];
        for (let i = 1; i <= c.quantity; i++) {
          groups[c.role].push({
            key: `${c.id}-front-${i}`,
            name: c.name,
            thumbnail: c.thumbnail_url,
            is_dfc: c.is_dfc,
            face: 'front',
            copy_index: i,
            quantity: c.quantity,
          });
          if (this.showBacks && c.is_dfc) {
            groups[c.role].push({
              key: `${c.id}-back-${i}`,
              name: `${c.back_name || c.name} ${window._t('proof_back_suffix')}`,
              thumbnail: c.back_thumbnail_url || c.thumbnail_url,
              is_dfc: true,
              face: 'back',
              copy_index: i,
              quantity: c.quantity,
            });
          }
        }
      }
      return order
        .filter(r => groups[r] && groups[r].length)
        .map(r => ({
          role: r,
          label: labels[r] || r,
          slots: groups[r],
          total: groups[r].length,
        }));
    },

    async load() {
      this.loading = true;
      try {
        this.deck = await window.api.decks.get(this.deckId);
      } catch (e) {
        window.toast?.(window._t('proof_load_error'), e.message, 'error');
      } finally {
        this.loading = false;
      }
    }
  }
}

window.proofView = proofView;
