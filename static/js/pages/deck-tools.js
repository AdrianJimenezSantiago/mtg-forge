(function () {
  'use strict'

  function choose(n, k) {
    if (k < 0 || k > n) return 0
    k = Math.min(k, n - k)
    let r = 1
    for (let i = 1; i <= k; i++) r = (r * (n - k + i)) / i
    return r
  }

  function hypergeomExactly(population, successes, draws, k) {
    if (draws > population) draws = population
    const total = choose(population, draws)
    if (!total) return 0
    return (choose(successes, k) * choose(population - successes, draws - k)) / total
  }

  function hypergeomAtLeast(population, successes, draws, k) {
    let p = 0
    for (let i = Math.max(0, k); i <= Math.min(draws, successes); i++) {
      p += hypergeomExactly(population, successes, draws, i)
    }
    return Math.min(1, Math.max(0, p))
  }

  function isLand(card) {
    const front = String(card.type_line || '').split('//')[0]
    return /\bLand\b/i.test(front)
  }

  function buildLibrary(cards) {
    const out = []
    for (const card of cards || []) {
      if (!card.include || card.role !== 'mainboard') continue
      const quantity = Number.isFinite(card.quantity) ? card.quantity : 1
      for (let i = 0; i < quantity; i++) {
        out.push({
          uid: `${card.id}:${i}`,
          id: card.id,
          name: card.name,
          thumb: card.thumbnail_url || '',
          land: isLand(card),
        })
      }
    }
    return out
  }

  function shuffle(list, rng = Math.random) {
    const a = [...list]
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1))
      ;[a[i], a[j]] = [a[j], a[i]]
    }
    return a
  }

  function landOdds(library, turns = 4) {
    const N = library.length
    const K = library.filter((c) => c.land).length
    const opening = []
    for (let k = 0; k <= Math.min(7, N); k++) opening.push({ lands: k, p: hypergeomExactly(N, K, 7, k) })
    const byTurn = []
    for (let t = 1; t <= turns; t++) {
      byTurn.push({
        turn: t,
        play: hypergeomAtLeast(N, K, 7 + t - 1, t),
        draw: hypergeomAtLeast(N, K, 7 + t, t),
      })
    }
    const keepable = opening.filter((o) => o.lands >= 2 && o.lands <= 5).reduce((s, o) => s + o.p, 0)
    return { size: N, lands: K, opening, byTurn, keepable }
  }

  function percent(p) {
    return `${(p * 100).toFixed(p >= 0.995 || p < 0.005 ? 0 : 1)}%`
  }

  const SHORTCUTS = [
    { keys: ['/'], action: 'search', label: 'shortcut_search' },
    { keys: ['a'], action: 'add', label: 'shortcut_add' },
    { keys: ['s'], action: 'select', label: 'shortcut_select' },
    { keys: ['h'], action: 'hand', label: 'shortcut_hand' },
    { keys: ['t'], action: 'stats', label: 'shortcut_stats' },
    { keys: ['p'], action: 'pdf', label: 'shortcut_pdf' },
    { keys: ['Ctrl', 'Z'], action: 'undo', label: 'shortcut_undo' },
    { keys: ['?'], action: 'help', label: 'shortcut_help' },
    { keys: ['Esc'], action: 'escape', label: 'shortcut_escape' },
  ]

  function shortcutFor(event) {
    const target = event.target || {}
    const tag = String(target.tagName || '')
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag) || target.isContentEditable
    if (event.key === 'Escape') return 'escape'
    if (typing || event.altKey) return null
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === 'z') return 'undo'
    if (event.ctrlKey || event.metaKey) return null
    if (event.key === '?') return 'help'
    const hit = SHORTCUTS.find((s) => s.keys.length === 1 && s.keys[0] === event.key)
    return hit ? hit.action : null
  }

  window.deckTools = {
    choose,
    hypergeomExactly,
    hypergeomAtLeast,
    isLand,
    buildLibrary,
    shuffle,
    landOdds,
    percent,
    SHORTCUTS,
    shortcutFor,
  }
})()
