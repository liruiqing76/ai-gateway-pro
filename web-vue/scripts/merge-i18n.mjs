// ============================================================
// 合并语言包: base.*.json + fragments/<ns>.<locale>.json → <locale>.json
// 用法: node scripts/merge-i18n.mjs   (在 web-vue 目录下执行)
// ============================================================
import { readFileSync, writeFileSync, readdirSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const pkgRoot = dirname(dirname(fileURLToPath(import.meta.url)))
const localesDir = join(pkgRoot, 'src', 'i18n', 'locales')
const fragDir = join(pkgRoot, 'src', 'i18n', 'fragments')

function deepMerge(target, src) {
  for (const [k, v] of Object.entries(src)) {
    if (
      v && typeof v === 'object' && !Array.isArray(v) &&
      target[k] && typeof target[k] === 'object' && !Array.isArray(target[k])
    ) {
      deepMerge(target[k], v)
    } else {
      target[k] = v
    }
  }
  return target
}

const fragments = existsSync(fragDir) ? readdirSync(fragDir) : []
for (const locale of ['zh-CN', 'en-US']) {
  const merged = JSON.parse(readFileSync(join(localesDir, `base.${locale}.json`), 'utf8'))
  let count = 0
  for (const f of fragments.sort()) {
    if (!f.endsWith(`.${locale}.json`)) continue
    deepMerge(merged, JSON.parse(readFileSync(join(fragDir, f), 'utf8')))
    count++
  }
  writeFileSync(join(localesDir, `${locale}.json`), JSON.stringify(merged, null, 2) + '\n')
  console.log(`[merge-i18n] ${locale}: ${Object.keys(merged).length} 个命名空间, 合并 ${count} 个片段`)
}
