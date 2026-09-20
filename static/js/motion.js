/**
 * MPC Forge · capa de movimiento (parte interactiva).
 *
 * Complemento de /static/motion.css. Aquí solo vive lo que necesita saber
 * dónde está el cursor o cuándo cambia un valor; el aspecto está en el CSS.
 *
 * Qué aporta
 * ----------
 *   [data-tilt]            Inclinación 3D + reflejo que sigue al cursor.
 *                          `data-tilt="foil"` añade la película de colores.
 *                          `data-tilt-max="8"` limita el ángulo (grados).
 *   [data-deal]            Reparte sus hijos como cartas al cargar la página
 *                          (una vez por sesión, o al recargar).
 *   [data-reveal]          Añade .is-revealed la primera vez que el elemento
 *                          entra en pantalla (la animación la define el CSS).
 *   [data-scroll-fade]     Contenedor con scroll propio: recibe .fx-more-below
 *                          mientras quede contenido por debajo.
 *   .bg-accent (botones)   Destello que nace en el punto exacto del clic.
 *
 *   Directivas de Alpine:
 *   x-auto-animate         FLIP automático de la lista (entradas, salidas,
 *                          reordenaciones) con @formkit/auto-animate.
 *                          `.lazy` no anima la primera carga de datos.
 *                          `.toasts` usa la variante para notificaciones.
 *   x-flip="expr"          Voltea el elemento cuando `expr` cambia (p. ej. al
 *                          elegir un arte nuevo). Si hay un ancestro con
 *                          [data-flash-row] lo ilumina un instante.
 *   x-tween.N="expr"       Interpola un número hasta su nuevo valor con N
 *                          decimales. Sufijo opcional en data-suffix.
 *                          `.from0` cuenta desde 0 también la primera vez.
 *
 * Por qué es un script clásico y no un módulo: igual que app.js. Se ejecuta
 * durante el parseo, antes que el `defer` de Alpine, así que el listener de
 * `alpine:init` está registrado a tiempo. Ver la nota de api.js.
 *
 * Todo degrada en silencio: sin Element.animate, sin ResizeObserver (jsdom),
 * o con "reducir movimiento" activado en el sistema, las funciones no hacen
 * nada y la interfaz se comporta exactamente como antes.
 */
