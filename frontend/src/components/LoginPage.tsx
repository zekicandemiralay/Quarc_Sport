import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import useAuthStore from '../store/authStore'
import StoreLoginButton from './StoreLoginButton'

export default function LoginPage() {
  const { t } = useTranslation()
  const login = useAuthStore(s => s.login)
  const register = useAuthStore(s => s.register)
  const [mode, setMode]         = useState<'login' | 'signup'>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError]       = useState<string | null>(null)
  const [busy, setBusy]         = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!username.trim() || !password) return
    setBusy(true)
    setError(null)
    try {
      if (mode === 'login') await login(username.trim(), password)
      else await register(username.trim(), password)
    } catch (err) {
      // fetch() rejects with a TypeError when there is no connection at all
      setError(err instanceof TypeError ? t('auth.needsConnection') : err instanceof Error ? err.message : t('auth.loginFailed'))
    } finally {
      setBusy(false)
    }
  }

  const field =
    'w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500'

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center gap-3 mb-8">
          <img src="/logo.png" alt="Quarc Sport" className="w-20 h-20 rounded-2xl shadow-md" />
          <div className="text-center">
            <h1 className="text-xl font-semibold text-white">Quarc Sport</h1>
            <p className="mt-1 text-sm text-slate-400">{t('auth.signInToAccount')}</p>
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6">
          <StoreLoginButton onSignedIn={() => useAuthStore.getState().checkSession()} />

          <form onSubmit={handleSubmit} className="space-y-3">
            {error && (
              <div className="bg-red-900/30 border border-red-700/50 text-red-300 rounded-lg px-3 py-2 text-sm">
                {error}
              </div>
            )}
            <input
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              autoFocus
              value={username}
              onChange={e => setUsername(e.target.value)}
              className={field}
              placeholder={t('auth.username')}
            />
            <input
              type="password"
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              value={password}
              onChange={e => setPassword(e.target.value)}
              className={field}
              placeholder={t('auth.password')}
            />
            <button
              type="submit"
              disabled={busy || !username.trim() || !password}
              className="w-full bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white font-medium rounded-lg py-2 text-sm transition-colors mt-1"
            >
              {mode === 'login' ? t('auth.login') : t('auth.signup')}
            </button>
          </form>

          <button
            type="button"
            className="mt-4 w-full text-center text-sm text-slate-400 hover:underline"
            onClick={() => { setMode(mode === 'login' ? 'signup' : 'login'); setError(null) }}
          >
            {mode === 'login' ? t('auth.needAccount') : t('auth.haveAccount')}
          </button>
        </div>
      </div>
    </div>
  )
}
