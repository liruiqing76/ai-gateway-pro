// ============================================================
// API 封装 — 自动带 JWT + 401 自动跳登录
// ============================================================
import axios from 'axios'

// pywebview(file://) 下不能用 '/', 要用绝对地址
const API_BASE = (window.pywebview !== undefined) 
  ? 'http://127.0.0.1:8765' 
  : '/'

const api = axios.create({
  baseURL: API_BASE,
  timeout: 60000,
})

api.interceptors.request.use(config => {
  const token = localStorage.getItem('gw_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  resp => resp,
  err => {
    if (err.response && err.response.status === 401) {
      localStorage.removeItem('gw_token')
      localStorage.removeItem('gw_api_key')
      window.location.hash = '#/login'
    }
    return Promise.reject(err)
  }
)

export default api