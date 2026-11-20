import { describe, it, expect } from 'vitest'
import en from '../i18n/en.json'
import tr from '../i18n/tr.json'

// Error codes api/main.py + api/garmin_link.py send as `detail` for the Garmin
// connect steps; GarminConnect.tsx shows settings.garmin.errors.<code>.
const API_ERROR_CODES = [
  'invalid_credentials', 'invalid_code', 'code_expired', 'rate_limited',
  'too_many_attempts', 'garmin_unreachable', 'missing_fields', 'not_connected', 'generic',
]

describe('Garmin connect errors', () => {
  it('every error code the API sends is translated', () => {
    for (const code of API_ERROR_CODES) {
      expect(en.settings.garmin.errors[code as keyof typeof en.settings.garmin.errors], `en ${code}`).toBeTruthy()
      expect(tr.settings.garmin.errors[code as keyof typeof tr.settings.garmin.errors], `tr ${code}`).toBeTruthy()
    }
  })
})
