import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const w = { Math }
w.window = w
vm.createContext(w)
vm.runInContext(readFileSync(process.argv[2], 'utf8'), w)
const t = w.deckTools
const problems = []
const close = (a, b, eps = 1e-9) => Math.abs(a - b) < eps

if (t.choose(5, 2) !== 10 || t.choose(3, 5) !== 0) problems.push('choose')
const total = [0, 1, 2, 3, 4, 5, 6, 7].reduce((s, k) => s + t.hypergeomExactly(60, 24, 7, k), 0)
if (!close(total, 1)) problems.push({ distribution: total })
if (!close(t.hypergeomExactly(60, 24, 7, 0), 8347680 / 386206920)) problems.push('zeroLands')
if (!close(t.hypergeomAtLeast(60, 24, 7, 0), 1)) problems.push('atLeastZero')
if (t.hypergeomAtLeast(10, 0, 7, 1) !== 0) problems.push('noSuccesses')

const cards = [
  { id: 1, name: 'Forest', type_line: 'Basic Land — Forest', quantity: 24, include: true, role: 'mainboard' },
  { id: 2, name: 'Elf', type_line: 'Creature — Elf', quantity: 36, include: true, role: 'mainboard' },
  { id: 3, name: 'Cmdr', type_line: 'Legendary Creature', quantity: 1, include: true, role: 'commander' },
  { id: 4, name: 'Off', type_line: 'Land', quantity: 3, include: false, role: 'mainboard' },
  { id: 5, name: 'Dryad Arbor', type_line: 'Land Creature — Forest Dryad', quantity: 1, include: true, role: 'sideboard' },
  { id: 6, name: 'Valki // Tibalt', type_line: 'Legendary Creature — God // Legendary Planeswalker', quantity: 0, include: true, role: 'mainboard' },
]
const lib = t.buildLibrary(cards)
if (lib.length !== 60 || lib.filter((c) => c.land).length !== 24) problems.push({ library: lib.length })
if (new Set(lib.map((c) => c.uid)).size !== 60) problems.push('uids')
if (!t.isLand({ type_line: 'Land // Creature' }) || t.isLand({ type_line: 'Creature // Land' })) problems.push('mdfc')

let seed = 1
const rng = () => ((seed = (seed * 16807) % 2147483647) / 2147483647)
const shuffled = t.shuffle(lib, rng)
if (shuffled.length !== lib.length || shuffled === lib) problems.push('shuffle')
if ([...shuffled].sort((a, b) => a.uid.localeCompare(b.uid)).map((c) => c.uid).join() !==
    [...lib].sort((a, b) => a.uid.localeCompare(b.uid)).map((c) => c.uid).join()) problems.push('shufflePerm')

const odds = t.landOdds(lib)
if (odds.size !== 60 || odds.lands !== 24 || odds.opening.length !== 8 || odds.byTurn.length !== 4) problems.push({ odds })
if (!(odds.byTurn[0].draw >= odds.byTurn[0].play)) problems.push('drawBeatsPlay')
if (!(odds.keepable > 0.8 && odds.keepable < 0.95)) problems.push({ keepable: odds.keepable })
if (t.percent(0.5) !== '50.0%' || t.percent(1) !== '100%') problems.push('percent')

const key = (k, extra = {}) => ({ key: k, target: { tagName: 'BODY' }, ...extra })
const cases = [
  [key('/'), 'search'], [key('a'), 'add'], [key('?'), 'help'], [key('z', { ctrlKey: true }), 'undo'],
  [key('z', { metaKey: true }), 'undo'], [key('Escape'), 'escape'], [key('x'), null],
  [key('a', { target: { tagName: 'INPUT' } }), null], [key('Escape', { target: { tagName: 'INPUT' } }), 'escape'],
  [key('a', { ctrlKey: true }), null], [key('a', { altKey: true }), null],
]
for (const [ev, expected] of cases) {
  if (t.shortcutFor(ev) !== expected) problems.push({ shortcut: ev.key, got: t.shortcutFor(ev), expected })
}
console.log(JSON.stringify({ problems }))
