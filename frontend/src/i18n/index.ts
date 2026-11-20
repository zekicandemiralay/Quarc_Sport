import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import en from './en.json'
import tr from './tr.json'

i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, tr: { translation: tr } },
  lng: localStorage.getItem('language') || 'en',
  fallbackLng: 'en',
  interpolation: { escapeValue: false },
})

export default i18n

/** The active UI language as a BCP-47 tag for Intl / toLocale*String ("en" or "tr"). */
export function locale(): string {
  return (i18n.language || 'en').slice(0, 2)
}

export function setLanguage(lng: string): void {
  localStorage.setItem('language', lng)
  i18n.changeLanguage(lng)
}

/**
 * Locale for toLocaleDateString / toLocaleTimeString. English keeps the browser's own
 * formatting (as before); Turkish gets Turkish month/day names.
 */
export function dateLocale(): string | undefined {
  return locale() === 'tr' ? 'tr-TR' : undefined
}

/** "TRAIL_RUNNING" → "Trail Running" (or its translation, see activityTypes in en/tr.json). */
export function typeLabel(type: string): string {
  const fallback = type.split('_').map(w => (w ? w[0] + w.slice(1).toLowerCase() : w)).join(' ')
  return i18n.t(`activityTypes.${type}`, { defaultValue: fallback })
}
