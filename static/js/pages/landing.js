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
        this.label = '—'
      }
    },
  }
}

function formatBytes(value) {
  const bytes = Number(value) || 0
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = bytes / 1024
  let i = 0
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++ }
  return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[i]}`
}

function onboardingPanel() {
  return {
    dismissed: false,
    creating: false,
    async dismiss() {
      this.dismissed = true
      try {
        await window.apiRequest('POST', '/api/tour/seen', { body: { tour: 'onboarding' } })
      } catch (_) {}
    },
    async createSample() {
      this.creating = true
      try {
        const data = await window.apiRequest('POST', '/api/decks/import/sample', {
          body: { name: window._t('onboarding_sample_name') },
        })
        window.location.href = `/decks/${data.deck.id}`
      } catch (e) {
        window.toast(window._t('common_error'), e.detail || e.message, 'error')
        this.creating = false
      }
    },
  }
}

window.onboardingPanel = onboardingPanel
window.landingImport = landingImport
window.workshopStorage = workshopStorage