(function () {
  'use strict';

  // ── Utilidades ─────────────────────────────────────────────────────────

  function media(query) {
    try {
      return !!(window.matchMedia && window.matchMedia(query).matches);
    } catch (_) {
      return false;
    }
  }

  function prefersReducedMotion() {
    return media('(prefers-reduced-motion: reduce)');
  }

  function hasFinePointer() {
    return media('(hover: hover) and (pointer: fine)');
  }

  var canAnimate =
    typeof Element !== 'undefined' && typeof Element.prototype.animate === 'function';

  var raf = typeof window.requestAnimationFrame === 'function'
    ? window.requestAnimationFrame.bind(window)
    : null;

  /**
   * Relanza una animación CSS basada en clase aunque ya se estuviera
   * ejecutando, y retira la clase al terminar para no dejar estado colgado.
   */
  function replay(el, className) {
    if (!el || !el.classList) return;
    el.classList.remove(className);
    // Forzar reflow: sin esto el navegador fusiona quitar+poner y no relanza.
    void el.offsetWidth;
    el.classList.add(className);
    var done = function (ev) {
      if (ev.target !== el) return;
      el.classList.remove(className);
      el.removeEventListener('animationend', done);
      el.removeEventListener('animationcancel', done);
    };
    el.addEventListener('animationend', done);
    el.addEventListener('animationcancel', done);
  }

  var EASE_OUT = 'cubic-bezier(0.16, 1, 0.3, 1)';
  var EASE_IN = 'cubic-bezier(0.55, 0, 1, 0.45)';

  // ── 1 · Inclinación 3D de cartas ───────────────────────────────────────

  (function tilt() {
    var active = null;
    var rect = null;
    var maxDeg = 10;
    var pending = null;
    var scheduled = false;

    function release(el) {
      el.classList.remove('is-tilting');
      el.style.setProperty('--fx-rx', '0deg');
      el.style.setProperty('--fx-ry', '0deg');
      el.style.setProperty('--fx-hover', '0');
    }

    function paint() {
      scheduled = false;
      if (!active || !pending || !rect) return;
      var px = (pending.x - rect.left) / rect.width;
      var py = (pending.y - rect.top) / rect.height;
      px = Math.min(1, Math.max(0, px));
      py = Math.min(1, Math.max(0, py));
      active.style.setProperty('--fx-rx', ((0.5 - py) * 2 * maxDeg).toFixed(2) + 'deg');
      active.style.setProperty('--fx-ry', ((px - 0.5) * 2 * maxDeg).toFixed(2) + 'deg');
      active.style.setProperty('--fx-mx', (px * 100).toFixed(1) + '%');
      active.style.setProperty('--fx-my', (py * 100).toFixed(1) + '%');
      active.style.setProperty('--fx-hover', '1');
    }

    function leaveActive() {
      if (active) release(active);
      active = null;
      rect = null;
    }

    document.addEventListener('pointermove', function (e) {
      if (e.pointerType && e.pointerType !== 'mouse') return;
      var target = e.target && e.target.closest ? e.target.closest('[data-tilt]') : null;
      if (target !== active) {
        leaveActive();
        if (!target || prefersReducedMotion() || !hasFinePointer()) return;
        active = target;
        // El rectángulo se toma al entrar, antes de inclinar: medirlo en
        // cada frame devolvería la caja ya rotada y la carta temblaría.
        rect = target.getBoundingClientRect();
        var m = parseFloat(target.getAttribute('data-tilt-max'));
        maxDeg = isFinite(m) ? m : 10;
        target.classList.add('is-tilting');
      }
      if (!active) return;
      pending = { x: e.clientX, y: e.clientY };
      if (!scheduled && raf) {
        scheduled = true;
        raf(paint);
      }
    }, { passive: true });

    document.documentElement.addEventListener('pointerleave', leaveActive);
    window.addEventListener('blur', leaveActive);
    // Al hacer scroll la caja cacheada deja de ser válida.
    document.addEventListener('scroll', leaveActive, { capture: true, passive: true });
  })();

  // ── 2 · Reparto de cartas al cargar ────────────────────────────────────

  (function deal() {
    var root = document.documentElement;
    var DEAL_KEY = 'mpc-dealt';
    var MAX_DEALT = 16;

    function run() {
      if (!root.classList.contains('fx-dealing')) return;
      var containers = document.querySelectorAll('[data-deal]');
      var vh = window.innerHeight || 800;
      containers.forEach(function (container) {
        var i = 0;
        Array.prototype.forEach.call(container.children, function (child) {
          if (i >= MAX_DEALT) return;
          var r = child.getBoundingClientRect();
          if (r.top > vh) return;
          child.style.setProperty('--fx-i', String(i));
          i += 1;
          replay(child, 'fx-deal');
        });
      });
      // En el mismo frame que las clases: el `fill-mode: both` del keyframe
      // mantiene las cartas ocultas durante su retardo.
      root.classList.remove('fx-dealing');
      try { sessionStorage.setItem(DEAL_KEY, '1'); } catch (_) { /* modo privado */ }
    }

    if (!canAnimate || prefersReducedMotion()) {
      root.classList.remove('fx-dealing');
      return;
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', run, { once: true });
    } else {
      run();
    }
  })();

  // ── 2b · Aparición al entrar en pantalla ──────────────────────────────
  // [data-reveal] recibe .is-revealed la primera vez que se ve. El estado
  // oculto solo existe bajo html.fx-reveal-ready, que se pone aquí: sin JS,
  // sin IntersectionObserver o con movimiento reducido, todo se ve desde el
  // principio.

  (function reveal() {
    var targets = document.querySelectorAll('[data-reveal]');
    if (!targets.length) return;
    var showAll = function () {
      Array.prototype.forEach.call(targets, function (el) { el.classList.add('is-revealed'); });
    };
    if (!canAnimate || prefersReducedMotion() || typeof IntersectionObserver === 'undefined') {
      showAll();
      return;
    }
    document.documentElement.classList.add('fx-reveal-ready');
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-revealed');
        io.unobserve(entry.target);
      });
    }, { threshold: 0.2, rootMargin: '0px 0px -6% 0px' });
    Array.prototype.forEach.call(targets, function (el) { io.observe(el); });
  })();

  // ── 2c · Aviso de "hay más abajo" en contenedores con scroll ─────────────
  // La barra lateral es fija y, en ventanas bajas, su navegación hace scroll
  // interno. Sin una pista visual, la lista de recientes y el buscador
  // quedarían ocultos sin que nada lo indique.

  (function scrollFade() {
    var boxes = document.querySelectorAll('[data-scroll-fade]');
    if (!boxes.length) return;
    var update = function (box) {
      var more = box.scrollHeight - box.clientHeight - box.scrollTop > 4;
      box.classList.toggle('fx-more-below', more);
    };
    Array.prototype.forEach.call(boxes, function (box) {
      update(box);
      box.addEventListener('scroll', function () { update(box); }, { passive: true });
      if (typeof ResizeObserver !== 'undefined') {
        // El contenido crece al llegar los datos (recientes, resultados).
        var ro = new ResizeObserver(function () { update(box); });
        ro.observe(box);
        Array.prototype.forEach.call(box.children, function (child) { ro.observe(child); });
      }
    });
    window.addEventListener('resize', function () {
      Array.prototype.forEach.call(boxes, update);
    }, { passive: true });
  })();

  // ── 3 · Destello de los botones primarios ──────────────────────────────

  document.addEventListener('pointerdown', function (e) {
    var btn = e.target && e.target.closest
      ? e.target.closest('button.bg-accent, a.bg-accent')
      : null;
    if (!btn || btn.disabled || prefersReducedMotion()) return;
    var r = btn.getBoundingClientRect();
    btn.style.setProperty('--fx-bx', (e.clientX - r.left) + 'px');
    btn.style.setProperty('--fx-by', (e.clientY - r.top) + 'px');
    replay(btn, 'fx-burst');
  }, { passive: true });

  // ── 4 · Directivas de Alpine ───────────────────────────────────────────

  /** Toasts: la entrada la pinta el CSS; aquí solo salida y recolocación. */
  function toastPlugin(el, action, oldCoords, newCoords) {
    if (action === 'add') {
      return new KeyframeEffect(el, [{ opacity: 1 }, { opacity: 1 }], { duration: 1 });
    }
    if (action === 'remove') {
      return new KeyframeEffect(el, [
        { opacity: 1, transform: 'translateX(0) scale(1)' },
        { opacity: 0, transform: 'translateX(48px) scale(0.94)' },
      ], { duration: 240, easing: EASE_IN });
    }
    var dx = oldCoords.left - newCoords.left;
    var dy = oldCoords.top - newCoords.top;
    return new KeyframeEffect(el, [
      { transform: 'translate(' + dx + 'px, ' + dy + 'px)' },
      { transform: 'translate(0, 0)' },
    ], { duration: 380, easing: EASE_OUT });
  }

  function hasContentChildren(el) {
    for (var i = 0; i < el.children.length; i += 1) {
      if (el.children[i].tagName !== 'TEMPLATE') return true;
    }
    return false;
  }

  document.addEventListener('alpine:init', function () {
    var Alpine = window.Alpine;
    if (!Alpine || typeof Alpine.directive !== 'function') return;

    Alpine.directive('auto-animate', function (el, meta, utils) {
      var modifiers = meta.modifiers || [];
      if (
        typeof window.autoAnimate !== 'function' ||
        !canAnimate ||
        typeof ResizeObserver === 'undefined' ||
        typeof KeyframeEffect === 'undefined' ||
        prefersReducedMotion()
      ) {
        return;
      }

      var controller = null;
      var observer = null;
      var settleTimer = 0;
      var fallbackTimer = 0;

      function attach() {
        if (controller || !el.isConnected) return;
        var config = modifiers.indexOf('toasts') !== -1
          ? toastPlugin
          : { duration: 280, easing: EASE_OUT };
        controller = window.autoAnimate(el, config);
      }

      if (modifiers.indexOf('lazy') !== -1) {
        // La lista se rellena de golpe al llegar los datos (cien cartas a la
        // vez). Animar eso sería ruido: se engancha cuando lleva un rato
        // quieta, y a partir de ahí anima solo lo que el usuario provoca.
        var settle = function () {
          clearTimeout(settleTimer);
          settleTimer = setTimeout(function () {
            if (observer) observer.disconnect();
            attach();
          }, 350);
        };
        observer = new MutationObserver(function () {
          if (hasContentChildren(el)) settle();
        });
        observer.observe(el, { childList: true });
        if (hasContentChildren(el)) settle();
        fallbackTimer = setTimeout(settle, 1500);
      } else {
        attach();
      }

      utils.cleanup(function () {
        clearTimeout(settleTimer);
        clearTimeout(fallbackTimer);
        if (observer) observer.disconnect();
        if (controller && typeof controller.destroy === 'function') controller.destroy();
      });
    });

    Alpine.directive('flip', function (el, meta, utils) {
      var getValue = utils.evaluateLater(meta.expression);
      var first = true;
      var last;

      function play() {
        if (!canAnimate || prefersReducedMotion()) return;
        replay(el, 'fx-flip');
        var row = el.closest ? el.closest('[data-flash-row]') : null;
        if (row) replay(row, 'fx-flash');
      }

      utils.effect(function () {
        getValue(function (value) {
          // Pasar de "sin valor" a un valor es la carga inicial (el dato llega
          // por fetch después de montar la vista), no un cambio del usuario.
          if (first || last == null || last === '') {
            first = false;
            last = value;
            return;
          }
          if (value === last || value == null || value === '') {
            last = value;
            return;
          }
          last = value;
          // Esperar a que la imagen nueva esté decodificada: si no, el
          // volteo enseña la vieja y cambia de golpe a mitad de giro.
          var img = el.tagName === 'IMG' ? el : el.querySelector('img');
          if (img && !img.complete) {
            var fired = false;
            var go = function () {
              if (fired) return;
              fired = true;
              img.removeEventListener('load', go);
              play();
            };
            img.addEventListener('load', go);
            setTimeout(go, 1200);
          } else {
            play();
          }
        });
      });
    });

    Alpine.directive('tween', function (el, meta, utils) {
      var getValue = utils.evaluateLater(meta.expression);
      var decimals = 0;
      var fromZero = false;
      (meta.modifiers || []).forEach(function (m) {
        if (/^\d+$/.test(m)) decimals = parseInt(m, 10);
        if (m === 'from0') fromZero = true;
      });
      var suffix = el.getAttribute('data-suffix') || '';
      var current = null;
      var frame = 0;
      var cancel = typeof window.cancelAnimationFrame === 'function'
        ? window.cancelAnimationFrame.bind(window)
        : function () {};

      function write(n) {
        el.textContent = Number(n).toFixed(decimals) + suffix;
      }

      utils.effect(function () {
        getValue(function (value) {
          var target = Number(value);
          if (!isFinite(target)) {
            el.textContent = value == null ? '' : String(value);
            current = null;
            return;
          }
          var initial = current === null;
          if ((initial && !fromZero) || !raf || prefersReducedMotion()) {
            current = target;
            write(target);
            return;
          }
          if (target === current) return;
          var from = initial ? 0 : current;
          var start = null;
          var duration = initial ? 1100 : 650;
          current = target;
          cancel(frame);
          if (!initial) replay(el, 'fx-bump');
          var step = function (now) {
            if (start === null) start = now;
            var t = Math.min(1, (now - start) / duration);
            var eased = 1 - Math.pow(1 - t, 4);
            write(from + (target - from) * eased);
            if (t < 1) frame = raf(step);
          };
          frame = raf(step);
        });
      });

      utils.cleanup(function () { cancel(frame); });
    });
  });
})();
