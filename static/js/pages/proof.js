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
        commander: 'Comandante',
        mainboard: 'Mazo',
        companion: 'Compañero',
        sideboard: 'Sideboard',
        maybeboard: 'Maybeboard',
        tokens: 'Tokens',
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
              name: c.name + ' (reverso)',
              thumbnail: c.thumbnail_url,
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
        const r = await fetch(`/api/decks/${this.deckId}`);
        this.deck = await r.json();
      } finally {
        this.loading = false;
      }
    }
  }
}

window.proofView = proofView;
