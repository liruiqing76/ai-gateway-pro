// ============================================================
// vue-i18n 初始化 — 中文(zh-CN) / 英文(en-US)
// 语言偏好持久化在 localStorage('gw_locale')，默认跟随浏览器语言
// ============================================================
import { createI18n } from 'vue-i18n'
import zhCN from './locales/zh-CN.json'
import enUS from './locales/en-US.json'

export const LOCALE_KEY = 'gw_locale'
export const SUPPORT_LOCALES = ['zh-CN', 'en-US']

function detectLocale() {
  try {
    const saved = localStorage.getItem(LOCALE_KEY)
    if (saved && SUPPORT_LOCALES.includes(saved)) return saved
  } catch (e) { /* localStorage 不可用时忽略 */ }
  const nav = (navigator.language || 'zh-CN').toLowerCase()
  return nav.startsWith('zh') ? 'zh-CN' : 'en-US'
}

export const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale: detectLocale(),
  fallbackLocale: 'zh-CN',
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
  },
})

/** 切换语言并持久化(同时更新标签页标题) */
export function setLocale(locale) {
  if (!SUPPORT_LOCALES.includes(locale)) return
  i18n.global.locale.value = locale
  try { localStorage.setItem(LOCALE_KEY, locale) } catch (e) { /* ignore */ }
  document.title = `⚡ ${i18n.global.t('app.title')}`
}
