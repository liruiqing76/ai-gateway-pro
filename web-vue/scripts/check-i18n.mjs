// ============================================================
// i18n 完整性检查: 扫描 src 下所有 .vue/.js 的 t('key')/$t('key')
// 引用, 核对在 zh-CN.json 与 en-US.json 中都存在。
// 用法: node scripts/check-i18n.mjs   (在 web-vue 目录下执行)
// ============================================================
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const pkgRoot = dirname(dirname(fileURLToPath(import.meta.url)))
const srcDir = join(pkgRoot, 'src')

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === 'fragments') continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (['.vue', '.js'].includes(extname(name))) out.push(p)
  }
  return out
}

function get(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj)
}

function flattenPrefix(obj, prefix = '') {
  // 返回该命名空间下实际存在的"叶子或分支"路径集合(分支也允许, 支持 $t('models.strategy.' + x) 动态用法)
  const set = new Set()
  for (const [k, v] of Object.entries(obj)) {
    const p = prefix ? `${prefix}.${k}` : k
    set.add(p)
    if (v && typeof v === 'object') for (const sub of flattenPrefix(v, p)) set.add(sub)
  }
  return set
}

const locales = {
  'zh-CN': JSON.parse(readFileSync(join(srcDir, 'i18n', 'locales', 'zh-CN.json'), 'utf8')),
  'en-US': JSON.parse(readFileSync(join(srcDir, 'i18n', 'locales', 'en-US.json'), 'utf8')),
}
const keySets = Object.fromEntries(
  Object.entries(locales).map(([l, m]) => [l, flattenPrefix(m)])
)

// 匹配 $t('xxx') / t("xxx") / i18n.global.t('xxx'), 以及模板字符串里的前缀引用
const callRe = /\$?t\(\s*['"`]([a-zA-Z][\w.]*)['"`]/g

let missing = 0
for (const file of walk(srcDir)) {
  const text = readFileSync(file, 'utf8')
  for (const m of text.matchAll(callRe)) {
    // 去掉动态拼接捕获的尾部点号, 如 $t('models.strategy.' + x) → 'models.strategy'
    const key = m[1].replace(/\.+$/, '')
    for (const [locale, keys] of Object.entries(keySets)) {
      if (!keys.has(key)) {
        // 允许动态拼接前缀: 如 $t(`app.nav.${x}`) 已被正则排除(反引号内含 ${}), 此处只查静态串
        console.log(`[缺失] ${locale}  ${key}  (${file.replace(srcDir, 'src')})`)
        missing++
      }
    }
  }
}
console.log(missing === 0 ? '[check-i18n] 全部引用完整 ✓' : `[check-i18n] 共 ${missing} 处缺失`)
process.exitCode = missing === 0 ? 0 : 1
