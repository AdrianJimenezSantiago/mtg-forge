document.addEventListener('alpine:init', () => {
  Alpine.store('ui', {
    toasts: [],
    _nextId: 1,

    toast(title, message = '', type = 'info', ttl = 4500) {
      const id = this._nextId++;
      const t = { id, title, message, type, ttl, createdAt: Date.now() };
      this.toasts.push(t);
      if (ttl > 0) {
        setTimeout(() => this.dismissToast(id), ttl);
      }
      return id;
    },
    success(title, message, ttl) { return this.toast(title, message, 'success', ttl); },
    error(title, message, ttl)   { return this.toast(title, message, 'error', ttl ?? 7000); },
    warn(title, message, ttl)    { return this.toast(title, message, 'warning', ttl); },
    info(title, message, ttl)    { return this.toast(title, message, 'info', ttl); },
    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },

    confirmState: {
      open: false,
      title: '',
      message: '',
      confirmLabel: 'Confirmar',
      cancelLabel: 'Cancelar',
      danger: false,
      icon: 'help-circle',
      _resolve: null,
    },

    async confirm(title, message = '', opts = {}) {
      return new Promise((resolve) => {
        this.confirmState = {
          open: true,
          title,
          message,
          confirmLabel: opts.confirmLabel || 'Confirmar',
          cancelLabel:  opts.cancelLabel  || 'Cancelar',
          danger:       !!opts.danger,
          icon:         opts.icon || (opts.danger ? 'alert-triangle' : 'help-circle'),
          _resolve:     resolve,
        };
        this.$nextTick?.(() => window.icons?.());
      });
    },
    _closeConfirm(value) {
      const r = this.confirmState._resolve;
      this.confirmState.open = false;
      this.confirmState._resolve = null;
      if (r) r(value);
    },
  });
});

window.addEventListener('error', (e) => {
  console.error('[MPC Forge] Uncaught error:', e.error || e.message);
  try {
    Alpine.store('ui').error('Error inesperado',
      (e.error && e.error.message) || e.message || 'Revisa la consola (F12)');
  } catch (_) {}
});
window.addEventListener('unhandledrejection', (e) => {
  console.error('[MPC Forge] Unhandled promise rejection:', e.reason);
  try {
    Alpine.store('ui').error('Error asíncrono',
      (e.reason && e.reason.message) || String(e.reason) || 'Revisa la consola (F12)');
  } catch (_) {}
});

window.toast = (title, message, type = 'info') => Alpine.store('ui').toast(title, message || '', type);
window.confirmDialog = (title, message = '', opts = {}) => Alpine.store('ui').confirm(title, message, opts);

window.icons = () => {
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
};
document.addEventListener('DOMContentLoaded', () => window.icons());
document.addEventListener('alpine:initialized', () => window.icons());

// Fix #14: el MutationObserver solo dispara window.icons() si el batch de
// mutaciones incluye al menos un nodo con [data-lucide] aún sin procesar
// (i.e. sin el atributo stroke que Lucide añade al renderizar). Antes
// llamaba document.querySelector('[data-lucide]') en cada rAF, lo que
// recorre el DOM entero aunque no haya iconos nuevos. Ahora filtramos
// los addedNodes directamente en el observer, que ya los tiene disponibles.
(function () {
  let scheduled = false;
  let hasPendingIcons = false;

  const observer = new MutationObserver((mutations) => {
    if (!hasPendingIcons) {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType !== 1) continue;
          if (
            (node.hasAttribute && node.hasAttribute('data-lucide') && !node.hasAttribute('stroke')) ||
            (node.querySelector && node.querySelector('[data-lucide]:not([stroke])'))
          ) {
            hasPendingIcons = true;
            break;
          }
        }
        if (hasPendingIcons) break;
      }
    }
    if (!hasPendingIcons || scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      if (hasPendingIcons) {
        hasPendingIcons = false;
        window.icons();
      }
    });
  });

  document.addEventListener('DOMContentLoaded', () => {
    observer.observe(document.body, { childList: true, subtree: true });
  });
})();

window.fmt = {
  money: (n) => '$' + Number(n || 0).toFixed(2),
  eur:   (n) => Number(n || 0).toFixed(2) + ' €',
  int:   (n) => Number(n || 0).toLocaleString(),
};

