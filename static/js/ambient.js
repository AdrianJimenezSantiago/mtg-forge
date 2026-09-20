/**
 * MPC Forge · fondo ambiental (motas de maná sobre una aurora tenue).
 *
 * Dos lienzos: `.fx-aurora` (1/8 de la pantalla, escalado por el navegador, se
 * redibuja 5 veces por segundo) y `.fx-ambient` (motas, 30 fps).
 *
 * Se carga como script clásico JUSTO DESPUÉS del <canvas> al principio del
 * <body>, sin `defer`: así el primer fotograma ya tiene las estrellas
 * dibujadas y la transición entre páginas no enseña un lienzo vacío.
 *
 * Continuidad entre páginas: la posición de cada mota es una función del
 * reloj (`Date.now()`) y de una semilla fija, no del tiempo desde que cargó
 * la página. Al navegar, el cielo sigue exactamente donde estaba.
 *
 * Coste: ~1 mota por cada 13.000 px² (máx. 170), a 30 fps, con el DPR
 * limitado a 1,5. Se detiene con la pestaña oculta. Con "reducir
 * movimiento" se pinta un único fotograma estático.
 *
 * El botón [data-ambient-toggle] de la barra lateral lo apaga y enciende;
 * la preferencia se guarda en localStorage ("mpc-ambient" = "off") y el
 * <head> la aplica antes del primer pintado (clase html.fx-ambient-off).
 */
