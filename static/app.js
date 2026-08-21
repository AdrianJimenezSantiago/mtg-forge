// ============================================================================
// MPC Forge — UI helpers globales
// ============================================================================

// -----------------------------------------------------------------------
// Alpine store: toasts + confirmaciones modales
// -----------------------------------------------------------------------
document.addEventListener('alpine:init', () => {
  Alpine.store('ui', {
    // -- Toasts ------------------------------------------------------------
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

    // -- Confirmación modal (reemplazo de window.confirm) ------------------
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
        // Refrescar iconos Lucide en el modal recién montado
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

// -----------------------------------------------------------------------
// Error handler global: captura excepciones no manejadas y las muestra como
// toast. Sin esto un error silencioso en un @click hace que ese botón parezca
// "no hacer nada" desde la perspectiva del usuario.
// -----------------------------------------------------------------------
window.addEventListener('error', (e) => {
  console.error('[MPC Forge] Uncaught error:', e.error || e.message);
  try {
    Alpine.store('ui').error('Error inesperado',
      (e.error && e.error.message) || e.message || 'Revisa la consola (F12)');
  } catch (_) { /* Alpine no cargado aún */ }
});
window.addEventListener('unhandledrejection', (e) => {
  console.error('[MPC Forge] Unhandled promise rejection:', e.reason);
  try {
    Alpine.store('ui').error('Error asíncrono',
      (e.reason && e.reason.message) || String(e.reason) || 'Revisa la consola (F12)');
  } catch (_) { /* Alpine no cargado aún */ }
});

// -----------------------------------------------------------------------
// API global de backwards compatibility
// -----------------------------------------------------------------------
// window.toast(title, message, type='info')
window.toast = (title, message, type = 'info') => {
  return Alpine.store('ui').toast(title, message || '', type);
};
// window.confirmDialog(title, message, {danger, confirmLabel, cancelLabel, icon})
window.confirmDialog = (title, message = '', opts = {}) => {
  return Alpine.store('ui').confirm(title, message, opts);
};

// -----------------------------------------------------------------------
// Lucide icons helper: re-crea iconos [data-lucide] tras cambios en el DOM.
// Se llama tras cargar la página y desde componentes Alpine con $nextTick().
// -----------------------------------------------------------------------
window.icons = () => {
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
};
document.addEventListener('DOMContentLoaded', () => window.icons());
// También al arrancar Alpine (por si Lucide se cargó después)
document.addEventListener('alpine:initialized', () => window.icons());
// Observamos mutaciones para renderizar iconos añadidos dinámicamente por Alpine.
// Debounce para no llamar mil veces por segundo.
(function() {
  let scheduled = false;
  const observer = new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      // Solo llamamos si hay iconos pendientes (con data-lucide y no procesados)
      if (document.querySelector('[data-lucide]')) window.icons();
    });
  });
  document.addEventListener('DOMContentLoaded', () => {
    observer.observe(document.body, { childList: true, subtree: true });
  });
})();

// -----------------------------------------------------------------------
// Formateadores rápidos
// -----------------------------------------------------------------------
window.fmt = {
  money: (n) => '$' + Number(n || 0).toFixed(2),
  eur:   (n) => Number(n || 0).toFixed(2) + ' €',
  int:   (n) => Number(n || 0).toLocaleString(),
};

// -----------------------------------------------------------------------
// Preview grande al mantener Ctrl y hover sobre cualquier <img data-preview>
// -----------------------------------------------------------------------
(function() {
  let ctrlHeld = false;
  let currentTarget = null;
  let previewEl = null;

  function ensurePreview() {
    if (previewEl) return previewEl;
    previewEl = document.createElement('div');
    previewEl.id = 'mpc-forge-preview';
    previewEl.style.cssText = `
      position: fixed; pointer-events: none; z-index: 9999; display: none;
      width: 340px; height: 475px; border-radius: 14px; overflow: hidden;
      box-shadow: 0 10px 40px rgba(0,0,0,0.7), 0 0 0 1px rgba(212,175,55,0.15);
      background: #0b0d10; transition: opacity 0.08s ease-out;
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
    const w = previewEl.offsetWidth;
    const h = previewEl.offsetHeight;
    let x = e.clientX + pad;
    let y = e.clientY + pad;
    if (x + w > window.innerWidth) x = e.clientX - w - pad;
    if (y + h > window.innerHeight) y = e.clientY - h - pad;
    if (y < 0) y = pad;
    previewEl.style.left = x + 'px';
    previewEl.style.top = y + 'px';
  }

  function show(target, e) {
    const src = target.getAttribute('data-preview') || target.src;
    if (!src) return;
    const el = ensurePreview();
    el.querySelector('img').src = src;
    el.style.display = 'block';
    positionPreview(e);
  }

  function hide() {
    if (previewEl) previewEl.style.display = 'none';
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Control') {
      ctrlHeld = true;
      if (currentTarget) {
        const rect = currentTarget.getBoundingClientRect();
        show(currentTarget, {clientX: rect.right, clientY: rect.top});
      }
    }
  });
  document.addEventListener('keyup', (e) => {
    if (e.key === 'Control') { ctrlHeld = false; hide(); }
  });
  window.addEventListener('blur', () => { ctrlHeld = false; hide(); });

  document.addEventListener('mouseover', (e) => {
    const img = e.target.closest('img[data-preview], .card-hover-preview img, [data-preview] img');
    if (!img) return;
    currentTarget = img;
    if (ctrlHeld) show(img, e);
  });
  document.addEventListener('mousemove', (e) => {
    if (ctrlHeld && previewEl && previewEl.style.display === 'block') {
      positionPreview(e);
    }
  });
  document.addEventListener('mouseout', (e) => {
    const img = e.target.closest('img[data-preview], .card-hover-preview img, [data-preview] img');
    if (img && img === currentTarget) { currentTarget = null; hide(); }
  });
})();
