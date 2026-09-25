(function () {
  'use strict';

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

  function replay(el, className) {
    if (!el || !el.classList) return;
    el.classList.remove(className);
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
    document.addEventListener('scroll', leaveActive, { capture: true, passive: true });
  })();

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
      root.classList.remove('fx-dealing');
      try { sessionStorage.setItem(DEAL_KEY, '1'); } catch (_) {}
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
        var ro = new ResizeObserver(function () { update(box); });
        ro.observe(box);
        Array.prototype.forEach.call(box.children, function (child) { ro.observe(child); });
      }
    });
    window.addEventListener('resize', function () {
      Array.prototype.forEach.call(boxes, update);
    }, { passive: true });
  })();

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
