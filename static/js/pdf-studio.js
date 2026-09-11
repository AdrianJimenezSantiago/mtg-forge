/**
 * PDF Studio
 *
 * Extraído de `templates/pdf_studio.html`, donde vivía como un bloque `<script>`
 * de 1022 líneas. La lógica es idéntica: solo ha cambiado de fichero.
 *
 * Las funciones que Alpine necesita resolver desde los atributos `x-data` del
 * HTML se publican en `window` al final del módulo. Es deliberado: Alpine
 * evalúa `x-data` como una expresión en el ámbito global, así que un `export`
 * por sí solo no basta.
 *
 * Regenerar con:  python scripts/extract_inline_js.py
 */
// Persistimos las opciones por deck en localStorage para que la próxima
// vez que el usuario abra el studio salgan igual.
const LS_KEY = (id) => `pdfStudio.opts.v2.${id}`;

function pdfStudio(deckId) { return {
  deckId,
  deck: null,
  // cardbackSettings.default_image_url = URL del cardback global (si existe)
  // cardbackSettings.image_url         = URL del cardback específico del mazo (si el user lo configuró)
  // El fallback en el preview: image_url → default_image_url → null (slot vacío).
  cardbackSettings: null,
  buildProgress: null,
  buildingPdf: false,
  exportingImages: false,
  autoOpen: true,
  zoom: 60,
  showPageMargins: false,

  // --- Cardback picker modal ---
  cardbackModalOpen: false,
  cbUrlInput: '',
  cbAddingUrl: false,
  cbDriveQuery: '',
  cbDriveHits: [],
  cbDriveSearching: false,
  cbDefaultHits: [],          // cardbacks precargados (tag "back") de drives indexados
  cbDefaultLoading: false,    // true mientras se cargan los cardbacks por defecto
  cbLocalArts: [],
  cbPickingId: null,          // id que está siendo aplicado (spinner)

  // --- Mini art picker (click sobre una carta en el preview) ---
  // Se abre con onPreviewClick(); reutiliza /cards/{id}/prints y
  // /cards/change-art. Es intencionadamente minimalista comparado con
  // el picker completo del editor de mazos: aquí solo mostramos las
  // impresiones existentes en un grid. Si el usuario necesita búsqueda
  // en Drives o URLs, se le ofrece el botón "Abrir en el editor completo".
  miniPickerOpen: false,
  miniPickerCard: null,       // referencia al DeckCard del deck.cards
  miniPickerFace: 'front',    // 'front' | 'back' (para DFC)
  miniPickerArts: [],
  miniPickerLoading: false,
  miniPickerBusy: false,      // spinner mientras se aplica el cambio
  miniPickerBusyKey: null,    // qué opción está aplicándose

  // --- Guía de opciones ---
  guideOpen: false,
  guideSection: null,         // sección para scrollear al abrir

  // Opciones — mismo shape que el dataclass PDFOptions del backend.
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
  },

  // ---------- INIT ----------
  async init() {
    // 1) carga persistencia si la hay
    this._loadOpts();
    // 2) reactivamos persistencia y watch para lucide
    this.$watch('opts', () => this._saveOpts(), { deep: true });
    this.$watch('zoom', () => this._saveVolatile());
    // 3) fetch del mazo
    try {
      const r = await fetch(`/api/decks/${this.deckId}`);
      if (r.ok) this.deck = await r.json();
    } catch (e) {
      console.error('deck fetch:', e);
    }
    // 3b) Cardback settings del mazo.
    try {
      const rs = await fetch(`/api/decks/${this.deckId}/cardback-settings`);
      if (rs.ok) this.cardbackSettings = await rs.json();
    } catch (e) { /* silent */ }
    // 4) icons
    if (window.lucide) window.lucide.createIcons();
    // Re-render iconos cuando el DOM cambie
    this.$nextTick(() => window.lucide && window.lucide.createIcons());
  },

  _loadOpts() {
    try {
      const raw = localStorage.getItem(LS_KEY(this.deckId));
      if (!raw) return;
      const saved = JSON.parse(raw);
      // Merge shallow (mantiene defaults para campos nuevos que no estén en LS)
      Object.assign(this.opts, saved.opts || {});
      if (typeof saved.zoom === 'number') this.zoom = saved.zoom;
      if (typeof saved.showPageMargins === 'boolean') this.showPageMargins = saved.showPageMargins;
    } catch (e) { /* silent */ }
  },

  _saveOpts() {
    try {
      localStorage.setItem(LS_KEY(this.deckId), JSON.stringify({
        opts: this.opts, zoom: this.zoom, showPageMargins: this.showPageMargins,
      }));
    } catch (e) { /* silent */ }
  },
  _saveVolatile() { this._saveOpts(); },

  // ---------- DERIVED ----------
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
  // px/mm on screen: 1mm = 3.7795 px @ 96dpi × zoom%
  get pxPerMm() { return 3.7795 * (this.zoom / 100); },

  // Cálculo de geometría (mm) — espejo del backend compute_geometry.
  geomForKind(kind) {
    const o = this.opts;
    // Tamaños de página en mm.
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
    // SVG y-down: la esquina TOP-LEFT de la rejilla en coordenadas de página.
    // El semántico de offset_y_mm es "positivo = mover ARRIBA" (heredado del
    // PDF y-up del backend), así que en SVG lo restamos.
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

  // ---------- PAGINACIÓN VIRTUAL ----------
  // Expande el mazo en slots (front + back donde aplique), y luego los agrupa
  // en páginas según cols×rows.
  get allSlots() {
    if (!this.deck) return { fronts: [], backs: [] };
    const includeBacks = this.opts.include_backs;
    const fillAllBacks = this.opts.backs_content === 'all_cards';
    // Prioridad: cardback específico del mazo → cardback global → null (slot vacío)
    const cardback = (this.cardbackSettings && this.cardbackSettings.image_url)
                  || (this.cardbackSettings && this.cardbackSettings.default_image_url)
                  || null;
    const fronts = [], backs = [];
    for (const c of (this.deck.cards || [])) {
      if (!c.include) continue;
      const qty = c.quantity || 1;
      for (let i = 0; i < qty; i++) {
        // kind: 'card-front'|'card-back'|'cardback' — usado por onPreviewClick
        // para decidir si abre el mini art picker o el cardback picker.
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

    // Trocear fronts en chunks de per_page (el último puede quedar corto).
    const frontChunks = [];
    for (let i = 0; i < fronts.length; i += per) frontChunks.push(fronts.slice(i, i + per));

    if (this.opts.include_backs && this.opts.backs_layout === 'duplex') {
      // Espejo (long-edge flip): cada hoja de fronts + su hoja de reversos.
      for (let i = 0; i < frontChunks.length; i++) {
        pages.push({
          key: `f${i}`, kind: 'front', mirror: false,
          chunk: this._chunkWithPositions(frontChunks[i], g),
        });
        const bChunk = backs.slice(i * per, i * per + per);
        if (bChunk.some(b => b !== null)) {
          pages.push({
            key: `bd${i}`, kind: 'back', mirror: true,
            chunk: this._chunkWithPositions(bChunk, this.geomForKind('back'), /* mirror */ true),
          });
        }
      }
    } else if (this.opts.include_backs && this.opts.backs_layout === 'append') {
      // Append: rellenar huecos libres de la ÚLTIMA hoja de fronts primero
      // (si compact_fill) y luego seguir con hojas nuevas de reversos.
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
    // SVG y-down: la primera fila (row 0) es la fila SUPERIOR de la hoja,
    // por lo que la carta i=0 cae arriba-izquierda (lectura natural 1-2-3
    // / 4-5-6 / 7-8-9). El backend usa el mismo mapeo de índices pero en
    // ReportLab (y-up), y por eso allí la fórmula es (rows-1-row).
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

  // ---------- SVG STRING (Alpine-friendly x-html) ----------
  // Ver comentario en la plantilla: no podemos anidar <template> dentro de
  // <svg>, así que generamos el SVG como string y lo inyectamos con x-html.
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

    // Márgenes del grid
    if (this.showPageMargins) {
      parts.push(
        `<rect x="${n(g.origin_x_mm)}" y="${n(g.origin_y_mm)}" ` +
        `width="${n(g.grid_w_mm)}" height="${n(g.grid_h_mm)}" ` +
        `fill="none" stroke="#3b82f6" stroke-width="0.15" stroke-dasharray="1,1" opacity="0.5"/>`
      );
    }

    // Imágenes — clickables. data-* atributos leídos por onPreviewClick():
    //  * kind=card-front|card-back|cardback
    //  * card-id (solo si kind es card-*): id del DeckCard
    //  * face: 'front' | 'back'
    // Un <rect> transparente encima captura el click con área garantizada
    // incluso si la <image> aún no ha resuelto la carga (Chrome/Firefox
    // fallan a veces con pointer-events en <image> mientras el href pende).
    for (const slot of page.chunk) {
      if (!slot) continue;
      const clickAttrs =
        slot.kind === 'cardback'
          ? ` data-slot-kind="cardback" style="cursor:pointer"`
          : slot.card_id != null
            ? ` data-slot-kind="${esc(slot.kind)}" data-card-id="${slot.card_id}" ` +
              `data-face="${esc(slot.face)}" style="cursor:pointer"`
            : '';
      // Tanto la imagen como el placeholder van en el área de trim (63×88),
      // NO estirados al slot completo — igual que en el backend
      // (``_render_page`` / ``_placeholder``). Las imágenes de Scryfall y la
      // mayoría de fuentes NO tienen bleed incorporado: estirarlas al slot
      // haría que las marcas de corte quedaran dentro de la carta.
      // Si el arte sí tiene bleed real (MPC drives), la diferencia visual es
      // mínima (solo se pierde la franja de bleed en el preview).
      // Antes el placeholder (p. ej. "Cardback" sin imagen) ocupaba el slot
      // entero, bleed incluido, y parecía desalineado respecto a las guías.
      const imgX = slot.x_mm + g.bleed_mm;
      const imgY = slot.y_mm + g.bleed_mm;
      const imgW = g.slot_w_mm - 2 * g.bleed_mm;  // = 63.0 siempre
      const imgH = g.slot_h_mm - 2 * g.bleed_mm;  // = 88.0 siempre
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
      // Rect transparente encima — captura click aunque la <image> tarde
      // en cargar. Solo si el slot es interactivo.
      if (clickAttrs) {
        parts.push(
          `<rect x="${n(slot.x_mm)}" y="${n(slot.y_mm)}" ` +
          `width="${n(g.slot_w_mm)}" height="${n(g.slot_h_mm)}" ` +
          `fill="transparent"${clickAttrs}>` +
          `<title>${esc(slot.kind === 'cardback' ? window._t('pdf_change_cardback') : window._t('pdf_mini_picker_title') + ' — ' + slot.name)}</title>` +
          `</rect>`
        );
      }
    }

    // Banda de bleed
    if (o.bleed_enabled && g.bleed_mm > 0) {
      for (const slot of page.chunk) {
        if (!slot) continue;
        parts.push(`<path d="${this._bleedPath(slot, g)}" fill="rgba(212,175,55,0.12)"/>`);
      }
    }

    // Card guides
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

    // Page guides
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

    // Reg marks (SVG y-down: TL/TR arriba, BL abajo — coincide con el backend).
    if (o.reg_marks_enabled) {
      parts.push(`<g fill="#000">`);
      const s = o.reg_marks_size_mm;
      for (const [cx, cy] of [
        [o.reg_marks_inset_mm, o.reg_marks_inset_mm],                          // TL
        [g.page_w_mm - o.reg_marks_inset_mm, o.reg_marks_inset_mm],            // TR
        [o.reg_marks_inset_mm, g.page_h_mm - o.reg_marks_inset_mm],            // BL
      ]) {
        parts.push(`<rect x="${n(cx - s/2)}" y="${n(cy - s/2)}" width="${n(s)}" height="${n(s)}"/>`);
      }
      parts.push(`</g>`);
    }

    parts.push(`</svg>`);
    return parts.join('');
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
        // SVG y-down: row 0 arriba, row n-1 abajo.
        const y_slot = g.origin_y_mm + row * (g.slot_h_mm + g.gap_y_mm);
        const xL = x_slot + g.bleed_mm;
        const xR = x_slot + g.slot_w_mm - g.bleed_mm;
        // yT/yB en SVG: yT = borde superior (menor Y), yB = borde inferior.
        const yT = y_slot + g.bleed_mm;
        const yB = y_slot + g.slot_h_mm - g.bleed_mm;
        // dy en las esquinas: negativo apunta hacia arriba en el sentido
        // "usuario" pero, ojo, aquí lo interpretamos como delta en el eje
        // SVG donde +Y es hacia abajo. Las 4 esquinas: TL(-1,-1) TR(+1,-1)
        // BL(-1,+1) BR(+1,+1).
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
            // Arco bezier de cuarto de círculo entre (hEnd, cy) → (cx, vEnd)
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
        // SVG y-down: row 0 arriba.
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
        // SVG y-down: row 0 arriba.
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

  // ---------- BUILD PDF ----------
  _startBuildPolling() {
    if (this._buildPollTimer) return;
    this._buildPollTimer = setInterval(async () => {
      try {
        const r = await fetch(`/api/decks/${this.deckId}/build-progress`);
        if (!r.ok) return;
        const p = await r.json();
        if (p.active) this.buildProgress = p;
        if (p.done) this._stopBuildPolling();
      } catch (e) { /* silent */ }
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
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      const data = await r.json();
      if (showToast && window.toast) window.toast(`PDF generado: ${data.total_pages} páginas`, 'success');
      if (this.autoOpen) window.open(`/api/exports/${data.filename}`, '_blank');
    } catch (e) {
      console.error('buildPdf:', e);
      if (window.toast) window.toast(`Error generando PDF: ${e.message}`, 'error');
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
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      const data = await r.json();
      const msg = `ZIP listo: ${data.total_unique_cards} cartas` +
                  (data.total_dfc_backs ? ` + ${data.total_dfc_backs} DFC backs` : '') +
                  (data.missing_images ? ` (⚠️ ${data.missing_images} imágenes no encontradas)` : '');
      if (window.toast) window.toast(msg, data.missing_images ? 'warning' : 'success');
      if (this.autoOpen) window.open(`/api/exports/${data.filename}`, '_blank');
    } catch (e) {
      console.error('exportImages:', e);
      if (window.toast) window.toast(`Error exportando: ${e.message}`, 'error');
    } finally {
      this.exportingImages = false;
      this._stopBuildPolling();
    }
  },

  // ---------- PRESETS ----------
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
    if (window.toast) window.toast(`Preset "${name}" aplicado`, 'info');
  },

  resetOpts() {
    localStorage.removeItem(LS_KEY(this.deckId));
    this.opts = { ...this.$data.opts };  // hack: re-evaluar defaults
    // Cleaner: reload
    location.reload();
  },

  // =====================================================================
  // Cardback picker — reutiliza custom-art + drives + from-url
  // =====================================================================
  async openCardbackPicker() {
    this.cardbackModalOpen = true;
    this.cbDriveQuery = '';
    this.cbDriveHits = [];
    this.cbUrlInput = '';
    // Refresca estado por si algo cambió detrás
    await this._refreshCardbackSettings();
    await this._loadLocalArts();
    // Cargar por defecto todos los cardbacks de los drives indexados
    await this._loadDefaultCardbacks();
    // Re-render iconos del modal
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
    if (this.cbDefaultHits.length > 0) return;  // ya cargados
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
      // 1) Descargar la URL a la librería local de custom art.
      const r = await fetch('/api/custom-art/from-url', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          url,
          // Nombre "sintético" para que quede indexado por mazo.
          card_name: `_cardback_${this.deck?.name || 'deck'}`,
          face: 'back',
        }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      const art = await r.json();
      // 2) Asignarlo como cardback del mazo.
      await this._setCardback(art.id);
      this.cbUrlInput = '';
      // 3) Refrescar librería local para que aparezca inmediatamente.
      await this._loadLocalArts();
      if (window.toast) window.toast(`Cardback añadido: ${art.filename}`, 'success');
    } catch (e) {
      console.error('addCardbackFromUrl:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message);
    } finally {
      this.cbAddingUrl = false;
    }
  },

  async pickDriveHit(hit) {
    this.cbPickingId = hit.file_id;
    try {
      // Descarga a la librería local y luego asigna.
      const r = await fetch('/api/custom-art/from-url', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          url: hit.download_url,
          card_name: `_cardback_${this.deck?.name || 'deck'}`,
          face: 'back',
          variant: hit.source_name,
        }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      const art = await r.json();
      await this._setCardback(art.id);
      await this._loadLocalArts();
      if (window.toast) window.toast(`Cardback desde ${hit.source_name}`, 'success');
    } catch (e) {
      console.error('pickDriveHit:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message);
    } finally {
      this.cbPickingId = null;
    }
  },

  async pickLocalArt(art) {
    this.cbPickingId = 'local-' + art.id;
    try {
      await this._setCardback(art.id);
      if (window.toast) window.toast(`Cardback: ${art.filename}`, 'success');
    } catch (e) {
      console.error('pickLocalArt:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message);
    } finally {
      this.cbPickingId = null;
    }
  },

  async _setCardback(customArtId) {
    const r = await fetch(`/api/decks/${this.deckId}/cardback-settings`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ custom_art_id: customArtId }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    this.cardbackSettings = await r.json();
  },

  async resetCardback() {
    try {
      const r = await fetch(`/api/decks/${this.deckId}/cardback-settings`, {
        method: 'DELETE',
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      this.cardbackSettings = await r.json();
      if (window.toast) window.toast(window._t('pdf_cardback_reset'), 'info');
    } catch (e) {
      console.error('resetCardback:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message);
    }
  },

  // =====================================================================
  // Click sobre una carta del preview → mini picker o cardback picker
  // =====================================================================
  onPreviewClick(ev) {
    // Event delegation: buscamos el ancestro más cercano con data-slot-kind.
    // Cubre <image>, <rect> (placeholder) y el <rect fill=transparent> que
    // ponemos encima para garantizar el click aunque la imagen tarde.
    const el = ev.target.closest && ev.target.closest('[data-slot-kind]');
    if (!el) return;
    ev.preventDefault();
    ev.stopPropagation();
    const kind = el.getAttribute('data-slot-kind');
    if (kind === 'cardback') {
      // Un cardback no es de una carta concreta — abrimos el picker global.
      this.openCardbackPicker();
      return;
    }
    const cardId = Number(el.getAttribute('data-card-id'));
    const face = el.getAttribute('data-face') || 'front';
    if (!cardId) return;
    this.openMiniArtPicker(cardId, face);
  },

  // =====================================================================
  // Mini art picker
  // =====================================================================
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
      // `allPrints` devuelve un array plano. El endpoint pasó a estar
      // paginado y este punto seguía haciendo `.filter()` sobre el sobre,
      // que no es un array: "arts.filter is not a function".
      const arts = await window.api.cards.allPrints(this.deckId, cardId);
      // Filtramos por cara — para DFC el usuario clickó una cara concreta.
      // Para no-DFC solo hay 'front', así que el filtro no hace daño.
      this.miniPickerArts = arts.filter(a => (a.face || 'front') === face);
    } catch (e) {
      console.error('openMiniArtPicker:', e);
      if (window.toast) window.toast(window._t('deck_error_load_arts'), e.message);
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
    // Clave única y estable para :key y para marcar el que se está aplicando.
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
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      const updated = await r.json();
      // Actualiza la carta en deck.cards para que el preview refleje el
      // cambio sin necesidad de re-fetch completo.
      const cards = this.deck.cards || [];
      const idx = cards.findIndex(c => c.id === updated.id);
      if (idx >= 0) cards[idx] = updated;
      // Refresca is_chosen dentro del picker abierto para feedback inmediato.
      this.miniPickerArts = this.miniPickerArts.map(a => ({
        ...a,
        is_chosen: this.miniArtKey(a) === this.miniArtKey(art),
      }));
      if (window.toast) window.toast(
        rememberGlobally ? window._t('pdf_art_global') : window._t('pdf_art_updated'),
        'success',
      );
    } catch (e) {
      console.error('pickMiniArt:', e);
      if (window.toast) window.toast(window._t('common_error'), e.message);
    } finally {
      this.miniPickerBusy = false;
      this.miniPickerBusyKey = null;
    }
  },

  openFullEditor() {
    // Fallback: si el usuario quiere el picker completo (drives, subida por
    // URL, filtros avanzados), lo mandamos al editor con la carta seleccionada.
    if (!this.miniPickerCard) return;
    const cid = this.miniPickerCard.id;
    window.open(`/decks/${this.deckId}?openArt=${cid}`, '_blank');
  },

  // =====================================================================
  // Guía de opciones
  // =====================================================================
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


// --- Puente con Alpine -------------------------------------
// Alpine resuelve las expresiones de `x-data` contra el ámbito
// global, así que estas funciones tienen que estar en `window`.
window.pdfStudio = pdfStudio
