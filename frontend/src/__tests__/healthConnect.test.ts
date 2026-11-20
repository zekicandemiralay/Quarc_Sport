import { describe, it, expect, vi, beforeEach } from 'vitest'

const ingest = vi.fn(async (body: any) => ({
  daily: body.daily.length, sleep: body.sleep.length, activities: body.activities.length,
}))
vi.mock('../api', () => ({ ingestHealthConnect: (b: any) => ingest(b) }))

import {
  daysToRead, getHcState, syncHealthConnect, autoSyncHealthConnect, setHcEnabled, hcEnabled, lastHcUpload, isAndroidApp,
} from '../lib/healthConnect'

// A phone: window.Capacitor with a fake HealthConnect plugin, and a localStorage.
function phone(plugin: Record<string, unknown>) {
  const mem = new Map<string, string>()
  ;(globalThis as any).localStorage = {
    getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v), removeItem: (k: string) => void mem.delete(k),
  }
  ;(globalThis as any).window = { Capacitor: { isNativePlatform: () => true, Plugins: { HealthConnect: plugin } } }
}

const rows = (n: number, prefix = 'r') => Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }))

beforeEach(() => {
  ingest.mockClear()
  delete (globalThis as any).window
  delete (globalThis as any).localStorage
})

describe('outside the Android app', () => {
  it('does nothing', async () => {
    expect(isAndroidApp()).toBe(false)
    expect(await getHcState()).toEqual({ state: 'unsupported' })
    await expect(syncHealthConnect()).rejects.toThrow('unsupported')
    expect(await autoSyncHealthConnect()).toBe(false)
  })
})

describe('what state Health Connect is in', () => {
  it('maps availability and permissions', async () => {
    phone({ availability: async () => ({ status: 'unavailable' }) })
    expect(await getHcState()).toEqual({ state: 'unavailable' })
    phone({ availability: async () => ({ status: 'update_required' }) })
    expect(await getHcState()).toEqual({ state: 'update_required' })
    phone({ availability: async () => ({ status: 'available' }), permissions: async () => ({ granted: false, complete: false, history: false }) })
    expect((await getHcState()).state).toBe('needs_permission')
    phone({ availability: async () => ({ status: 'available' }), permissions: async () => ({ granted: true, complete: false, history: true }) })
    expect(await getHcState()).toEqual({ state: 'ready', history: true, complete: false })
  })
})

describe('how far back to read', () => {
  it('goes as far as allowed the first time, then just the last week', () => {
    expect(daysToRead(true, null)).toBe(365)
    expect(daysToRead(false, null)).toBe(30)
    expect(daysToRead(true, new Date())).toBe(7)
  })
})

describe('uploading', () => {
  it('reads, sends in chunks the server accepts, and remembers when', async () => {
    const read = vi.fn(async () => ({ daily: rows(1500, 'd'), sleep: rows(10, 's'), activities: rows(3, 'a') }))
    phone({ permissions: async () => ({ granted: true, complete: true, history: true }), read })

    const sent = await syncHealthConnect({ device: 'Pixel' })
    expect(read).toHaveBeenCalledWith({ days: 365 })
    expect(sent).toEqual({ daily: 1500, sleep: 10, activities: 3 })
    expect(ingest).toHaveBeenCalledTimes(3)                                   // 1500 daily rows = 700 + 700 + 100
    expect(ingest.mock.calls.every(([b]) => b.daily.length <= 700 && b.device === 'Pixel')).toBe(true)
    expect(ingest.mock.calls[0][0].sleep).toHaveLength(10)                    // the small kinds go in the first upload
    expect(ingest.mock.calls[1][0].sleep).toHaveLength(0)
    expect(lastHcUpload()).not.toBeNull()

    await syncHealthConnect()
    expect(read).toHaveBeenLastCalledWith({ days: 7 })                         // afterwards only the last week
  })

  it('without the history permission reads 30 days first', async () => {
    const read = vi.fn(async () => ({ daily: [], sleep: [], activities: [] }))
    phone({ permissions: async () => ({ granted: true, complete: false, history: false }), read })
    await syncHealthConnect()
    expect(read).toHaveBeenCalledWith({ days: 30 })
    expect(ingest).toHaveBeenCalledTimes(1)
  })

  it('refuses to read without permission', async () => {
    const read = vi.fn()
    phone({ permissions: async () => ({ granted: false, complete: false, history: false }), read })
    await expect(syncHealthConnect()).rejects.toThrow('permission')
    expect(read).not.toHaveBeenCalled()
    expect(ingest).not.toHaveBeenCalled()
  })

  it('does not remember a failed upload', async () => {
    phone({ permissions: async () => ({ granted: true, complete: true, history: true }), read: async () => ({ daily: rows(1), sleep: [], activities: [] }) })
    ingest.mockRejectedValueOnce(new Error('offline'))
    await expect(syncHealthConnect()).rejects.toThrow('offline')
    expect(lastHcUpload()).toBeNull()
  })
})

describe('automatic sync', () => {
  const ok = { permissions: async () => ({ granted: true, complete: true, history: true }), read: async () => ({ daily: rows(1), sleep: [], activities: [] }) }

  it('only runs when the person turned syncing on', async () => {
    phone(ok)
    expect(await autoSyncHealthConnect()).toBe(false)
    setHcEnabled(true)
    expect(hcEnabled()).toBe(true)
    expect(await autoSyncHealthConnect()).toBe(true)
  })

  it('does not hammer: a recent upload is skipped unless forced', async () => {
    phone(ok)
    setHcEnabled(true)
    expect(await autoSyncHealthConnect()).toBe(true)
    expect(await autoSyncHealthConnect()).toBe(false)
    expect(await autoSyncHealthConnect(true)).toBe(true)
  })

  it('never throws, even when everything fails', async () => {
    phone({ permissions: async () => { throw new Error('boom') } })
    setHcEnabled(true)
    expect(await autoSyncHealthConnect()).toBe(false)
  })

  it('turning it off forgets the last upload', async () => {
    phone(ok)
    setHcEnabled(true)
    await autoSyncHealthConnect()
    setHcEnabled(false)
    expect(hcEnabled()).toBe(false)
    expect(lastHcUpload()).toBeNull()
  })
})