// Fix #15: los listeners de hover se delegan en document pero la lógica
// pesada (closest + findPreviewSrc) solo se ejecuta cuando el evento viene
// de dentro de un contenedor que contiene al menos un [data-preview]. El
// check rápido con e.target.closest('[data-preview]') ya está — el coste
// principal era que mouseover/mouseout se disparaban en CUALQUIER movimiento
// sobre la página, incluyendo áreas sin cartas. Añadimos un guard temprano
// que descarta el evento si el target no tiene ningún ancestro con
// [data-preview], evitando el traversal innecesario en la mayoría de casos.
(function() {
  const HOVER_DELAY_MS = 180;
  let ctrlHeld = false;
  let currentImg = null;
  let previewEl = null;
  let hoverTimer = null;
  let lastMouseEvent = null;

  function ensurePreview() {
    if (previewEl) return previewEl;
    previewEl = document.createElement('div');
    previewEl.id = 'mpc-forge-preview';
    previewEl.style.cssText = `
      position: fixed; pointer-events: none; z-index: 9999; display: none;
      width: 340px; height: 475px; border-radius: 14px; overflow: hidden;
      box-shadow: 0 10px 40px rgba(0,0,0,0.7), 0 0 0 1px rgba(212,175,55,0.15);
      background: #0b0d10;
    `;
    const img = document.createElement('img');
    img.style.cssText = 'width: 100%; height: 100%; object-fit: cover; object-position: center;';
    previewEl.appendChild(img);
    document.body.appendChild(previewEl);
    return previewEl;
  }

  function positionPreview(e) {
    if (!previewEl) return;
    const pad = 20;
    const w = previewEl.offsetWidth || 340;
    const h = previewEl.offsetHeight || 475;
    let x = e.clientX + pad;
    let y = e.clientY + pad;
    if (x + w > window.innerWidth) x = e.clientX - w - pad;
    if (y + h > window.innerHeight) y = e.clientY - h - pad;
    if (y < 0) y = pad;
    if (x < 0) x = pad;
    previewEl.style.left = x + 'px';
    previewEl.style.top = y + 'px';
  }

  function findPreviewSrc(el) {
    if (!el) return null;
    if (el.dataset && el.dataset.preview) return el.dataset.preview;
    const inner = el.querySelector && el.querySelector('img[data-preview]');
    if (inner) return inner.dataset.preview;
    if (el.tagName === 'IMG' && el.src) return el.src;
    return null;
  }

  function show(target, e) {
    const src = findPreviewSrc(target);
    if (!src) return;
    const el = ensurePreview();
    const img = el.querySelector('img');
    if (img.src !== src) img.src = src;
    el.style.display = 'block';
    positionPreview(e || lastMouseEvent || {clientX: 0, clientY: 0});
  }

  function hidePreview() {
    if (previewEl) previewEl.style.display = 'none';
  }

  function cancelAndHide() {
    clearTimeout(hoverTimer);
    hoverTimer = null;
    hidePreview();
  }

  document.addEventListener('mousemove', (e) => {
    lastMouseEvent = e;
    if (previewEl && previewEl.style.display === 'block') positionPreview(e);
  }, {passive: true});

  document.addEventListener('mouseover', (e) => {
    // Guard rápido: si no hay ningún [data-preview] en el camino del evento,
    // salimos antes de hacer el closest completo.
    if (!currentImg && !e.target.closest('[data-preview]')) return;

    const el = e.target.closest('[data-preview]');
    if (el === currentImg) return;
    cancelAndHide();
    currentImg = el;
    if (!el) return;
    if (ctrlHeld) {
      show(el, e);
    } else {
      hoverTimer = setTimeout(() => {
        if (currentImg === el) show(el, lastMouseEvent);
      }, HOVER_DELAY_MS);
    }
  });

  document.addEventListener('mouseout', (e) => {
    if (!currentImg) return;
    const to = e.relatedTarget;
    if (to && currentImg.contains(to)) return;
    cancelAndHide();
    currentImg = null;
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Control' && !ctrlHeld) {
      ctrlHeld = true;
      if (currentImg && hoverTimer !== null) {
        clearTimeout(hoverTimer);
        hoverTimer = null;
        show(currentImg, lastMouseEvent);
      }
    }
  });
  document.addEventListener('keyup', (e) => {
    if (e.key === 'Control') ctrlHeld = false;
  });
  window.addEventListener('blur', () => {
    ctrlHeld = false;
    cancelAndHide();
    currentImg = null;
  });

  document.addEventListener('scroll', hidePreview, {capture: true, passive: true});
  document.addEventListener('click', hidePreview);
})();
