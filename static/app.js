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
// Preview grande al hacer hover sobre cualquier elemento con [data-preview]
// -----------------------------------------------------------------------
// Comportamiento tipo Moxfield: preview aparece automáticamente tras un
// pequeño delay. Ctrl+hover se mantiene como shortcut de "mostrar ya".
//
// Puntos importantes de la implementación (fueron fuentes de bugs):
//
// 1. Trackeamos SIEMPRE la última posición del ratón en `lastMouseEvent` — el
//    evento capturado en el closure del setTimeout puede quedar desfasado si
//    el usuario mueve el ratón durante el delay. Al mostrar, usamos la
//    posición más reciente.
//
// 2. `mouseout` se dispara cada vez que el ratón entra en un hijo del target
//    (bubbling raro de la spec). Usamos `relatedTarget` para saber si es una
//    salida real (fuera del elemento) o solo un movimiento interno.
//
// 3. `mouseover` sobre el mismo elemento se ignora (comparamos con currentImg).
//    Sin esto, movimientos internos cancelarían y re-crearían el timer sin
//    parar, y el preview no llegaba a aparecer.
//
// 4. `scroll` solo esconde el preview VISIBLE, no cancela el timer en curso —
//    si el timer estaba a mitad, dejamos que termine (el scroll no invalida la
//    intención de ver la carta).
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

  // Encuentra la URL del preview en el elemento o sus ancestros/descendientes
  // directos. Soportamos varios patrones:
  //   <img data-preview="url">              (patrón canónico)
  //   <button><img data-preview="url"></button>  (data-preview en el img hijo)
  //   <div data-preview="url">              (data-preview en un contenedor sin img)
  function findPreviewSrc(el) {
    if (!el) return null;
    // 1. El propio elemento
    if (el.dataset && el.dataset.preview) return el.dataset.preview;
    // 2. Un img descendiente con data-preview
    const inner = el.querySelector && el.querySelector('img[data-preview]');
    if (inner) return inner.dataset.preview;
    // 3. Fallback: el src del propio img si es <img>
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

  // Trackeo continuo de la posición del ratón (barato, con passive).
  // Sirve para (a) reposicionar el preview visible siguiendo al cursor,
  // (b) usar la posición fresca en el setTimeout cuando expira el delay.
  document.addEventListener('mousemove', (e) => {
    lastMouseEvent = e;
    if (previewEl && previewEl.style.display === 'block') {
      positionPreview(e);
    }
  }, {passive: true});

  document.addEventListener('mouseover', (e) => {
    // closest sube por el árbol; hace match si el elemento o algún ancestro
    // tiene [data-preview]. Cubre tanto <img data-preview> como wrappers.
    const el = e.target.closest('[data-preview]');
    if (el === currentImg) return;  // mismo elemento, sin cambios
    // Cambio de elemento: cancelar cualquier timer/preview anterior
    cancelAndHide();
    currentImg = el;
    if (!el) return;
    if (ctrlHeld) {
      show(el, e);
    } else {
      hoverTimer = setTimeout(() => {
        // Al expirar el delay: usar posición fresca del ratón, no la
        // capturada al inicio del hover.
        if (currentImg === el) show(el, lastMouseEvent);
      }, HOVER_DELAY_MS);
    }
  });

  document.addEventListener('mouseout', (e) => {
    if (!currentImg) return;
    // mouseout se dispara al pasar a un hijo — comprobamos relatedTarget
    // para saber si es una salida real fuera del elemento actual.
    const to = e.relatedTarget;
    if (to && currentImg.contains(to)) return;  // sigue dentro
    cancelAndHide();
    currentImg = null;
  });

  // Ctrl para mostrar instantáneamente (sin esperar delay).
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

  // Scroll y click: SOLO ocultar el preview visible, no cancelar el timer
  // en curso. Con timer en curso, el usuario probablemente sigue interesado
  // en la carta bajo el cursor — que aparezca cuando toque.
  document.addEventListener('scroll', hidePreview, {capture: true, passive: true});
  document.addEventListener('click', hidePreview);
})();
