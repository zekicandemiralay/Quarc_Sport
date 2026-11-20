import { create } from 'zustand'
import { apiUrl } from '../lib/apiUrl'
import { syncFromStore, watchStoreSession } from '../lib/storeSso'
import type { User } from '../types'

// Login goes through the shared quarc-auth service (same account as every other
// Quarc app). The session lives in an httpOnly cookie; the last known user is
// cached in localStorage so the app still opens when the server can't be reached.

const CACHE_KEY = 'quarc_user'

const cachedUser = (): User | null => {
  try {
    return JSON.parse(localStorage.getItem(CACHE_KEY) || 'null')
  } catch {
    return null
  }
}

interface AuthState {
  user: User | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  /** Server says the session is gone (expired / revoked): back to the login screen. */
  sessionLost: () => void
  checkSession: () => Promise<void>
}

async function authError(res: Response, fallback: string): Promise<Error> {
  const err = await res.json().catch(() => ({}))
  return new Error((err as { error?: string }).error || fallback)
}

async function fetchWithTimeout(path: string, ms = 5000): Promise<Response> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), ms)
  try {
    return await fetch(apiUrl(path), { credentials: 'include', signal: controller.signal })
  } finally {
    clearTimeout(timeout)
  }
}

async function postCredentials(path: string, username: string, password: string, fallback: string) {
  const res = await fetch(apiUrl(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) throw await authError(res, fallback)
}

export const useAuthStore = create<AuthState>()((set, get) => ({
  user: cachedUser(),
  loading: true,

  async login(username, password) {
    await postCredentials('/api/auth/login', username, password, 'Login failed')
    await get().checkSession()
  },

  async register(username, password) {
    await postCredentials('/api/auth/register', username, password, 'Sign up failed')
    await get().checkSession()
  },

  async logout() {
    await fetch(apiUrl('/api/auth/logout'), { method: 'POST', credentials: 'include' }).catch(() => {})
    localStorage.removeItem(CACHE_KEY)
    set({ user: null, loading: false })
  },

  sessionLost() {
    localStorage.removeItem(CACHE_KEY)
    set({ user: null, loading: false })
  },

  async checkSession() {
    // On Android, Quarc Store (if installed) is the primary login source:
    // this applies its sign-in/sign-out to this app's cookie, if it changed.
    await syncFromStore()
    try {
      // 1. quarc-auth: is this login still valid? (This is where a session that was
      //    revoked elsewhere is noticed — the API itself only checks the signature.)
      const session = await fetchWithTimeout('/api/auth/me')
      if (session.status === 401) return get().sessionLost()
      if (!session.ok) return set({ user: get().user, loading: false }) // server trouble: stay as we are

      // 2. This app: who am I here, and is Garmin connected?
      const res = await fetchWithTimeout('/api/me')
      if (res.status === 401) return get().sessionLost()
      if (!res.ok) return set({ user: get().user, loading: false })
      const user = (await res.json()) as User
      localStorage.setItem(CACHE_KEY, JSON.stringify(user))
      set({ user, loading: false })
    } catch {
      // Network error / offline: fall back to the last cached user rather than logging out.
      set({ user: get().user, loading: false })
    }
  },
}))

// Signing in/out in Quarc Store applies as soon as the user switches back here.
watchStoreSession(() => useAuthStore.getState().checkSession())

export default useAuthStore
