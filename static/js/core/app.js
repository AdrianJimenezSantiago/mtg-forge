document.addEventListener('alpine:init', () => {
  Alpine.store('ui', {
    toasts: [],
    _nextId: 1,

    toast(title, message = '', type = 'info', ttl = 4500) {
      const _t = window._t || ((k) => k);
      const fallbacks = {
        success: _t('js_toast_success_fallback'),
        error:   _t('js_toast_error_fallback'),
        warning: _t('js_toast_warning_fallback'),
        info:    _t('js_toast_info_fallback'),
      };
      const cleanMsg = (message ?? '').toString().trim();
      const finalMsg = cleanMsg || fallbacks[type] || fallbacks.info;
      const id = this._nextId++;
      const t = { id, title, message: finalMsg, type, ttl, createdAt: Date.now() };
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
      const _t = window._t || ((k) => k);
      return new Promise((resolve) => {
        this.confirmState = {
          open: true,
          title,
          message,
          confirmLabel: opts.confirmLabel || _t('common_confirm'),
          cancelLabel:  opts.cancelLabel  || _t('common_cancel'),
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
    const _t = window._t || ((k) => k);
    Alpine.store('ui').error(_t('js_error_unexpected'),
      (e.error && e.error.message) || e.message || _t('js_check_console'));
  } catch (_) {}
});
window.addEventListener('unhandledrejection', (e) => {
  console.error('[MPC Forge] Unhandled promise rejection:', e.reason);
  try {
    const _t = window._t || ((k) => k);
    Alpine.store('ui').error(_t('js_error_async'),
      (e.reason && e.reason.message) || String(e.reason) || _t('js_check_console'));
  } catch (_) {}
});

// Los errores duran más en pantalla (store.error) que el resto de avisos.
window.toast = (title, message, type = 'info') => {
  const ui = Alpine.store('ui');
  return type === 'error' ? ui.error(title, message || '') : ui.toast(title, message || '', type);
};
window.confirmDialog = (title, message = '', opts = {}) => Alpine.store('ui').confirm(title, message, opts);

// --- Guardar descargas donde elija el usuario ---------------------------------
//
// Chrome y Edge abren la ventana "Guardar como" del sistema (File System Access
// API). Firefox y Safari no la tienen: ahí la descarga va donde diga el
// navegador, como siempre.
//
// `source` es la URL del fichero o una función async que lo genera y devuelve
// su URL. El navegador solo deja abrir el diálogo justo después del clic, así
// que se pregunta dónde guardar ANTES de generar: si se esperase a que acabe un
// PDF de 200 páginas, Chrome lo rechazaría por falta de gesto del usuario.
const SAVE_TYPES = {
  pdf: { description: 'PDF', accept: { 'application/pdf': ['.pdf'] } },
  zip: { description: 'ZIP', accept: { 'application/zip': ['.zip'] } },
  xml: { description: 'XML', accept: { 'application/xml': ['.xml'] } },
  txt: { description: 'Text', accept: { 'text/plain': ['.txt'] } },
  log: { description: 'Log', accept: { 'text/plain': ['.log'] } },
};

window.canChooseSaveLocation = () => typeof window.showSaveFilePicker === 'function';

// Nombre propuesto en el diálogo: el del mazo, sin caracteres que Windows rechaza.
window.saveFileName = (base, ext) => {
  const clean = String(base || '').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '_').replace(/\s+/g, ' ').trim();
  return `${(clean || 'mpc-forge').slice(0, 120)}.${ext}`;
};

function plainDownload(url, name) {
  const a = document.createElement('a');
  a.href = url;
  a.download = name || '';
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

window.saveFile = async (source, suggestedName) => {
  const _t = window._t || ((k) => k);
  const produce = typeof source === 'function' ? source : async () => source;
  if (!window.canChooseSaveLocation()) {
    const url = await produce();
    if (url) plainDownload(url, suggestedName);
    return Boolean(url);
  }

  const ext = (suggestedName || '').split('.').pop().toLowerCase();
  let handle;
  try {
    handle = await window.showSaveFilePicker({
      suggestedName,
      ...(SAVE_TYPES[ext] ? { types: [SAVE_TYPES[ext]] } : {}),
    });
  } catch (e) {
    if (e.name === 'AbortError') return false;
    // Diálogo bloqueado (sin gesto, iframe, política…): descarga de siempre.
    const url = await produce();
    if (url) plainDownload(url, suggestedName);
    return Boolean(url);
  }

  const url = await produce();
  if (!url) return false;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(await window.apiErrorDetail(r));
    const writable = await handle.createWritable();
    await r.body.pipeTo(writable);
    window.toast(_t('save_done'), handle.name, 'success');
    return true;
  } catch (e) {
    window.toast(_t('save_failed'), e.message, 'error');
    return false;
  }
};

window.icons = () => {
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
};
document.addEventListener('DOMContentLoaded', () => window.icons());
document.addEventListener('alpine:initialized', () => window.icons());

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

(function() {
  const HOVER_DELAY_MS = 120;
  let ctrlHeld = false;
  let currentImg = null;
  let previewEl = null;
  let hoverTimer = null;
  let lastMouseEvent = null;

  function upsizeUrl(url) {
    if (!url) return url;
    if (url.includes('cards.scryfall.io/thumb/')) {
      return url.replace('cards.scryfall.io/thumb/', 'cards.scryfall.io/grid/');
    }
    return url.includes('cards.scryfall.io/small/')
      ? url.replace('cards.scryfall.io/small/', 'cards.scryfall.io/normal/')
      : url;
  }

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
    const hiresSrc = upsizeUrl(src);
    const el = ensurePreview();
    const img = el.querySelector('img');
    const wasHidden = el.style.display !== 'block';
    if (img.src !== hiresSrc) img.src = hiresSrc;
    el.style.display = 'block';
    if (wasHidden) {
      el.classList.remove('fx-pop');
      void el.offsetWidth;
      el.classList.add('fx-pop');
    }
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
