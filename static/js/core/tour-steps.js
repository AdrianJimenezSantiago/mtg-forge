// Guías del tutorial. Cada paso toma su texto de las claves
// tour_<guía>_<id>_title / tour_<guía>_<id>_body de locales/*.json.
//
// target: selector (o lista de alternativas) del elemento a destacar; sin target
//         el paso se muestra centrado. Si el elemento no existe o no se ve, el
//         paso se salta solo.
// dynamic: el elemento lo crea Alpine en tiempo de ejecución (no está en el HTML
//          del servidor); los tests no lo buscan en la plantilla.
(function () {
  'use strict'

  const TOURS = {
    welcome: {
      steps: [
        { id: 'intro', icon: 'sparkles', hero: true },
        { id: 'home', target: '[data-tour="nav-home"]', icon: 'house', placement: 'right' },
        { id: 'decks', target: '[data-tour="nav-decks"]', icon: 'layers', placement: 'right' },
        { id: 'collection', target: '[data-tour="nav-collection"]', icon: 'library', placement: 'right' },
        { id: 'library', target: '[data-tour="nav-library"]', icon: 'images', placement: 'right' },
        { id: 'planner', target: '[data-tour="nav-planner"]', icon: 'package', placement: 'right' },
        { id: 'calibrate', target: '[data-tour="nav-calibrate"]', icon: 'crosshair', placement: 'right' },
        { id: 'history', target: '[data-tour="nav-history"]', icon: 'history', placement: 'right' },
        { id: 'settings', target: '[data-tour="nav-settings"]', icon: 'settings', placement: 'right' },
        { id: 'search', target: '[data-tour="nav-search"]', icon: 'search', placement: 'right' },
        { id: 'lang', target: '[data-tour="nav-lang"]', icon: 'globe', placement: 'right' },
        { id: 'replay', target: '[data-tour="nav-tour"]', icon: 'graduation-cap', placement: 'right' },
        { id: 'done', icon: 'party-popper', hero: true },
      ],
    },

    home: {
      steps: [
        { id: 'import', target: '[data-tour="home-import"]', icon: 'link' },
        { id: 'text', target: '[data-tour="home-text"]', icon: 'file-text' },
        { id: 'hand', target: '[data-tour="home-hand"]', icon: 'layers' },
        { id: 'recent', target: '[data-tour="home-recent"]', icon: 'clock' },
        { id: 'flow', target: '[data-tour="home-flow"]', icon: 'route' },
        { id: 'tools', target: '[data-tour="home-tools"]', icon: 'wrench' },
        { id: 'status', target: '[data-tour="home-status"]', icon: 'activity' },
      ],
    },

    decks: {
      steps: [
        { id: 'url', target: '[data-tour="decks-import-url"]', icon: 'download' },
        { id: 'text', target: '[data-tour="decks-import-text"]', icon: 'file-text' },
        { id: 'list', target: ['[data-tour="decks-grid"]', '[data-tour="decks-empty"]'], icon: 'layers' },
        { id: 'menu', target: '[data-tour="decks-card"]', icon: 'more-vertical' },
      ],
    },

    deck: {
      steps: [
        { id: 'title', target: '[data-tour="deck-title"]', icon: 'pencil' },
        { id: 'cost', target: '[data-tour="deck-cost"]', icon: 'euro' },
        { id: 'stock', target: '[data-tour="deck-stock"]', icon: 'layers' },
        { id: 'filters', target: '[data-tour="deck-filters"]', icon: 'search' },
        { id: 'art', target: '[data-tour="deck-row-art"]', icon: 'image' },
        { id: 'upload', target: '[data-tour="deck-row-upload"]', icon: 'upload' },
        { id: 'actions', target: '[data-tour="deck-row-actions"]', icon: 'sliders-horizontal' },
        { id: 'add', target: '[data-tour="deck-add"]', icon: 'plus' },
        { id: 'tools', target: '[data-tour="deck-tools"]', icon: 'wrench' },
        { id: 'xml', target: '[data-tour="deck-xml"]', icon: 'code' },
        { id: 'pdf', target: '[data-tour="deck-pdf"]', icon: 'file-text' },
      ],
    },

    pdf: {
      steps: [
        { id: 'intro', icon: 'printer', hero: true },
        { id: 'presets', target: '[data-tour="pdf-presets"]', icon: 'wand-2' },
        { id: 'page', target: '[data-tour="pdf-page"]', icon: 'mouse-pointer-click', placement: 'right' },
        { id: 'settings', target: '[data-tour="pdf-settings"]', icon: 'sliders-horizontal', placement: 'left' },
        { id: 'cardback', target: '[data-tour="pdf-cardback"]', icon: 'images', placement: 'left' },
        { id: 'guide', target: '[data-tour="pdf-guide"]', icon: 'circle-help' },
        { id: 'build', target: '[data-tour="pdf-build"]', icon: 'download', placement: 'right' },
      ],
    },

    proof: {
      steps: [
        { id: 'intro', target: '[data-tour="proof-header"]', icon: 'eye' },
        { id: 'controls', target: '[data-tour="proof-controls"]', icon: 'zoom-in' },
      ],
    },

    history: {
      steps: [
        { id: 'decks', target: '[data-tour="history-decks"]', icon: 'layers' },
        { id: 'runs', target: '[data-tour="history-runs"]', icon: 'printer' },
      ],
    },

    collection: {
      steps: [
        { id: 'stats', target: '[data-tour="coll-stats"]', icon: 'bar-chart-3' },
        { id: 'sets', target: '[data-tour="coll-sets"]', icon: 'library', placement: 'right' },
        { id: 'cards', target: '[data-tour="coll-main"]', icon: 'check-circle-2', placement: 'left' },
      ],
    },

    planner: {
      steps: [
        { id: 'decks', target: '[data-tour="planner-decks"]', icon: 'layers', placement: 'right' },
        { id: 'results', target: '[data-tour="planner-results"]', icon: 'package', placement: 'left' },
      ],
    },

    library: {
      steps: [
        { id: 'filters', target: '[data-tour="library-filters"]', icon: 'filter', placement: 'right' },
        { id: 'grid', target: '[data-tour="library-grid"]', icon: 'images', placement: 'left' },
      ],
    },

    calibrate: {
      steps: [
        { id: 'why', icon: 'crosshair', hero: true },
        { id: 'steps', target: '[data-tour="calib-steps"]', icon: 'list-ordered' },
      ],
    },

    settings: {
      steps: [
        { id: 'nav', target: '[data-tour="settings-nav"]', icon: 'list', placement: 'right' },
        { id: 'search', target: '[data-tour="settings-search"]', icon: 'search' },
        { id: 'drives', target: '[data-tour="settings-nav-art-sources"]', icon: 'image', placement: 'right', dynamic: true },
        { id: 'backup', target: '[data-tour="settings-nav-backup"]', icon: 'archive', placement: 'right', dynamic: true },
        { id: 'tour', target: '[data-tour="settings-tour"]', icon: 'graduation-cap' },
      ],
    },
  }

  // Qué guía corresponde a cada URL.
  const PAGES = [
    [/^\/$/, 'home'],
    [/^\/decks\/?$/, 'decks'],
    [/^\/decks\/\d+\/pdf\/?$/, 'pdf'],
    [/^\/decks\/\d+\/proof\/?$/, 'proof'],
    [/^\/decks\/\d+\/?$/, 'deck'],
    [/^\/history\/?$/, 'history'],
    [/^\/collection\/?$/, 'collection'],
    [/^\/print-planner\/?$/, 'planner'],
    [/^\/art-library\/?$/, 'library'],
    [/^\/calibrate\/?$/, 'calibrate'],
    [/^\/settings\/?$/, 'settings'],
  ]

  window.TOURS = TOURS
  window.tourForPath = (path) => {
    for (const [re, id] of PAGES) if (re.test(path)) return id
    return null
  }
})()
