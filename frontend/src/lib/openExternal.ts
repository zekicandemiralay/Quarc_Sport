type Native = Window & {
  Capacitor?: { isNativePlatform?: () => boolean; Plugins: Record<string, any> }
  __TAURI__?: any
}

/**
 * Opens an address outside the app — Strava's sign-in page must not load inside the app's own
 * window. Android: the system browser (through the Quarc plugin); desktop app: the default
 * browser; web: a new tab.
 */
export async function openExternal(url: string): Promise<void> {
  const w = window as Native
  if (w.Capacitor?.isNativePlatform?.()) {
    try {
      await w.Capacitor.Plugins.QuarcSso.openUrl({ url })
      return
    } catch {
      /* fall through */
    }
  }
  if (w.__TAURI__?.shell?.open) {
    await w.__TAURI__.shell.open(url)
    return
  }
  window.open(url, '_blank', 'noopener,noreferrer')
}
