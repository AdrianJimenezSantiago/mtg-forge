// Toast global. Se dispara un CustomEvent que la layout escucha.
window.toast = function (title, body) {
  window.dispatchEvent(new CustomEvent('toast', {detail: {title, body: body || ''}}));
};

// Formateadores rápidos.
window.fmt = {
  money: (n) => '$' + Number(n || 0).toFixed(2),
  eur: (n) => Number(n || 0).toFixed(2) + ' €',
  int: (n) => Number(n || 0).toLocaleString(),
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
      position: fixed;
      pointer-events: none;
      z-index: 9999;
      display: none;
      width: 340px;
      height: 475px;
      border-radius: 14px;
      overflow: hidden;
      box-shadow: 0 10px 40px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.08);
      background: #0b0d10;
      transition: opacity 0.08s ease-out;
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
    if (e.key === 'Control') {
      ctrlHeld = false;
      hide();
    }
  });
  window.addEventListener('blur', () => { ctrlHeld = false; hide(); });

  // Delegación: cualquier <img data-preview> o <img> dentro de un elemento con
  // clase 'card-hover-preview'.
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
    if (img && img === currentTarget) {
      currentTarget = null;
      hide();
    }
  });
})();
