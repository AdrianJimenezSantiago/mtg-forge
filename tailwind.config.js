/** Configuración de Tailwind para el build local.
 *
 * Antes este objeto vivía inline en `templates/base.html` y lo consumía el CDN
 * de Tailwind, que compilaba las clases EN EL NAVEGADOR en cada carga de
 * página. Eso tenía dos problemas: bloqueaba el render ~200 ms con internet, y
 * sin internet la app se veía completamente sin estilos — inaceptable en una
 * herramienta que se distribuye como .exe para uso local.
 *
 * Ahora se compila una vez a `static/vendor/tailwind.css` (~25 KB) con:
 *     npm run build:css
 *
 * El `content` debe cubrir TODO sitio donde aparezcan clases de Tailwind, o el
 * purgado las eliminará del CSS final y la vista saldrá rota.
 */
module.exports = {
  content: [
    './templates/**/*.html',
    './static/js/**/*.js',
    './static/app.js',
  ],
  // Clases que se construyen dinámicamente en JS y que el escáner estático de
  // Tailwind no puede ver. Sin esta lista, el purgado se las lleva.
  safelist: [
    'bg-success-bg', 'bg-warning-bg', 'bg-danger-bg', 'bg-info-bg',
    'border-success-border', 'border-warning-border',
    'border-danger-border', 'border-info-border',
    'text-success', 'text-warning', 'text-danger', 'text-info',
    'text-mtg-white', 'text-mtg-blue', 'text-mtg-black',
    'text-mtg-red', 'text-mtg-green', 'text-mtg-gold',
    { pattern: /^(grid-cols|col-span)-(1|2|3|4|5|6|7|8|9|10|11|12)$/ },
    { pattern: /^animate-/ },
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: {
          base:     '#0a0d13',   /* casi negro con tinte azulado (arcane) */
          elevated: '#141821',
          subtle:   '#1c2230',
          inset:    '#0f131b',
        },
        border: {
          subtle: '#232a3a',
          strong: '#3a4358',
        },
        /* Paleta MTG-inspired ---------------------------------------- */
        accent: {
          /* Oro de las cartas mythic-rare: cálido y llamativo sin ser chillón */
          DEFAULT: '#d4af37',
          soft:    '#a58524',
          glow:    '#e9c86a',
        },
        mtg: {
          white: '#f5f0d8',
          blue:  '#5b9bd5',
          black: '#3a3a4a',
          red:   '#d9534f',
          green: '#5cb85c',
          gold:  '#d4af37',
        },
        /* Colores semánticos alineados con las tierras básicas ------- */
        success: { DEFAULT: '#4ade80', bg: 'rgba(74,222,128,0.10)', border: 'rgba(74,222,128,0.30)' },
        warning: { DEFAULT: '#fb923c', bg: 'rgba(251,146,60,0.10)', border: 'rgba(251,146,60,0.30)' },
        danger:  { DEFAULT: '#f87171', bg: 'rgba(248,113,113,0.10)', border: 'rgba(248,113,113,0.30)' },
        info:    { DEFAULT: '#60a5fa', bg: 'rgba(96,165,250,0.10)', border: 'rgba(96,165,250,0.30)' },
        fg: {
          DEFAULT: '#e8e6dd',    /* ligero cálido, no gris frío */
          muted:   '#a8a8b8',
          faint:   '#6a6a80',
        },
      },
      fontFamily: {
        sans:    ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono:    ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
        display: ['Cinzel', 'Georgia', 'serif'], /* para títulos "arcane" opcional */
      },
      boxShadow: {
        'card':   '0 1px 2px 0 rgba(0,0,0,.5), 0 0 0 1px rgba(255,255,255,0.02)',
        'glow':   '0 0 24px 0 rgba(212,175,55,0.15)',
        'modal':  '0 25px 60px -12px rgba(0,0,0,0.7), 0 0 0 1px rgba(212,175,55,0.10)',
      },
      animation: {
        'shimmer':       'shimmer 2s linear infinite',
        'toast-in':      'toast-in 220ms ease-out',
        'fade-in':       'fade-in 200ms ease-out both',
        'slide-in-up':   'slide-in-up 260ms cubic-bezier(0.22,1,0.36,1) both',
        'slide-in-down': 'slide-in-down 260ms cubic-bezier(0.22,1,0.36,1) both',
        'scale-in':      'scale-in 180ms cubic-bezier(0.22,1,0.36,1) both',
        'pulse-soft':    'pulse-soft 2s ease-in-out infinite',
        'skeleton':      'skeleton 1.4s ease-in-out infinite',
        'spin-slow':     'spin 2s linear infinite',
        'bounce-soft':   'bounce-soft 1.2s ease-in-out infinite',
        'progress-bar':  'progress-bar 1.4s ease-in-out infinite',
      },
      keyframes: {
        shimmer: {
          '0%':   { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        'toast-in': {
          '0%':   { opacity: '0', transform: 'translateX(20px) scale(0.95)' },
          '100%': { opacity: '1', transform: 'translateX(0) scale(1)' },
        },
        'fade-in': {
          '0%':   { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'slide-in-up': {
          '0%':   { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'slide-in-down': {
          '0%':   { opacity: '0', transform: 'translateY(-12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'scale-in': {
          '0%':   { opacity: '0', transform: 'scale(0.94)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
        'pulse-soft': {
          '0%,100%': { opacity: '1' },
          '50%':     { opacity: '0.55' },
        },
        'skeleton': {
          '0%,100%': { opacity: '0.4' },
          '50%':     { opacity: '0.8' },
        },
        'bounce-soft': {
          '0%,100%': { transform: 'translateY(0)' },
          '50%':     { transform: 'translateY(-3px)' },
        },
        'progress-bar': {
          '0%':   { transform: 'translateX(-100%)' },
          '100%': { transform: 'translateX(400%)' },
        },
      },
    }
  }
}
