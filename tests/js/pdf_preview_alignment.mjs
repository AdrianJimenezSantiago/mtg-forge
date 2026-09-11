// Comprueba que, en la vista previa de PDF Studio, cada carta y cada
// placeholder (p. ej. "Cardback" sin imagen) caen exactamente sobre una guía
// de corte, para todas las combinaciones de sangrado, separación, offset de
// reversos y espejado dúplex. Lo ejecuta tests/test_pdf_preview_alignment.py.
//
// Uso: node pdf_preview_alignment.mjs static/js/pdf-studio.js
import fs from 'fs'; import vm from 'vm';
const src = fs.readFileSync(process.argv[2], 'utf8');
const ctx = { window: { _t: k => k }, console, localStorage: { getItem(){return null}, setItem(){} } };
vm.createContext(ctx); vm.runInContext(src + '\n;globalThis.__f = pdfStudio;', ctx);
const num = (el, a) => +parseFloat(el.match(new RegExp(`\\b${a}="([-\\d.]+)"`))[1]).toFixed(3);
const box = el => ['x','y','width','height'].map(a => num(el, a)).join(',');
let cases = 0, bad = [];
for (const bleed of [0, 1, 3]) for (const gap of [0, 2]) for (const [bx, by] of [[0,0],[1.5,-0.8]])
for (const layout of ['duplex', 'append']) for (const kind of ['front', 'back']) {
  const s = ctx.__f(1);
  Object.assign(s.opts, { bleed_enabled: bleed > 0, bleed_mm: bleed, gap_x_mm: gap, gap_y_mm: gap,
    back_offset_x_mm: bx, back_offset_y_mm: by, card_guides_style: 'full',
    include_backs: true, backs_layout: layout });
  const g = s.geomForKind(kind);
  const slots = Array.from({length: 9}, (_, i) => i % 2
    ? { name: 'Cardback', thumbnail: null, kind: 'cardback' }
    : { name: 'Img', thumbnail: 'x.png', kind: 'card-front', card_id: i, face: 'front' });
  const chunk = s._chunkWithPositions(slots, g, kind === 'back' && layout === 'duplex');
  const svg = s.svgMarkupForPage({ kind, chunk });
  const guides = new Set([...svg.matchAll(/<rect [^>]*>/g)].map(m => m[0]).filter(r => !/fill=/.test(r)).map(box));
  const cards = [...svg.matchAll(/<image [^>]*>|<rect [^>]*fill="#242a3a"[^>]*>/g)].map(m => box(m[0]));
  cases++;
  const off = cards.filter(c => !guides.has(c));
  if (off.length) bad.push({ bleed, gap, bx, by, layout, kind, off });
}
console.log(JSON.stringify({ cases, misaligned: bad }));
