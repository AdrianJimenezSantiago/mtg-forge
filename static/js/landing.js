/**
 * Landing (ruta "/", templates/landing.html).
 *
 * La importación rápida del hero es exactamente la de la biblioteca: se
 * reutiliza `importPanel()` de home.js, que la plantilla carga antes que este
 * fichero. Así el flujo (detección del sitio, modal de cartas no resueltas,
 * salto al editor) no se duplica ni puede divergir.
 *
 * Lo único que añade la landing es el atajo de teclado "/" para enfocar el
 * campo de URL, el mismo que usan muchos buscadores.
 *
 * Script clásico con `defer`, no módulo ES: ver la nota de static/js/api.js.
 */

/** ¿El foco está en un sitio donde "/" es texto y no un atajo? */
function isTypingTarget(el) {
  if (!el) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable
}

function landingImport() {
  const panel = window.importPanel()
  panel.init = function () {
    const onKey = (e) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return
      if (isTypingTarget(document.activeElement)) return
      const field = this.$refs && this.$refs.url
      if (!field) return
      e.preventDefault()
      field.focus()
      field.select()
    }
    window.addEventListener('keydown', onKey)
  }
  return panel
}
/**
 * Total ocupado en disco, en el panel "Estado del taller".
 *
 * Las otras tres cifras del panel las pinta Jinja con datos que ya están en la
 * base de datos. Esta no puede: hay que recorrer la carpeta de datos, y meter
 * eso en el render de "/" haría que la portada tardase lo que tarde el disco
 * del usuario. Se pide aparte y se sustituye el "Calculando…" al llegar.
 *
 * El endpoint cachea el resultado un minuto, así que ir y volver a la portada
 * no relanza el escaneo.
 */
function workshopStorage() {
  return {
    label: window._t('landing_status_storage_loading'),
    async load() {
      try {
        const r = await fetch('/api/storage/')
        if (!r.ok) throw new Error('storage')
        const data = await r.json()
        this.label = window._t('landing_status_storage_value')
          .replace('{size}', formatBytes(data.totals.bytes))
      } catch (e) {
        // Sin cifra no se deja un "Calculando…" eterno: el enlace a Ajustes
        // sigue ahí y allí se puede recalcular a mano.
        this.label = '—'
      }
    },
  }
}

/** Bytes → "1,4 GB". Duplicado mínimo del helper de settings.js: la landing no
 *  carga ese módulo y traer 1.100 líneas por una función no compensa. */
function formatBytes(value) {
  const bytes = Number(value) || 0
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = bytes / 1024
  let i = 0
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++ }
  return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[i]}`
}


// --- Puente con Alpine -------------------------------------
window.landingImport = landingImport
window.workshopStorage = workshopStorage
