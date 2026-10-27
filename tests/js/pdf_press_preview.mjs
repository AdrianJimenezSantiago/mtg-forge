import fs from 'fs'; import vm from 'vm';
const src = fs.readFileSync(process.argv[2], 'utf8');
const ctx = { window: { _t: k => k }, console, localStorage: { getItem(){return null}, setItem(){} } };
vm.createContext(ctx); vm.runInContext(src + '\n;globalThis.__f = pdfStudio;', ctx);
const spec = {
  key: 'pinbro_poker', shop_name: 'Pinbro Games', product_name: 'Póquer 63×88 mm',
  trim_w_mm: 63, trim_h_mm: 88, bleed_mm: 3, safe_margin_mm: 3, corner_radius_mm: 3,
  page_w_mm: 69, page_h_mm: 94,
};
const num = (el, a) => +parseFloat(el.match(new RegExp(`\\b${a}="([-\\d.]+)"`))[1]).toFixed(3);
const box = el => ['x','y','width','height'].map(a => num(el, a)).join(',');
const problems = [];
const s = ctx.__f(1);
s.pressShops = [spec];
s.press = spec.key;
s.cardbackSettings = { image_url: '/cb.png' };
s.deck = { cards: [
  { id: 1, name: 'A', include: true, quantity: 2, thumbnail_url: '/a.png' },
  { id: 2, name: 'B // C', include: true, quantity: 1, thumbnail_url: '/b.png',
    is_dfc: true, back_name: 'C', back_thumbnail_url: '/c.png' },
  { id: 3, name: 'Fuera', include: false, quantity: 1, thumbnail_url: '/x.png' },
]};
Object.assign(s.opts, { cols: 3, rows: 3, gap_x_mm: 5, include_backs: false,
  backs_content: 'dfc_only', card_guides_enabled: true, reg_marks_enabled: true });

const pages = s.allPages;
const sequence = pages.map(p => `${p.kind}:${p.chunk[0] && p.chunk[0].thumbnail}`);
const expected = ['front:/a.png', 'back:/cb.png', 'front:/a.png', 'back:/cb.png',
                  'front:/b.png', 'back:/c.png'];
if (JSON.stringify(sequence) !== JSON.stringify(expected)) problems.push({ sequence });
for (const [idx, page] of pages.entries()) {
  const g = s.geomForKind(page.kind);
  if (g.page_w_mm !== 69 || g.page_h_mm !== 94 || g.cols !== 1 || g.rows !== 1) problems.push({ idx, g });
  const svg = s.svgMarkupForPage(page);
  const images = [...svg.matchAll(/<image [^>]*>/g)].map(m => m[0]);
  const card = images.filter(i => !i.includes('data-press-bleed')).map(box);
  const bleed = images.filter(i => i.includes('data-press-bleed')).map(box);
  if (card.length !== 1 || card[0] !== '3,3,63,88') problems.push({ idx, card });
  if (bleed.length !== 1 || bleed[0] !== '0,0,69,94') problems.push({ idx, bleed });
  const cut = [...svg.matchAll(/<rect [^>]*data-press-cut[^>]*>/g)].map(m => box(m[0]));
  if (cut.length !== 1 || cut[0] !== '3,3,63,88') problems.push({ idx, cut });
  const safe = [...svg.matchAll(/<rect [^>]*stroke="#16a34a"[^>]*>/g)].map(m => box(m[0]));
  if (safe.length !== 1 || safe[0] !== '6,6,57,82') problems.push({ idx, safe });
  if (/fill="#000"/.test(svg)) problems.push({ idx, regmarks: true });
  if (!s.pageLabel(page, idx).includes(String(page.card))) problems.push({ idx, label: s.pageLabel(page, idx) });
}
s.cardbackSettings = { image_url: null, default_image_url: null };
if (!s.pressNeedsCardback) problems.push({ needsCardback: false });
if (s.pressDfcCount !== 1) problems.push({ dfc: s.pressDfcCount });
s.setPress = ctx.__f(1).setPress;
s.$nextTick = () => {}; s._saveOpts = () => {};
s.setPress(spec.key);
if (s.press !== null || s.geomForKind('front').cols !== 3) problems.push({ toggleOff: s.press });
console.log(JSON.stringify({ pages: pages.length, problems }));
