// ============================================================
// API 封装 — 自动带 JWT; access token 过期时用 refresh token
// 静默换新并重放原请求, 刷新也失败才清会话跳登录页
// ============================================================
import axios from 'axios'

// pywebview(同源 http://127.0.0.1:8765 加载) 下不能用相对 '/', 要用绝对地址;
// 浏览器/dev 模式留空走相对路径(axios 默认 baseURL)。
const API_BASE = (window.pywebview !== undefined)
  ? 'http://127.0.0.1:8765'
  : ''

const api = axios.create({
  baseURL: API_BASE || undefined,
  timeout: 60000,
})

api.interceptors.request.use(config => {
  const token = localStorage.getItem('gw_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

function clearAuth() {
  localStorage.removeItem('gw_token')
  localStorage.removeItem('gw_refresh_token')
  localStorage.removeItem('gw_api_key')
}

// ── 静默刷新: 并发多个 401 只发一次刷新请求(单飞) ──
let _refreshing = null
function tryRefresh() {
  const rt = localStorage.getItem('gw_refresh_token')
  if (!rt) return Promise.resolve(false)
  if (!_refreshing) {
    _refreshing = axios.post(`${API_BASE}/api/auth/refresh`, { refresh_token: rt })
      .then(resp => {
        localStorage.setItem('gw_token', resp.data.access_token)
        if (resp.data.refresh_token) {
          localStorage.setItem('gw_refresh_token', resp.data.refresh_token)
        }
        return true
      })
      .catch(() => {
        clearAuth()
        return false
      })
      .finally(() => { _refreshing = null })
  }
  return _refreshing
}

api.interceptors.response.use(
  resp => resp,
  async err => {
    const cfg = err.config
    if (err.response && err.response.status === 401 && cfg && !cfg._retried) {
      cfg._retried = true
      if (await tryRefresh()) {
        return api(cfg) // 已换到新 token, 重放原请求
      }
      window.location.hash = '#/login'
    } else if (err.response && err.response.status === 401) {
      // 重试过仍 401 / 无刷新令牌
      clearAuth()
      window.location.hash = '#/login'
    }
    return Promise.reject(err)
  }
)

export default api
