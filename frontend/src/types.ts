/** Who is signed in (quarc-auth account) plus whether Quarc Sport has their Garmin login. */
export interface User {
  id: string
  username: string
  has_garmin: boolean
  /** null when not connected (servers before 1.0.1 don't send it at all) */
  garmin_status?: GarminConnectionState | null
}

/** connected | needs_code (Garmin wants a new two-factor code) | auth_failed (login no longer accepted) | error (temporary) */
export type GarminConnectionState = 'connected' | 'needs_code' | 'auth_failed' | 'error'

/** GET /api/garmin/status — the Garmin connection and how far the import got. */
export interface GarminStatus {
  status: GarminConnectionState | 'not_connected'
  email: string | null              // masked, e.g. "ze•••@gmail.com"
  pending_code: boolean             // a sign-in is waiting for the two-factor code
  queued: boolean                   // a sync was asked for and starts within seconds
  syncing: boolean
  first_import: boolean             // nothing imported yet
  last_sync_at: string | null
  retry_after: string | null
  synced_until: string | null
  history_from: string | null       // older history is imported back to this day…
  history_target: string | null     // …and goes back to this one
  history_done: boolean
  history_percent: number | null
}

export interface GarminConnectResult {
  status: 'connected' | 'needs_code'
  method?: 'email' | 'phone' | null
}

export interface DataRange {
  earliest: string | null
  latest: string | null
}

export interface DailyRow {
  date: string
  steps: number | null
  step_goal: number | null
  distance_meters: number | null
  active_calories: number | null
  total_calories: number | null
  floors_ascended: number | null
  floors_descended: number | null
  active_time_seconds: number | null
  sedentary_seconds: number | null
  stress_avg: number | null
  stress_rest: number | null
  body_battery_high: number | null
  body_battery_low: number | null
  spo2_avg: number | null
  spo2_min: number | null
  hydration_ml: number | null
  resting_hr: number | null
  min_hr_day: number | null
  max_hr_day: number | null
}

export interface SleepRow {
  date: string
  start_time: string | null
  end_time: string | null
  duration_seconds: number | null
  light_seconds: number | null
  deep_seconds: number | null
  rem_seconds: number | null
  awake_seconds: number | null
  sleep_score: number | null
  avg_spo2: number | null
  avg_respiration: number | null
}

export interface HrvRow {
  date: string
  hrv_weekly_avg: number | null
  hrv_last_night: number | null
  hrv_last_night_5min: number | null
  hrv_status: string | null
}

export interface Activity {
  activity_id: number
  start_time: string
  activity_type: string
  name: string
  duration_seconds: number | null
  distance_meters: number | null
  avg_hr: number | null
  max_hr: number | null
  calories: number | null
  avg_pace_sec_per_km: number | null
  aerobic_te: number | null
  anaerobic_te: number | null
  start_lat: number | null
  start_lng: number | null
  elevation_gain_m: number | null
  avg_speed_mps: number | null
  avg_cadence: number | null
  avg_power: number | null
}

export interface MapActivity {
  activity_id: number
  start_time: string
  activity_type: string
  name: string
  duration_seconds: number | null
  distance_meters: number | null
  avg_hr: number | null
  calories: number | null
  avg_pace_sec_per_km: number | null
  elevation_gain_m: number | null
  start_lat: number | null
  start_lng: number | null
  end_lat: number | null
  end_lng: number | null
  polyline: number[][] | null  // each point: [lat, lng] or [lat, lng, speed_mps]
}

export interface WeatherHourly {
  time: string[]
  temperature_2m: number[]
  precipitation: number[]
  wind_speed_10m: number[]
  wind_direction_10m: number[]
  weather_code: number[]
  relative_humidity_2m: number[]
}

export interface CountryCrossing {
  lat: number
  lng: number
  from: string
  to: string
}

export interface TouringActivity {
  activity_id: number
  start_time: string
  activity_type: string
  name: string
  duration_seconds: number | null
  distance_meters: number | null
  elevation_gain_m: number | null
  avg_speed_mps: number | null
  avg_hr: number | null
  avg_pace_sec_per_km: number | null
  calories: number | null
  start_lat: number
  start_lng: number
  end_lat: number | null
  end_lng: number | null
  polyline: number[][]
  weather_data: WeatherHourly | null
  country_crossings: CountryCrossing[] | null
  country: string | null
}

export interface TourSummary {
  id: number
  name: string
  description: string | null
  created_at: string
  activity_count: number
  total_km: number
  start_date: string | null
  end_date: string | null
}

export interface TourDetail {
  tour: { id: number; name: string; description: string | null; created_at: string }
  activities: TouringActivity[]
  sleep: TouringData['sleep']
}

export interface TouringData {
  activities: TouringActivity[]
  sleep: {
    date: string
    start_time: string | null
    end_time: string | null
    duration_seconds: number | null
    sleep_score: number | null
    avg_spo2: number | null
  }[]
}

export interface CountryTypeStat {
  type: string
  count: number
  total_km: number
  total_hours: number
  total_elevation_m: number
  avg_hr: number | null
}

export interface CountryStat {
  country: string   // ISO 3166-1 alpha-2 code, e.g. "AT"
  total_activities: number
  total_km: number
  total_hours: number
  total_elevation_m: number
  types: CountryTypeStat[]
}

export interface WeatherGridPoint {
  lat: number
  lng: number
  date: string
  hour: number
  temperature_2m: number | null
  precipitation: number | null
  wind_speed_10m: number | null
  wind_direction_10m: number | null
}

export interface Summary {
  period_7d: {
    daily: {
      avg_steps: number | null
      avg_resting_hr: number | null
      avg_bb_high: number | null
      avg_bb_low: number | null
      avg_stress: number | null
      avg_spo2: number | null
    }
    sleep: {
      avg_sleep_hours: number | null
      avg_sleep_score: number | null
      avg_deep_min: number | null
      avg_rem_min: number | null
    }
    hrv: {
      avg_hrv: number | null
      avg_hrv_weekly: number | null
    }
    activities: number
  }
  activities_30d: number
}

// ─── Sources (Garmin, Strava, Health Connect — never mixed) ───────────────────

export type SourceId = 'garmin' | 'strava' | 'health_connect'

export interface SourceInfo {
  source: SourceId
  connected: boolean
  status: string | null
  last_sync_at: string | null
  has_data: boolean
  /** table → columns that hold at least one value for this source */
  available: Record<string, string[]>
}

export interface SourcesResponse {
  sources: SourceInfo[]
  default: SourceId
}

export interface StravaStatus {
  configured: boolean
  connected: boolean
  /** not_connected | connected | auth_failed | error */
  status: string
  error: string | null
  athlete: string | null
  last_sync_at: string | null
  syncing: boolean
  queued: boolean
  retry_after: string | null
  imported: number
  history_done: boolean
}
