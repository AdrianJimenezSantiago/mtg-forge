(function () {
  var root = document.documentElement;

  try {
    if (localStorage.getItem('mpc-ambient') === 'off') root.classList.add('fx-ambient-off');
  } catch (_) {}

  try {
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reduce && location.pathname === '/decks') {
      var navEntry = performance.getEntriesByType && performance.getEntriesByType('navigation')[0];
      if ((navEntry && navEntry.type === 'reload') || !sessionStorage.getItem('mpc-dealt')) {
        root.classList.add('fx-dealing');
        setTimeout(function () { root.classList.remove('fx-dealing'); }, 2000);
      }
    }
  } catch (_) {}

  var DECK_PATH = /^\/decks\/(\d+)\/?$/;
  var deckIdFromUrl = function (url) {
    try {
      var m = DECK_PATH.exec(new URL(url, location.href).pathname);
      return m ? m[1] : null;
    } catch (_) { return null; }
  };
  var nameCover = function (id, transition) {
    var cover = id && document.querySelector('[data-deck-cover="' + id + '"]');
    if (!cover) return;
    cover.style.viewTransitionName = 'deck-cover';
    transition.finished.finally(function () { cover.style.viewTransitionName = ''; });
  };
  window.addEventListener('pageswap', function (e) {
    if (e.viewTransition && e.activation && e.activation.entry) {
      nameCover(deckIdFromUrl(e.activation.entry.url), e.viewTransition);
    }
  });
  window.addEventListener('pagereveal', function (e) {
    document.querySelectorAll('[data-deck-cover]').forEach(function (el) {
      el.style.viewTransitionName = '';
    });
    var from = window.navigation && window.navigation.activation && window.navigation.activation.from;
    if (e.viewTransition && from) {
      nameCover(deckIdFromUrl(from.url), e.viewTransition);
    }
  });
})();
