// In-app update checking — mirrors Quarc Music's mechanism (same GitHub-releases-based
// check, same per-platform install behavior), pointed at this repo. Native platforms
// only (Capacitor/Tauri); plain web has no "installed version" concept, so it only
// ever links out.

import { updateViaStore } from './storeSso'

const REPO = 'zekicandemiralay/Quarc_Sport'
export const NO_RELEASE = 'no-release'

export type Platform = 'android' | 'desktop' | 'web'

interface ReleaseAsset { name: string; browser_download_url: string }
export interface Release { tag_name: string; html_url: string; assets?: ReleaseAsset[] }

type NativeWindow = Window & {
  Capacitor?: { isNativePlatform?: () => boolean; Plugins: Record<string, any> }
  __TAURI__?: any
}
const w = () => window as NativeWindow

export function semverGt(a: string | null, b: string | null): boolean {
  const pa = (a || '0').split('.').map(Number)
  const pb = (b || '0').split('.').map(Number)
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) > (pb[i] || 0)) return true
    if ((pa[i] || 0) < (pb[i] || 0)) return false
  }
  return false
}

export function getPlatform(): Platform {
  if (w().Capacitor?.isNativePlatform?.()) return 'android'
  if (w().__TAURI__) return 'desktop'
  return 'web'
}

export async function getCurrentVersion(platform: Platform): Promise<string | null> {
  try {
    if (platform === 'android') return (await w().Capacitor!.Plugins.App.getInfo()).version
    if (platform === 'desktop') return await w().__TAURI__.app.getVersion()
  } catch {
    /* native API unavailable — treat as unknown */
  }
  return null
}

export async function fetchLatestRelease(): Promise<Release> {
  const res = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`)
  if (res.status === 404) throw new Error(NO_RELEASE) // repo exists but nothing is published yet
  if (!res.ok) throw new Error(`GitHub API ${res.status}`)
  return res.json()
}

export function getDownloadUrl(release: Release | null, platform: Platform): string | undefined {
  if (!release) return undefined
  if (platform === 'android') return release.assets?.find((a) => a.name.endsWith('.apk'))?.browser_download_url
  if (platform === 'desktop') {
    return release.assets?.find((a) => a.name.includes('x64-setup.exe'))?.browser_download_url ?? release.html_url
  }
  return release.html_url
}

export function installUpdate(platform: Platform, url: string, version: string): void {
  if (platform === 'android') {
    // Quarc Store installed? Update there (it becomes this app's installer, so
    // later updates can be silent); otherwise download the APK ourselves.
    updateViaStore().then((viaStore: boolean) => {
      if (!viaStore) w().Capacitor?.Plugins?.Updater?.downloadUpdate({ url, version })
    })
  } else if (platform === 'desktop') {
    // The NSIS installer updates in place.
    w().__TAURI__.shell.open(url)
  } else {
    window.open(url, '_blank', 'noopener,noreferrer')
  }
}
