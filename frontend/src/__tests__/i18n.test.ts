import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import en from '../i18n/en.json'
import tr from '../i18n/tr.json'

type Tree = { [k: string]: string | Tree }

function flatten(tree: Tree, prefix = ''): string[] {
  return Object.entries(tree).flatMap(([k, v]) =>
    typeof v === 'string' ? [prefix + k] : flatten(v, `${prefix}${k}.`)
  )
}

function placeholders(s: string): string[] {
  return [...s.matchAll(/\{\{(\w+)\}\}/g)].map(m => m[1]).sort()
}

function lookup(tree: Tree, key: string): string | undefined {
  let cur: string | Tree | undefined = tree
  for (const part of key.split('.')) {
    if (typeof cur !== 'object' || cur === null) return undefined
    cur = cur[part]
  }
  return typeof cur === 'string' ? cur : undefined
}

const enKeys = flatten(en as Tree)
const trKeys = flatten(tr as Tree)

describe('translations', () => {
  it('English and Turkish have exactly the same keys', () => {
    expect(trKeys.filter(k => !enKeys.includes(k))).toEqual([])
    expect(enKeys.filter(k => !trKeys.includes(k))).toEqual([])
  })

  it('no empty strings', () => {
    for (const k of enKeys) expect(lookup(en as Tree, k)!.trim(), `en ${k}`).not.toBe('')
    for (const k of trKeys) expect(lookup(tr as Tree, k)!.trim(), `tr ${k}`).not.toBe('')
  })

  it('both languages use the same {{placeholders}}', () => {
    for (const k of enKeys) {
      expect(placeholders(lookup(tr as Tree, k)!), `placeholders of ${k}`).toEqual(placeholders(lookup(en as Tree, k)!))
    }
  })

  it('every t("key") used in the source exists', () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const src = join(here, '..')
    const files: string[] = []
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name)
        if (statSync(p).isDirectory()) { if (name !== '__tests__') walk(p) }
        else if (/\.(tsx?|jsx?)$/.test(name)) files.push(p)
      }
    }
    walk(src)

    // plural forms (key_one / key_other) count as the base key
    const known = new Set(enKeys.map(k => k.replace(/_(one|other)$/, '')))
    const missing: string[] = []
    for (const f of files) {
      const text = readFileSync(f, 'utf8')
      for (const m of text.matchAll(/\bt\(\s*['"]([\w.]+)['"]/g)) {
        if (!known.has(m[1])) missing.push(`${f.slice(src.length + 1)}: ${m[1]}`)
      }
      // header keys stored as data (PersonalBestsTable column presets)
      for (const m of text.matchAll(/header:\s*'([\w.]+)'/g)) {
        if (!known.has(m[1])) missing.push(`${f.slice(src.length + 1)}: ${m[1]}`)
      }
    }
    expect(missing).toEqual([])
  })
})