(function () {
  'use strict';

  var canvas = document.querySelector('.fx-ambient');
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext('2d');
  if (!ctx) return;

  var auroraCanvas = document.querySelector('.fx-aurora');
  var actx = auroraCanvas && auroraCanvas.getContext ? auroraCanvas.getContext('2d') : null;

  var root = document.documentElement;
  var STORAGE_KEY = 'mpc-ambient';
  var SEED = 20260916;
  var FRAME_MS = 1000 / 30;
  var AURORA_FRAME_MS = 200;
  var AURORA_SCALE = 8;
  // Se dibuja con alfas ×4 y el CSS la atenúa (opacity: .25): con alfas tan
  // bajos, 8 bits por canal dejaban escalones visibles al escalar.
  var AURORA_GAIN = 4;

  // Luces de la aurora: color, alfa, radio relativo y trayectoria lenta
  // (centro + amplitud · seno(t / periodo)). Periodos de 40–70 s.
  var LIGHTS = [
    {c: [212, 175, 55], a: 0.13, r: 0.42, x: 0.78, y: 0.16, ax: 0.06, ay: 0.05, px: 47, py: 61},
    {c: [120, 90, 190], a: 0.12, r: 0.40, x: 0.20, y: 0.84, ax: 0.07, ay: 0.04, px: 58, py: 43},
    {c: [60, 150, 170], a: 0.08, r: 0.34, x: 0.28, y: 0.30, ax: 0.08, ay: 0.07, px: 69, py: 52},
    {c: [212, 175, 55], a: 0.05, r: 0.30, x: 0.72, y: 0.72, ax: 0.05, ay: 0.06, px: 53, py: 71},
  ];

  function media(q) {
    try { return !!(window.matchMedia && window.matchMedia(q).matches); } catch (_) { return false; }
  }
  var reduced = media('(prefers-reduced-motion: reduce)');
  var enabled = !root.classList.contains('fx-ambient-off');

  // PRNG determinista (mulberry32)
  function rng(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      var t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // Blanco cálido y dorado dominan; los colores de maná son un acento.
  var PALETTE = [
    [255, 244, 222], [255, 244, 222], [255, 244, 222], [255, 244, 222],
    [233, 200, 106], [233, 200, 106], [233, 200, 106],
    [140, 190, 240],  // U
    [140, 215, 150],  // G
    [240, 140, 130],  // R
    [250, 238, 200],  // W
    [185, 160, 225],  // B
  ];

  var W = 0;
  var H = 0;
  var dpr = 1;
  var motes = [];

  function build() {
    dpr = Math.min(1.5, window.devicePixelRatio || 1);
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (actx) {
      auroraCanvas.width = Math.max(16, Math.round(W / AURORA_SCALE));
      auroraCanvas.height = Math.max(9, Math.round(H / AURORA_SCALE));
    }

    // Misma semilla y mismas dimensiones de referencia → mismas motas en
    // todas las páginas. Se generan sobre un lienzo de referencia grande y
    // se envuelven con módulo, así redimensionar no las reordena.
    var rand = rng(SEED);
    var count = Math.max(50, Math.min(170, Math.round((W * H) / 13000)));
    motes = [];
    for (var i = 0; i < count; i++) {
      var z = 0.25 + rand() * 0.75;             // profundidad (1 = cerca)
      var glint = rand() < 0.08;
      motes.push({
        x0: rand() * 4000,
        y0: rand() * 4000,
        z: z,
        r: glint ? 1.1 + rand() * 0.8 : 0.45 + rand() * 0.95 * z,
        vx: (rand() - 0.5) * 3 * z,              // px/s
        vy: -(1.5 + rand() * 5) * z,             // suben despacio
        tw: 0.4 + rand() * 1.6,                  // velocidad de titileo
        ph: rand() * Math.PI * 2,
        a: glint ? 0.9 : 0.32 + rand() * 0.5,
        c: PALETTE[Math.floor(rand() * PALETTE.length)],
        glint: glint,
      });
    }
  }

  // Parallax suave (ratón y scroll)
  var px = 0, py = 0, tx = 0, ty = 0;
  window.addEventListener('pointermove', function (e) {
    if (e.pointerType && e.pointerType !== 'mouse') return;
    tx = (e.clientX / (W || 1) - 0.5) * 2;
    ty = (e.clientY / (H || 1) - 0.5) * 2;
  }, { passive: true });

  function mod(n, m) { return ((n % m) + m) % m; }

  // Estrella fugaz ocasional (no determinista: es un detalle, no el cielo)
  var shooting = null;
  var nextShot = performance.now() + 14000 + Math.random() * 20000;

  function drawAurora() {
    if (!actx) return;
    var t = Date.now() / 1000;
    var w = auroraCanvas.width;
    var h = auroraCanvas.height;
    actx.clearRect(0, 0, w, h);
    actx.globalCompositeOperation = 'lighter';
    for (var i = 0; i < LIGHTS.length; i++) {
      var L = LIGHTS[i];
      var cx = (L.x + L.ax * Math.sin((t / L.px) * Math.PI * 2)) * w;
      var cy = (L.y + L.ay * Math.cos((t / L.py) * Math.PI * 2)) * h;
      var rad = L.r * Math.max(w, h);
      var g = actx.createRadialGradient(cx, cy, 0, cx, cy, rad);
      var a = Math.min(1, L.a * AURORA_GAIN);
      g.addColorStop(0, 'rgba(' + L.c[0] + ',' + L.c[1] + ',' + L.c[2] + ',' + a + ')');
      g.addColorStop(0.5, 'rgba(' + L.c[0] + ',' + L.c[1] + ',' + L.c[2] + ',' + (a * 0.4) + ')');
      g.addColorStop(1, 'rgba(' + L.c[0] + ',' + L.c[1] + ',' + L.c[2] + ',0)');
      actx.fillStyle = g;
      actx.fillRect(0, 0, w, h);
    }
    actx.globalCompositeOperation = 'source-over';
  }

  function drawGlint(x, y, r, alpha, c) {
    var len = r * 5.5;
    var g = ctx.createRadialGradient(x, y, 0, x, y, r * 4);
    g.addColorStop(0, 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + (alpha * 0.55) + ')');
    g.addColorStop(1, 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',0)');
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(x, y, r * 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + (alpha * 0.5) + ')';
    ctx.lineWidth = 0.6;
    ctx.beginPath();
    ctx.moveTo(x - len, y); ctx.lineTo(x + len, y);
    ctx.moveTo(x, y - len); ctx.lineTo(x, y + len);
    ctx.stroke();
  }

  function draw(nowMs) {
    var t = Date.now() / 1000;
    px += (tx - px) * 0.04;
    py += (ty - py) * 0.04;
    var scroll = window.scrollY || 0;

    ctx.clearRect(0, 0, W, H);
    for (var i = 0; i < motes.length; i++) {
      var m = motes[i];
      var x = mod(m.x0 + m.vx * t - px * 12 * m.z, W + 20) - 10;
      var y = mod(m.y0 + m.vy * t - py * 8 * m.z - scroll * 0.05 * m.z, H + 20) - 10;
      var twinkle = reduced ? 0.8 : 0.55 + 0.45 * Math.sin(t * m.tw + m.ph);
      var alpha = m.a * twinkle;
      var c = m.c;
      if (m.glint) {
        drawGlint(x, y, m.r, alpha, c);
      }
      ctx.fillStyle = 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + alpha + ')';
      ctx.beginPath();
      ctx.arc(x, y, m.r, 0, Math.PI * 2);
      ctx.fill();
    }

    if (reduced) return;
    if (!shooting && nowMs > nextShot) {
      shooting = {
        start: nowMs,
        x: W * (0.25 + Math.random() * 0.6),
        y: H * (0.05 + Math.random() * 0.3),
        dx: -(260 + Math.random() * 160),
        dy: 110 + Math.random() * 80,
      };
    }
    if (shooting) {
      var k = (nowMs - shooting.start) / 900;
      if (k >= 1) {
        shooting = null;
        nextShot = nowMs + 18000 + Math.random() * 26000;
      } else {
        var hx = shooting.x + shooting.dx * k;
        var hy = shooting.y + shooting.dy * k;
        var fade = Math.sin(Math.PI * k);
        var grad = ctx.createLinearGradient(hx, hy, hx - shooting.dx * 0.35, hy - shooting.dy * 0.35);
        grad.addColorStop(0, 'rgba(255,246,214,' + (0.55 * fade) + ')');
        grad.addColorStop(1, 'rgba(233,200,106,0)');
        ctx.strokeStyle = grad;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(hx, hy);
        ctx.lineTo(hx - shooting.dx * 0.35, hy - shooting.dy * 0.35);
        ctx.stroke();
      }
    }
  }

  var rafId = 0;
  var last = 0;
  var lastAurora = 0;
  function loop(now) {
    rafId = requestAnimationFrame(loop);
    if (now - lastAurora >= AURORA_FRAME_MS) {
      lastAurora = now;
      drawAurora();
    }
    if (now - last < FRAME_MS) return;
    last = now;
    draw(now);
  }

  function paintAll() {
    drawAurora();
    draw(performance.now());
  }

  function start() {
    if (!enabled) return;
    if (reduced) { paintAll(); return; }
    if (!rafId) rafId = requestAnimationFrame(loop);
  }
  function stop() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
  }

  build();
  if (enabled) paintAll();   // primer fotograma, ya mismo
  start();

  var resizeTimer = 0;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      build();
      if (enabled) paintAll();
    }, 150);
  });

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop(); else start();
  });

  // ── Interruptor ────────────────────────────────────────────────────────
  function syncToggles() {
    var buttons = document.querySelectorAll('[data-ambient-toggle]');
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
      var label = btn.getAttribute(enabled ? 'data-label-on' : 'data-label-off');
      if (label) {
        btn.setAttribute('title', label);
        btn.setAttribute('aria-label', label);
      }
    });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('[data-ambient-toggle]') : null;
    if (!btn) return;
    enabled = !enabled;
    root.classList.toggle('fx-ambient-off', !enabled);
    try {
      if (enabled) localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, 'off');
    } catch (_) { /* sin almacenamiento: vale para esta página */ }
    if (enabled) {
      paintAll();
      start();
    } else {
      stop();
      ctx.clearRect(0, 0, W, H);
    }
    syncToggles();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', syncToggles, { once: true });
  } else {
    syncToggles();
  }
})();
