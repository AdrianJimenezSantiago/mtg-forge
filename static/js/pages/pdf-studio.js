const LS_KEY = (id) => `pdfStudio.opts.v2.${id}`;
// La escala depende de la impresora, no del mazo: se guarda una vez para todos.
const LS_PRINT_SCALE = 'pdfStudio.printScale';

// Lo mismo que dicen los botones de presets del panel izquierdo.
const PRESET_LABELS = {
  mpc_classic: 'MPC classic',
  mpc_bleed: 'MPC with bleed',
  duplex_dfc: 'Duplex DFC',
  duplex_all: 'Duplex all-backs',
  silhouette: 'Silhouette (reg. marks)',
};

// Hueco del preview resaltado mientras se arrastra una imagen. Vive fuera del
// estado de Alpine: un nodo DOM dentro de un proxy reactivo rompe setAttribute.
let dropTarget = null;

function pdfStudio(deckId) { return {
  deckId,
  deck: null,
  cardbackSettings: null,
  buildProgress: null,
  buildingPdf: false,
  exportingImages: false,
  autoOpen: true,
  zoom: 60,
  showPageMargins: false,

  cardbackModalOpen: false,
  cbUrlInput: '',
  cbAddingUrl: false,
  cbDriveQuery: '',
  cbDriveHits: [],
  cbDriveSearching: false,
  cbDefaultHits: [],
  cbDefaultLoading: false,
  cbLocalArts: [],
  cbPickingId: null,

  miniPickerOpen: false,
  miniPickerCard: null,
  miniPickerFace: 'front',
  miniPickerArts: [],
  miniPickerLoading: false,
  miniPickerBusy: false,
  miniPickerBusyKey: null,

  guideOpen: false,
  guideSection: null,

  // Modo imprenta: el preview y la exportación siguen la plantilla de una
  // imprenta (una carta por página, anverso y reverso) en vez de la hoja de casa.
  pressShops: [],
  press: null,
  exportingPress: false,
  pressResult: null,
  pressError: null,

  opts: {
    page_size: 'a4',
    orientation: 'portrait',
    cols: 3, rows: 3,
    gap_x_mm: 0, gap_y_mm: 0,
    offset_x_mm: 0, offset_y_mm: 0,
    back_offset_x_mm: 0, back_offset_y_mm: 0,
    bleed_enabled: false, bleed_mm: 0,
    card_guides_enabled: true,
    card_guides_style: 'corners',
    card_guides_shape: 'square',
    card_guides_pattern: 'solid',
    card_guides_placement: 'outside',
    card_guides_length_mm: 4.0,
    card_guides_color: '#606060',
    card_guides_width_pt: 0.4,
    page_guides: 'none',
    hide_card_guides_front: false,
    hide_card_guides_back: false,
    hide_page_guides_front: false,
    hide_page_guides_back: false,
    reg_marks_enabled: false,
    reg_marks_inset_mm: 10,
    reg_marks_size_mm: 5,
    include_backs: false,
    backs_layout: 'append',
    backs_content: 'all_cards',
    backs_compact_fill: true,
    page_range: '',
    show_footer: true,
    print_scale_x_pct: 100,
    print_scale_y_pct: 100,
  },

  async init() {
    this._loadOpts();
    this.$watch('opts', () => this._saveOpts(), { deep: true });
    this.$watch('zoom', () => this._saveVolatile());
    try {
      const r = await fetch(`/api/decks/${this.deckId}`);
      if (r.ok) this.deck = await r.json();
    } catch (e) {
      console.error('deck fetch:', e);
    }
    try {
      const rs = await fetch(`/api/decks/${this.deckId}/cardback-settings`);
      if (rs.ok) this.cardbackSettings = await rs.json();
    } catch (e) {}
    try {
      const rp = await fetch('/api/print-shops');
      if (rp.ok) this.pressShops = await rp.json();
    } catch (e) {}
    if (this.press && !this.pressSpec) this.press = null;
    if (window.lucide) window.lucide.createIcons();
    this.$nextTick(() => window.lucide && window.lucide.createIcons());
  },

  _loadOpts() {
    try {
      const raw = localStorage.getItem(LS_KEY(this.deckId));
      if (raw) {
        const saved = JSON.parse(raw);
        Object.assign(this.opts, saved.opts || {});
        if (typeof saved.zoom === 'number') this.zoom = saved.zoom;
        if (typeof saved.showPageMargins === 'boolean') this.showPageMargins = saved.showPageMargins;
        if (typeof saved.press === 'string') this.press = saved.press;
      }
    } catch (e) {}
    try {
      const scale = JSON.parse(localStorage.getItem(LS_PRINT_SCALE) || 'null');
      if (scale && typeof scale.x === 'number' && typeof scale.y === 'number') {
        this.opts.print_scale_x_pct = scale.x;
        this.opts.print_scale_y_pct = scale.y;
      }
    } catch (e) {}
  },

  _saveOpts() {
    try {
      localStorage.setItem(LS_KEY(this.deckId), JSON.stringify({
        opts: this.opts, zoom: this.zoom, showPageMargins: this.showPageMargins,
        press: this.press,
      }));
      localStorage.setItem(LS_PRINT_SCALE, JSON.stringify({
        x: this.opts.print_scale_x_pct, y: this.opts.print_scale_y_pct,
      }));
    } catch (e) {}
  },

  get printScaleActive() {
    return this.opts.print_scale_x_pct !== 100 || this.opts.print_scale_y_pct !== 100;
  },

  resetPrintScale() {
    this.opts.print_scale_x_pct = 100;
    this.opts.print_scale_y_pct = 100;
  },
  _saveVolatile() { this._saveOpts(); },

  get uniqueCards() {
    if (!this.deck) return 0;
    return (this.deck.cards || []).filter(c => c.include).length;
  },
  get totalSlots() {
    if (!this.deck) return 0;
    return (this.deck.cards || [])
      .filter(c => c.include)
      .reduce((sum, c) => sum + (c.quantity || 1), 0);
  },
  get pxPerMm() { return 3.7795 * (this.zoom / 100); },

  get pressSpec() {
    return this.press ? (this.pressShops.find(s => s.key === this.press) || null) : null;
  },

  _pressGeom(spec) {
    return {
      page_w_mm: spec.page_w_mm, page_h_mm: spec.page_h_mm,
      cols: 1, rows: 1, slot_w_mm: spec.page_w_mm, slot_h_mm: spec.page_h_mm,
      gap_x_mm: 0, gap_y_mm: 0,
      grid_w_mm: spec.page_w_mm, grid_h_mm: spec.page_h_mm,
      origin_x_mm: 0, origin_y_mm: 0,
      bleed_mm: spec.bleed_mm,
    };
  },

  geomForKind(kind) {
    if (this.pressSpec) return this._pressGeom(this.pressSpec);
    const o = this.opts;
    const sizes = { a4: [210, 297], letter: [215.9, 279.4], a3: [297, 420] };
    let [pw, ph] = sizes[o.page_size] || sizes.a4;
    if (o.orientation === 'landscape') [pw, ph] = [ph, pw];

    const bleed = o.bleed_enabled ? o.bleed_mm : 0;
    const slotW = 63.0 + 2 * bleed;
    const slotH = 88.0 + 2 * bleed;

    const cols = o.cols > 0 ? o.cols : Math.max(1, Math.floor((pw + o.gap_x_mm) / (slotW + o.gap_x_mm)));
    const rows = o.rows > 0 ? o.rows : Math.max(1, Math.floor((ph + o.gap_y_mm) / (slotH + o.gap_y_mm)));

    const gridW = cols * slotW + (cols - 1) * o.gap_x_mm;
    const gridH = rows * slotH + (rows - 1) * o.gap_y_mm;

    const backX = kind === 'back' ? o.back_offset_x_mm : 0;
    const backY = kind === 'back' ? o.back_offset_y_mm : 0;
    const originX = (pw - gridW) / 2 + o.offset_x_mm + backX;
    const originY = (ph - gridH) / 2 - o.offset_y_mm - backY;

    return {
      page_w_mm: pw, page_h_mm: ph,
      cols, rows, slot_w_mm: slotW, slot_h_mm: slotH,
      gap_x_mm: o.gap_x_mm, gap_y_mm: o.gap_y_mm,
      grid_w_mm: gridW, grid_h_mm: gridH,
      origin_x_mm: originX, origin_y_mm: originY,
      bleed_mm: bleed,
    };
  },
  get geom() { return this.geomForKind('front'); },

  get allSlots() {
    if (!this.deck) return { fronts: [], backs: [] };
    // La imprenta imprime a doble cara: toda carta lleva reverso.
    const fillAllBacks = !!this.pressSpec || this.opts.backs_content === 'all_cards';
    const cardback = (this.cardbackSettings && this.cardbackSettings.image_url)
                  || (this.cardbackSettings && this.cardbackSettings.default_image_url)
                  || null;
    const fronts = [], backs = [];
    for (const c of (this.deck.cards || [])) {
      if (!c.include) continue;
      const qty = c.quantity || 1;
      for (let i = 0; i < qty; i++) {
        fronts.push({
          name: c.name, thumbnail: c.thumbnail_url || null,
          card_id: c.id, face: 'front', kind: 'card-front',
        });
        if (c.is_dfc && c.back_thumbnail_url) {
          backs.push({
            name: c.back_name || c.name, thumbnail: c.back_thumbnail_url,
            card_id: c.id, face: 'back', kind: 'card-back',
          });
        } else if (fillAllBacks && cardback) {
          backs.push({ name: 'Cardback', thumbnail: cardback, kind: 'cardback' });
        } else if (fillAllBacks) {
          backs.push({ name: 'Cardback', thumbnail: null, kind: 'cardback' });
        } else {
          backs.push(null);
        }
      }
    }
    return { fronts, backs };
  },

  get allPages() {
    const g = this.geom;
    const per = g.cols * g.rows;
    if (per <= 0 || !this.deck) return [];
    const { fronts, backs } = this.allSlots;
    const pages = [];

    if (this.pressSpec) {
      for (let i = 0; i < fronts.length; i++) {
        pages.push({ key: `pf${i}`, kind: 'front', mirror: false, card: i + 1,
                     chunk: this._chunkWithPositions([fronts[i]], g) });
        pages.push({ key: `pb${i}`, kind: 'back', mirror: false, card: i + 1,
                     chunk: this._chunkWithPositions([backs[i]], g) });
      }
      return pages;
    }

    const frontChunks = [];
    for (let i = 0; i < fronts.length; i += per) frontChunks.push(fronts.slice(i, i + per));

    if (this.opts.include_backs && this.opts.backs_layout === 'duplex') {
      for (let i = 0; i < frontChunks.length; i++) {
        pages.push({
          key: `f${i}`, kind: 'front', mirror: false,
          chunk: this._chunkWithPositions(frontChunks[i], g),
        });
        const bChunk = backs.slice(i * per, i * per + per);
        if (bChunk.some(b => b !== null)) {
          pages.push({
            key: `bd${i}`, kind: 'back', mirror: true,
            chunk: this._chunkWithPositions(bChunk, this.geomForKind('back'),  true),
          });
        }
      }
    } else if (this.opts.include_backs && this.opts.backs_layout === 'append') {
      const realBacks = backs.filter(b => b !== null);
      let bIdx = 0;
      if (this.opts.backs_compact_fill && frontChunks.length) {
        const last = frontChunks[frontChunks.length - 1];
        const free = per - last.length;
        if (free > 0 && realBacks.length > 0) {
          const take = Math.min(free, realBacks.length);
          frontChunks[frontChunks.length - 1] = last.concat(realBacks.slice(0, take));
          bIdx = take;
        }
      }
      for (let i = 0; i < frontChunks.length; i++) {
        pages.push({
          key: `f${i}`, kind: 'front', mirror: false,
          chunk: this._chunkWithPositions(frontChunks[i], g),
        });
      }
      const remaining = realBacks.slice(bIdx);
      const gBack = this.geomForKind('back');
      for (let i = 0, j = 0; i < remaining.length; i += per, j++) {
        pages.push({
          key: `ba${j}`, kind: 'back', mirror: false,
          chunk: this._chunkWithPositions(remaining.slice(i, i + per), gBack),
        });
      }
    } else {
      for (let i = 0; i < frontChunks.length; i++) {
        pages.push({
          key: `f${i}`, kind: 'front', mirror: false,
          chunk: this._chunkWithPositions(frontChunks[i], g),
        });
      }
    }
    return pages;
  },

  _chunkWithPositions(chunk, g, mirror = false) {
    const out = [];
    for (let i = 0; i < chunk.length; i++) {
      const slot = chunk[i];
      if (!slot) { out.push(null); continue; }
      const row = Math.floor(i / g.cols);
      let col = i % g.cols;
      if (mirror) col = g.cols - 1 - col;
      const x = g.origin_x_mm + col * (g.slot_w_mm + g.gap_x_mm);
      const y = g.origin_y_mm + row * (g.slot_h_mm + g.gap_y_mm);
      out.push({ ...slot, x_mm: x, y_mm: y, key: `${i}-${slot.name}` });
    }
    return out;
  },

  get totalPages() { return this.allPages.length; },

  _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  },
  _n(v) { return (Math.round(v * 1000) / 1000).toString(); },

  svgMarkupForPage(page) {
    const g = this.geomForKind(page.kind);
    const n = this._n.bind(this);
    const esc = this._esc.bind(this);
    const o = this.opts;
    const parts = [];

    parts.push(
      `<svg xmlns="http://www.w3.org/2000/svg" ` +
      `viewBox="0 0 ${n(g.page_w_mm)} ${n(g.page_h_mm)}" ` +
      `preserveAspectRatio="xMidYMid meet" ` +
      `style="width:100%;height:100%;display:block;background:#fff;">`
    );

    if (this.pressSpec) {
      parts.push(this._pressOverlaySvg(page, g, this.pressSpec));
      parts.push(`</svg>`);
      return parts.join('');
    }

    if (this.showPageMargins) {
      parts.push(
        `<rect x="${n(g.origin_x_mm)}" y="${n(g.origin_y_mm)}" ` +
        `width="${n(g.grid_w_mm)}" height="${n(g.grid_h_mm)}" ` +
        `fill="none" stroke="#3b82f6" stroke-width="0.15" stroke-dasharray="1,1" opacity="0.5"/>`
      );
    }

    for (const slot of page.chunk) {
      if (slot) parts.push(this._slotSvg(slot, g));
    }

    if (o.bleed_enabled && g.bleed_mm > 0) {
      for (const slot of page.chunk) {
        if (!slot) continue;
        parts.push(`<path d="${this._bleedPath(slot, g)}" fill="rgba(212,175,55,0.12)"/>`);
      }
    }

    const showCardGuides = o.card_guides_enabled &&
      !(page.kind === 'front' && o.hide_card_guides_front) &&
      !(page.kind === 'back' && o.hide_card_guides_back);
    if (showCardGuides) {
      const sw = o.card_guides_width_pt * 0.353;
      const dash = this._dashArray(o.card_guides_pattern);
      const cap = o.card_guides_pattern === 'dotted' ? ' stroke-linecap="round"' : '';
      parts.push(
        `<g stroke="${esc(o.card_guides_color)}" stroke-width="${n(sw)}"${dash}${cap} fill="none">`
      );
      if (o.card_guides_style === 'full') {
        parts.push(this._cardFullRectsSvg(g, o));
      } else {
        parts.push(this._cardCornerMarksSvg(g, o));
      }
      parts.push(`</g>`);
    }

    const showPageGuides = o.page_guides !== 'none' &&
      !(page.kind === 'front' && o.hide_page_guides_front) &&
      !(page.kind === 'back' && o.hide_page_guides_back);
    if (showPageGuides) {
      const sw = o.card_guides_width_pt * 0.353;
      const dash = this._dashArray(o.card_guides_pattern);
      const cap = o.card_guides_pattern === 'dotted' ? ' stroke-linecap="round"' : '';
      parts.push(
        `<g stroke="${esc(o.card_guides_color)}" stroke-width="${n(sw)}"${dash}${cap} fill="none">`
      );
      if (o.page_guides === 'full_lines') {
        parts.push(this._pageFullLinesSvg(g));
      } else {
        parts.push(this._pageCornerLinesSvg(g, o));
      }
      parts.push(`</g>`);
    }

    if (o.reg_marks_enabled) {
      parts.push(`<g fill="#000">`);
      const s = o.reg_marks_size_mm;
      for (const [cx, cy] of [
        [o.reg_marks_inset_mm, o.reg_marks_inset_mm],
        [g.page_w_mm - o.reg_marks_inset_mm, o.reg_marks_inset_mm],
        [o.reg_marks_inset_mm, g.page_h_mm - o.reg_marks_inset_mm],
      ]) {
        parts.push(`<rect x="${n(cx - s/2)}" y="${n(cy - s/2)}" width="${n(s)}" height="${n(s)}"/>`);
      }
      parts.push(`</g>`);
    }

    parts.push(`</svg>`);
    return parts.join('');
  },

  _slotSvg(slot, g) {
    const n = this._n.bind(this);
    const esc = this._esc.bind(this);
    const parts = [];
    const clickAttrs =
      slot.kind === 'cardback'
        ? ` data-slot-kind="cardback" style="cursor:pointer"`
        : slot.card_id != null
          ? ` data-slot-kind="${esc(slot.kind)}" data-card-id="${slot.card_id}" ` +
            `data-face="${esc(slot.face)}" style="cursor:pointer"`
          : '';
    const imgX = slot.x_mm + g.bleed_mm;
    const imgY = slot.y_mm + g.bleed_mm;
    const imgW = g.slot_w_mm - 2 * g.bleed_mm;
    const imgH = g.slot_h_mm - 2 * g.bleed_mm;
    if (slot.thumbnail) {
      parts.push(
        `<image href="${esc(slot.thumbnail)}" ` +
        `x="${n(imgX)}" y="${n(imgY)}" ` +
        `width="${n(imgW)}" height="${n(imgH)}" ` +
        `preserveAspectRatio="none"${clickAttrs}/>`
      );
    } else {
      parts.push(
        `<rect x="${n(imgX)}" y="${n(imgY)}" ` +
        `width="${n(imgW)}" height="${n(imgH)}" ` +
        `fill="#242a3a" stroke="#3a4358" stroke-width="0.1"${clickAttrs}/>` +
        `<text x="${n(imgX + imgW / 2)}" ` +
        `y="${n(imgY + imgH / 2)}" ` +
        `text-anchor="middle" fill="#a8a8b8" font-size="4" style="pointer-events:none">${esc(slot.name)}</text>`
      );
    }
    if (clickAttrs) {
      parts.push(
        `<rect x="${n(slot.x_mm)}" y="${n(slot.y_mm)}" ` +
        `width="${n(g.slot_w_mm)}" height="${n(g.slot_h_mm)}" ` +
        `fill="transparent"${clickAttrs}>` +
        `<title>${esc(slot.kind === 'cardback' ? window._t('pdf_change_cardback') : window._t('pdf_mini_picker_title') + ' — ' + slot.name)}</title>` +
        `</rect>`
      );
    }
    return parts.join('');
  },

  // Como la plantilla de la imprenta: sangrado sombreado, línea de corte
  // punteada con sus esquinas y zona segura. Solo existe en el preview: el PDF
  // de imprenta no lleva marcas.
  _pressOverlaySvg(page, g, spec) {
    const n = this._n.bind(this);
    const parts = [];
    const slot = page.chunk[0];
    if (slot) {
      // El PDF rellena el sangrado prolongando el borde de la carta; estirar la
      // miniatura a toda la página da la misma impresión sin calcularlo aquí.
      if (slot.thumbnail) {
        parts.push(
          `<image href="${this._esc(slot.thumbnail)}" data-press-bleed="1" x="0" y="0" ` +
          `width="${n(g.page_w_mm)}" height="${n(g.page_h_mm)}" preserveAspectRatio="none" ` +
          `style="pointer-events:none"/>`
        );
      }
      parts.push(this._slotSvg(slot, g));
      parts.push(`<path d="${this._bleedPath(slot, g)}" fill="rgba(212,175,55,0.18)" fill-rule="evenodd" style="pointer-events:none"/>`);
    }
    const b = spec.bleed_mm;
    const r = spec.corner_radius_mm;
    const s = spec.safe_margin_mm;
    // Rosa, el color de las líneas de corte en preimpresión: se distingue igual
    // sobre un borde negro que sobre uno blanco.
    parts.push(
      `<rect x="${n(b)}" y="${n(b)}" width="${n(spec.trim_w_mm)}" height="${n(spec.trim_h_mm)}" ` +
      `rx="${n(r)}" ry="${n(r)}" fill="none" stroke="#ec4899" stroke-width="0.4" ` +
      `stroke-dasharray="1.4,0.8" data-press-cut="1" style="pointer-events:none"/>`
    );
    parts.push(
      `<rect x="${n(b + s)}" y="${n(b + s)}" width="${n(spec.trim_w_mm - 2 * s)}" ` +
      `height="${n(spec.trim_h_mm - 2 * s)}" rx="${n(r)}" ry="${n(r)}" fill="none" ` +
      `stroke="#16a34a" stroke-width="0.25" stroke-dasharray="0.6,0.6" opacity="0.85" ` +
      `style="pointer-events:none"/>`
    );
    return parts.join('');
  },

  pageLabel(page, idx) {
    if (!this.pressSpec) return '';
    const face = page.kind === 'back' ? window._t('press_page_back') : window._t('press_page_front');
    return `${window._t('press_page')} ${idx + 1} · ${face} ${page.card}`;
  },

  _dashArray(pattern) {
    if (pattern === 'dashed') return ` stroke-dasharray="1.5,1"`;
    if (pattern === 'dotted') return ` stroke-dasharray="0.1,1.2"`;
    return '';
  },

  _bleedPath(slot, g) {
    const b = g.bleed_mm; if (b <= 0) return '';
    const n = this._n.bind(this);
    const x1 = slot.x_mm, y1 = slot.y_mm;
    const x2 = x1 + g.slot_w_mm, y2 = y1 + g.slot_h_mm;
    const ix1 = x1 + b, iy1 = y1 + b, ix2 = x2 - b, iy2 = y2 - b;
    return `M ${n(x1)} ${n(y1)} H ${n(x2)} V ${n(y2)} H ${n(x1)} Z ` +
           `M ${n(ix1)} ${n(iy1)} V ${n(iy2)} H ${n(ix2)} V ${n(iy1)} Z`;
  },

  _cardCornerMarksSvg(g, o) {
    const n = this._n.bind(this);
    const L = o.card_guides_length_mm;
    const K = 0.5522847498;
    const parts = [];
    for (let col = 0; col < g.cols; col++) {
      for (let row = 0; row < g.rows; row++) {
        const x_slot = g.origin_x_mm + col * (g.slot_w_mm + g.gap_x_mm);
        const y_slot = g.origin_y_mm + row * (g.slot_h_mm + g.gap_y_mm);
        const xL = x_slot + g.bleed_mm;
        const xR = x_slot + g.slot_w_mm - g.bleed_mm;
        const yT = y_slot + g.bleed_mm;
        const yB = y_slot + g.slot_h_mm - g.bleed_mm;
        for (const [cx, cy, dx, dy] of [
          [xL, yT, -1, -1], [xR, yT, 1, -1],
          [xL, yB, -1, 1],  [xR, yB, 1, 1],
        ]) {
          let hEnd, vEnd;
          if (o.card_guides_placement === 'outside') { hEnd = cx + dx*L; vEnd = cy + dy*L; }
          else if (o.card_guides_placement === 'inside') { hEnd = cx - dx*L; vEnd = cy - dy*L; }
          else { hEnd = cx + dx*(L/2); vEnd = cy + dy*(L/2); }
          const hStart = o.card_guides_placement === 'middle' ? cx - dx*(L/2) : cx;
          const vStart = o.card_guides_placement === 'middle' ? cy - dy*(L/2) : cy;

          if (o.card_guides_shape === 'square') {
            parts.push(`<line x1="${n(hStart)}" y1="${n(cy)}" x2="${n(hEnd)}" y2="${n(cy)}"/>`);
            parts.push(`<line x1="${n(cx)}" y1="${n(vStart)}" x2="${n(cx)}" y2="${n(vEnd)}"/>`);
          } else {
            let c1x, c1y, c2x, c2y;
            if (o.card_guides_placement === 'middle') {
              c1x = hEnd - dx*(L/2)*K; c1y = cy + dy*(L/2)*K;
              c2x = cx + dx*(L/2)*K;   c2y = vEnd - dy*(L/2)*K;
            } else {
              c1x = hEnd;                c1y = cy + (vEnd - cy) * K;
              c2x = cx + (hEnd - cx) * K; c2y = vEnd;
            }
            parts.push(
              `<path d="M ${n(hEnd)} ${n(cy)} C ${n(c1x)} ${n(c1y)}, ${n(c2x)} ${n(c2y)}, ${n(cx)} ${n(vEnd)}"/>`
            );
          }
        }
      }
    }
    return parts.join('');
  },

  _cardFullRectsSvg(g, o) {
    const n = this._n.bind(this);
    const r = 3.0;
    const parts = [];
    for (let col = 0; col < g.cols; col++) {
      for (let row = 0; row < g.rows; row++) {
        const x_slot = g.origin_x_mm + col * (g.slot_w_mm + g.gap_x_mm);
        const y_slot = g.origin_y_mm + row * (g.slot_h_mm + g.gap_y_mm);
        const x = x_slot + g.bleed_mm;
        const y = y_slot + g.bleed_mm;
        const w = g.slot_w_mm - 2 * g.bleed_mm;
        const h = g.slot_h_mm - 2 * g.bleed_mm;
        if (o.card_guides_shape === 'round') {
          parts.push(`<rect x="${n(x)}" y="${n(y)}" width="${n(w)}" height="${n(h)}" rx="${n(r)}" ry="${n(r)}"/>`);
        } else {
          parts.push(`<rect x="${n(x)}" y="${n(y)}" width="${n(w)}" height="${n(h)}"/>`);
        }
      }
    }
    return parts.join('');
  },

  _guideLinePositions(g) {
    const V = new Set(), H = new Set();
    const round3 = v => Math.round(v * 1000) / 1000;
    for (let col = 0; col < g.cols; col++) {
      for (let row = 0; row < g.rows; row++) {
        const x_slot = g.origin_x_mm + col * (g.slot_w_mm + g.gap_x_mm);
        const y_slot = g.origin_y_mm + row * (g.slot_h_mm + g.gap_y_mm);
        V.add(round3(x_slot + g.bleed_mm));
        V.add(round3(x_slot + g.slot_w_mm - g.bleed_mm));
        H.add(round3(y_slot + g.bleed_mm));
        H.add(round3(y_slot + g.slot_h_mm - g.bleed_mm));
      }
    }
    return { V: [...V].sort((a,b)=>a-b), H: [...H].sort((a,b)=>a-b) };
  },

  _pageFullLinesSvg(g) {
    const n = this._n.bind(this);
    const { V, H } = this._guideLinePositions(g);
    const parts = [];
    for (const x of V) parts.push(`<line x1="${n(x)}" y1="0" x2="${n(x)}" y2="${n(g.page_h_mm)}"/>`);
    for (const y of H) parts.push(`<line x1="0" y1="${n(y)}" x2="${n(g.page_w_mm)}" y2="${n(y)}"/>`);
    return parts.join('');
  },

  _pageCornerLinesSvg(g, o) {
    const n = this._n.bind(this);
    const L = o.card_guides_length_mm;
    const { V, H } = this._guideLinePositions(g);
    const gx1 = g.origin_x_mm, gx2 = g.origin_x_mm + g.grid_w_mm;
    const gy1 = g.origin_y_mm, gy2 = g.origin_y_mm + g.grid_h_mm;
    const parts = [];
    for (const x of V) {
      parts.push(`<line x1="${n(x)}" y1="${n(Math.max(0, gy1 - L))}" x2="${n(x)}" y2="${n(gy1)}"/>`);
      parts.push(`<line x1="${n(x)}" y1="${n(gy2)}" x2="${n(x)}" y2="${n(Math.min(g.page_h_mm, gy2 + L))}"/>`);
    }
    for (const y of H) {
      parts.push(`<line x1="${n(Math.max(0, gx1 - L))}" y1="${n(y)}" x2="${n(gx1)}" y2="${n(y)}"/>`);
      parts.push(`<line x1="${n(gx2)}" y1="${n(y)}" x2="${n(Math.min(g.page_w_mm, gx2 + L))}" y2="${n(y)}"/>`);
    }
    return parts.join('');
  },

  _startBuildPolling() {
    if (this._buildPollTimer) return;
    this._buildPollTimer = setInterval(async () => {
      try {
        const r = await fetch(`/api/decks/${this.deckId}/build-progress`);
        if (!r.ok) return;
        const p = await r.json();
        if (p.active) this.buildProgress = p;
        if (p.done) this._stopBuildPolling();
      } catch (e) {}
    }, 300);
  },
  _stopBuildPolling() {
    if (this._buildPollTimer) { clearInterval(this._buildPollTimer); this._buildPollTimer = null; }
    setTimeout(() => { this.buildProgress = null; }, 700);
  },

  async buildPdf(showToast = true) {
    if (this.buildingPdf) return;
    this.buildingPdf = true;
    this._startBuildPolling();
    try {
      const r = await fetch(`/api/decks/${this.deckId}/build-pdf`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(this.opts),
      });
      if (!r.ok) {
        throw new Error(await window.apiErrorDetail(r));
      }
      const data = await r.json();
      if (showToast && window.toast) window.toast(window._t('pdf_done'), `${data.total_pages} ${window._t('common_pages')}`, 'success');
      if (this.autoOpen) window.open(`/api/exports/${data.filename}`, '_blank');
    } catch (e) {
      console.error('buildPdf:', e);
      if (window.toast) window.toast(window._t('pdf_error_generating'), e.message, 'error');
    } finally {
      this.buildingPdf = false;
      this._stopBuildPolling();
    }
  },

  async exportImages() {
    if (this.exportingImages) return;
    this.exportingImages = true;
    this._startBuildPolling();
    try {
      const r = await fetch(`/api/decks/${this.deckId}/export-images`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ decklist_format: 'with_set' }),
      });
      if (!r.ok) {
        throw new Error(await window.apiErrorDetail(r));
      }
      const data = await r.json();
      const msg = `${data.total_unique_cards} ${window._t('history_cards_count')}` +
                  (data.total_dfc_backs ? ` + ${data.total_dfc_backs} DFC` : '') +
                  (data.missing_images
                    ? ` · ${window._t('pdf_zip_missing').replace('{n}', data.missing_images)}`
                    : '');
      if (window.toast) window.toast(window._t('pdf_zip_done'), msg, data.missing_images ? 'warning' : 'success');
      if (this.autoOpen) window.open(`/api/exports/${data.filename}`, '_blank');
    } catch (e) {
      console.error('exportImages:', e);
      if (window.toast) window.toast(window._t('pdf_error_exporting'), e.message, 'error');
    } finally {
      this.exportingImages = false;
      this._stopBuildPolling();
    }
  },

  setPress(key) {
    this.press = key && this.press !== key ? key : null;
    this.pressResult = null;
    this.pressError = null;
    this._saveOpts();
    this.$nextTick(() => window.lucide && window.lucide.createIcons());
  },

  get pressCardbackUrl() {
    const cb = this.cardbackSettings;
    return (cb && (cb.image_url || cb.default_image_url)) || null;
  },

  // Cartas que no son de dos caras: sin dorso no hay reverso que imprimir.
  get pressNeedsCardback() {
    if (!this.deck || this.pressCardbackUrl) return false;
    return (this.deck.cards || []).some(c => c.include && !(c.is_dfc && c.back_thumbnail_url));
  },

  get pressDfcCount() {
    if (!this.deck) return 0;
    return (this.deck.cards || [])
      .filter(c => c.include && c.is_dfc && c.back_thumbnail_url)
      .reduce((sum, c) => sum + (c.quantity || 1), 0);
  },

  pressWarningText(w) {
    const face = w.face === 'back' ? window._t('press_page_back') : window._t('press_page_front');
    return `${w.card_name} (${face.toLowerCase()}): ${window._t('press_warn_' + w.code)}`;
  },

  formatBytes(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
    return `${Math.max(1, Math.round(bytes / 1e3))} kB`;
  },

  async copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      if (window.toast) window.toast(window._t('press_copied'), '', 'success');
    } catch (e) {
      if (window.toast) window.toast(window._t('common_error'), e.message, 'error');
    }
  },

  async exportPress() {
    if (this.exportingPress || !this.pressSpec) return;
    this.exportingPress = true;
    this.pressResult = null;
    this.pressError = null;
    this._startBuildPolling();
    try {
      const r = await fetch(`/api/decks/${this.deckId}/export-press`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ shop: this.pressSpec.key }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const d = data.detail;
        this.pressError = (d && typeof d === 'object')
          ? { code: d.code || 'error', message: d.message || `HTTP ${r.status}`, cards: d.cards || [] }
          : { code: 'error', message: d || `HTTP ${r.status}`, cards: [] };
        if (window.toast) window.toast(window._t('press_export_failed'), this.pressError.message, 'error');
        return;
      }
      this.pressResult = data;
      if (window.toast) window.toast(
        window._t('press_export_done'),
        `${data.total_cards} · ${data.total_pages} ${window._t('common_pages')}`,
        data.warnings.length ? 'warning' : 'success',
      );
    } catch (e) {
      console.error('exportPress:', e);
      this.pressError = { code: 'error', message: e.message, cards: [] };
      if (window.toast) window.toast(window._t('press_export_failed'), e.message, 'error');
    } finally {
      this.exportingPress = false;
      this._stopBuildPolling();
      this.$nextTick(() => window.lucide && window.lucide.createIcons());
    }
  },

  applyPreset(name) {
    const presets = {
      mpc_classic: {
        cols: 3, rows: 3, gap_x_mm: 0, gap_y_mm: 0,
        bleed_enabled: false, bleed_mm: 0,
        card_guides_enabled: true, card_guides_style: 'corners',
        card_guides_shape: 'square', card_guides_pattern: 'solid',
        card_guides_placement: 'outside', card_guides_length_mm: 4,
        page_guides: 'none',
        reg_marks_enabled: false, include_backs: false,
      },
      mpc_bleed: {
        cols: 3, rows: 3, gap_x_mm: 0, gap_y_mm: 0,
        bleed_enabled: true, bleed_mm: 1.875,
        card_guides_enabled: true, card_guides_style: 'corners',
        card_guides_shape: 'square', card_guides_pattern: 'solid',
        card_guides_placement: 'outside', card_guides_length_mm: 4,
        page_guides: 'none',
        reg_marks_enabled: false, include_backs: false,
      },
      duplex_dfc: {
        cols: 3, rows: 3, gap_x_mm: 0, gap_y_mm: 0,
        bleed_enabled: false, bleed_mm: 0,
        card_guides_enabled: true, card_guides_style: 'corners',
        card_guides_shape: 'square', card_guides_pattern: 'solid',
        card_guides_placement: 'outside', card_guides_length_mm: 4,
        page_guides: 'none',
        include_backs: true, backs_layout: 'duplex', backs_content: 'dfc_only',
      },
      duplex_all: {
        cols: 3, rows: 3, gap_x_mm: 0, gap_y_mm: 0,
        bleed_enabled: true, bleed_mm: 1.0,
        card_guides_enabled: true, card_guides_style: 'corners',
        card_guides_shape: 'square', card_guides_pattern: 'solid',
        card_guides_placement: 'outside', card_guides_length_mm: 4,
        page_guides: 'none',
        include_backs: true, backs_layout: 'duplex', backs_content: 'all_cards',
      },
      silhouette: {
        cols: 3, rows: 3, gap_x_mm: 3, gap_y_mm: 3,
        bleed_enabled: true, bleed_mm: 1.5,
        card_guides_enabled: true, card_guides_style: 'corners',
        card_guides_shape: 'square', card_guides_pattern: 'solid',
        card_guides_placement: 'outside', card_guides_length_mm: 4,
        page_guides: 'none',
        reg_marks_enabled: true, reg_marks_inset_mm: 10, reg_marks_size_mm: 5,
      },
    };
    const p = presets[name];
    if (!p) return;
    Object.assign(this.opts, p);
    if (window.toast) window.toast(window._t('pdf_preset_applied'), PRESET_LABELS[name] || name, 'info');
  },

  resetOpts() {
    localStorage.removeItem(LS_KEY(this.deckId));
    this.opts = { ...this.$data.opts };
    location.reload();
  },

  async openCardbackPicker() {
    this.cardbackModalOpen = true;
    this.cbDriveQuery = '';
    this.cbDriveHits = [];
    this.cbUrlInput = '';
    await this._refreshCardbackSettings();
    await this._loadLocalArts();
    await this._loadDefaultCardbacks();
    this.$nextTick(() => window.lucide && window.lucide.createIcons());
  },

  closeCardbackPicker() {
    this.cardbackModalOpen = false;
  },

  async _refreshCardbackSettings() {
    try {
      const r = await fetch(`/api/decks/${this.deckId}/cardback-settings`);
      if (r.ok) this.cardbackSettings = await r.json();
    } catch (e) { console.error('refreshCardback:', e); }
  },

  async _loadLocalArts() {
    try {
      const r = await fetch(`/api/custom-art/`);
      if (r.ok) this.cbLocalArts = await r.json();
    } catch (e) {
      this.cbLocalArts = [];
      console.error('loadLocalArts:', e);
    }
  },

  async _loadDefaultCardbacks() {
    if (this.cbDefaultHits.length > 0) return;
    this.cbDefaultLoading = true;
    try {
      const r = await fetch('/api/drives/cardbacks?limit=200');
      if (r.ok) this.cbDefaultHits = await r.json();
      else this.cbDefaultHits = [];
    } catch (e) {
      this.cbDefaultHits = [];
      console.error('loadDefaultCardbacks:', e);
    } finally {
      this.cbDefaultLoading = false;
    }
  },

  async searchDrives(query) {
    const q = (query || '').trim();
    if (!q) { this.cbDriveHits = []; return; }
    this.cbDriveSearching = true;
    try {
      const r = await fetch(`/api/drives/search?q=${encodeURIComponent(q)}&limit=20`);
      if (r.ok) this.cbDriveHits = await r.json();
      else this.cbDriveHits = [];
    } catch (e) {
      this.cbDriveHits = [];
      console.error('searchDrives:', e);
    } finally {
      this.cbDriveSearching = false;
    }
  },

  async addCardbackFromUrl() {
    const url = this.cbUrlInput.trim();
    if (!url) return;
    this.cbAddingUrl = true;
    try {
      const r = await fetch('/api/custom-art/from-url', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          url,
          card_name: `_cardback_${this.deck?.name || 'deck'}`,
          face: 'back',
        }),
      });
      if (!r.ok) throw new Error(await window.apiErrorDetail(r));
      const art = await r.json();
      await this._setCardback(art.id);
      this.cbUrlInput = '';
      await this._loadLocalArts();
      if (window.toast) window.toast(window._t('pdf_cardback_added'), art.filename, 'success');
    } catch (e) {
      console.error('addCardbackFromUrl:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message, 'error');
    } finally {
      this.cbAddingUrl = false;
    }
  },

  async pickDriveHit(hit) {
    this.cbPickingId = hit.file_id;
    try {
      const r = await fetch('/api/custom-art/from-url', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          url: hit.download_url,
          card_name: `_cardback_${this.deck?.name || 'deck'}`,
          face: 'back',
          variant: hit.source_name,
        }),
      });
      if (!r.ok) throw new Error(await window.apiErrorDetail(r));
      const art = await r.json();
      await this._setCardback(art.id);
      await this._loadLocalArts();
      if (window.toast) window.toast(window._t('pdf_cardback_added'), hit.source_name, 'success');
    } catch (e) {
      console.error('pickDriveHit:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message, 'error');
    } finally {
      this.cbPickingId = null;
    }
  },

  async pickLocalArt(art) {
    this.cbPickingId = 'local-' + art.id;
    try {
      await this._setCardback(art.id);
      if (window.toast) window.toast(window._t('pdf_cardback_selected'), art.filename, 'success');
    } catch (e) {
      console.error('pickLocalArt:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message, 'error');
    } finally {
      this.cbPickingId = null;
    }
  },

  async uploadCardback(file = null) {
    const current = this.cardbackSettings?.image_url || this.cardbackSettings?.default_image_url || null;
    const result = await window.artUpload.open({
      title: window._t('art_upload_cardback_title'),
      displayName: this.deck?.name || '',
      cardName: `_cardback_${this.deck?.name || 'deck'}`,
      face: 'back',
      currentImage: current,
      file,
    });
    if (!result || !result.art) return;
    try {
      await this._setCardback(result.art.id);
      await this._loadLocalArts();
    } catch (e) {
      console.error('uploadCardback:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message, 'error');
    }
  },

  async uploadArtForSlot(cardId, face = 'front', file = null) {
    const card = (this.deck?.cards || []).find(c => c.id === cardId);
    if (!card) return;
    const result = await window.artUpload.open({
      cardName: card.name,
      displayName: face === 'back' && card.back_name ? card.back_name : card.name,
      face,
      currentImage: face === 'back' ? card.back_thumbnail_url : card.thumbnail_url,
      deckId: this.deckId,
      deckCardId: card.id,
      file,
    });
    if (!result || !result.card) return;
    const cards = this.deck.cards || [];
    const idx = cards.findIndex(c => c.id === result.card.id);
    if (idx >= 0) cards[idx] = result.card;
    if (this.miniPickerOpen && this.miniPickerCard?.id === card.id) this.closeMiniArtPicker();
  },

  uploadFromMiniPicker(file = null) {
    if (!this.miniPickerCard) return;
    this.uploadArtForSlot(this.miniPickerCard.id, this.miniPickerFace, file);
  },

  _clearDropHighlight() {
    const el = dropTarget;
    if (!el) return;
    el.setAttribute('fill', el.dataset.prevFill || 'transparent');
    el.removeAttribute('stroke');
    el.removeAttribute('stroke-width');
    dropTarget = null;
  },

  onPreviewDragOver(ev) {
    if (!window.artUpload.hasFile(ev.dataTransfer)) return;
    const el = ev.target.closest && ev.target.closest('[data-slot-kind]');
    if (!el) { this._clearDropHighlight(); return; }
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'copy';
    if (dropTarget === el) return;
    this._clearDropHighlight();
    el.dataset.prevFill = el.getAttribute('fill') || '';
    el.setAttribute('fill', 'rgba(212, 175, 55, 0.35)');
    el.setAttribute('stroke', '#d4af37');
    el.setAttribute('stroke-width', '0.8');
    dropTarget = el;
  },

  onPreviewDragLeave(ev) {
    if (ev.currentTarget.contains(ev.relatedTarget)) return;
    this._clearDropHighlight();
  },

  onPreviewDrop(ev) {
    const el = ev.target.closest && ev.target.closest('[data-slot-kind]');
    this._clearDropHighlight();
    const file = window.artUpload.fileFrom(ev.dataTransfer);
    if (!el || !file) return;
    ev.preventDefault();
    if (el.getAttribute('data-slot-kind') === 'cardback') {
      this.uploadCardback(file);
      return;
    }
    const cardId = Number(el.getAttribute('data-card-id'));
    if (cardId) this.uploadArtForSlot(cardId, el.getAttribute('data-face') || 'front', file);
  },

  onMiniPickerDrop(ev) {
    const file = window.artUpload.fileFrom(ev.dataTransfer);
    if (!file) return;
    ev.preventDefault();
    this.uploadFromMiniPicker(file);
  },

  async _setCardback(customArtId) {
    const r = await fetch(`/api/decks/${this.deckId}/cardback-settings`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ custom_art_id: customArtId }),
    });
    if (!r.ok) throw new Error(await window.apiErrorDetail(r));
    this.cardbackSettings = await r.json();
  },

  async resetCardback() {
    try {
      const r = await fetch(`/api/decks/${this.deckId}/cardback-settings`, {
        method: 'DELETE',
      });
      if (!r.ok) throw new Error(await window.apiErrorDetail(r));
      this.cardbackSettings = await r.json();
      if (window.toast) window.toast(window._t('pdf_cardback_reset'), '', 'info');
    } catch (e) {
      console.error('resetCardback:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message, 'error');
    }
  },

  onPreviewClick(ev) {
    const el = ev.target.closest && ev.target.closest('[data-slot-kind]');
    if (!el) return;
    ev.preventDefault();
    ev.stopPropagation();
    const kind = el.getAttribute('data-slot-kind');
    if (kind === 'cardback') {
      this.openCardbackPicker();
      return;
    }
    const cardId = Number(el.getAttribute('data-card-id'));
    const face = el.getAttribute('data-face') || 'front';
    if (!cardId) return;
    this.openMiniArtPicker(cardId, face);
  },

  async openMiniArtPicker(cardId, face) {
    const card = (this.deck?.cards || []).find(c => c.id === cardId);
    if (!card) return;
    this.miniPickerCard = card;
    this.miniPickerFace = face;
    this.miniPickerOpen = true;
    this.miniPickerArts = [];
    this.miniPickerLoading = true;
    this.miniPickerBusy = false;
    this.miniPickerBusyKey = null;
    try {
      const arts = await window.api.cards.allPrints(this.deckId, cardId);
      this.miniPickerArts = arts.filter(a => (a.face || 'front') === face);
    } catch (e) {
      console.error('openMiniArtPicker:', e);
      if (window.toast) window.toast(window._t('deck_error_load_arts'), e.message, 'error');
      this.miniPickerArts = [];
    } finally {
      this.miniPickerLoading = false;
      this.$nextTick(() => window.lucide && window.lucide.createIcons());
    }
  },

  closeMiniArtPicker() {
    this.miniPickerOpen = false;
    this.miniPickerCard = null;
    this.miniPickerArts = [];
    this.miniPickerBusyKey = null;
  },

  miniArtKey(art) {
    if (art.kind === 'custom') return `c-${art.custom_art_id}-${art.face}`;
    return `s-${art.scryfall_id}-${art.face}`;
  },

  async pickMiniArt(art, rememberGlobally = false) {
    if (this.miniPickerBusy) return;
    const card = this.miniPickerCard;
    if (!card) return;
    this.miniPickerBusy = true;
    this.miniPickerBusyKey = this.miniArtKey(art);
    try {
      const body = { deck_card_id: card.id, face: art.face || this.miniPickerFace };
      if (art.kind === 'custom') body.custom_art_id = art.custom_art_id;
      else { body.scryfall_id = art.scryfall_id; body.remember_globally = !!rememberGlobally; }
      const r = await fetch(`/api/decks/${this.deckId}/cards/change-art`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await window.apiErrorDetail(r));
      const updated = await r.json();
      const cards = this.deck.cards || [];
      const idx = cards.findIndex(c => c.id === updated.id);
      if (idx >= 0) cards[idx] = updated;
      this.miniPickerArts = this.miniPickerArts.map(a => ({
        ...a,
        is_chosen: this.miniArtKey(a) === this.miniArtKey(art),
      }));
      if (window.toast) window.toast(
        rememberGlobally ? window._t('pdf_art_global') : window._t('pdf_art_updated'),
        card.name,
        'success',
      );
    } catch (e) {
      console.error('pickMiniArt:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message, 'error');
    } finally {
      this.miniPickerBusy = false;
      this.miniPickerBusyKey = null;
    }
  },

  openFullEditor() {
    if (!this.miniPickerCard) return;
    const cid = this.miniPickerCard.id;
    window.open(`/decks/${this.deckId}?openArt=${cid}`, '_blank');
  },

  openGuide(section = null) {
    this.guideOpen = true;
    this.guideSection = section;
    this.$nextTick(() => {
      window.lucide && window.lucide.createIcons();
      if (section) {
        const el = document.getElementById('guide-section-' + section);
        if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' });
      }
    });
  },

  closeGuide() { this.guideOpen = false; this.guideSection = null; },

}}

window.pdfStudio = pdfStudio
