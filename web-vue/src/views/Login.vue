<template>
  <div class="login-split">
    <!-- 左侧品牌区 -->
    <aside class="brand-panel">
      <div class="brand-decor brand-decor--1"></div>
      <div class="brand-decor brand-decor--2"></div>
      <div class="brand-decor brand-decor--grid"></div>

      <div class="brand-content">
        <div class="brand-mark">
          <img class="brand-mark-img" src="/frenchie-logo.png" alt="AI 网关" />
        </div>
        <h1 class="brand-title">{{ $t('app.title') }}</h1>
        <p class="brand-tagline">{{ $t('login.tagline') }}</p>

        <ul class="brand-features">
          <li>
            <span class="feat-icon"><el-icon><Check /></el-icon></span>
            <span>{{ $t('login.feature1') }}</span>
          </li>
          <li>
            <span class="feat-icon"><el-icon><Check /></el-icon></span>
            <span>{{ $t('login.feature2') }}</span>
          </li>
          <li>
            <span class="feat-icon"><el-icon><Check /></el-icon></span>
            <span>{{ $t('login.feature3') }}</span>
          </li>
        </ul>
      </div>

      <div class="brand-footer">© 2026 AI 网关 · Smart Gateway</div>
    </aside>

    <!-- 右侧表单区 -->
    <main class="form-panel">
      <div class="login-card">
        <div class="form-brand">
          <span class="form-brand-mark"><img src="/frenchie-logo.png" alt="AI 网关" /></span>
          <span class="form-brand-name">{{ $t('app.title') }}</span>
        </div>

        <h2 class="form-title">
          {{ mode === 'login' ? $t('login.welcomeBack') : $t('login.createAccount') }}
        </h2>
        <p class="form-subtitle">{{ $t('login.subtitle') }}</p>

        <el-form :model="form" label-position="top" @submit.prevent>
          <el-form-item :label="$t('login.email')">
            <el-input v-model="form.email" :placeholder="$t('login.emailPlaceholder')" size="large" />
          </el-form-item>
          <el-form-item :label="$t('login.username')" v-if="mode === 'register'">
            <el-input v-model="form.username" :placeholder="$t('login.username')" size="large" />
          </el-form-item>
          <el-form-item :label="$t('login.password')">
            <el-input
              v-model="form.password"
              type="password"
              :placeholder="$t('login.passwordPlaceholder')"
              show-password
              size="large"
            />
          </el-form-item>
          <el-button
            type="primary"
            class="submit-btn"
            @click="submit"
            :loading="loading"
            size="large"
          >
            {{ mode === 'login' ? $t('login.loginBtn') : $t('login.registerBtn') }}
          </el-button>
        </el-form>

        <div class="mode-switch">
          <span>{{ mode === 'login' ? $t('login.noAccount') : $t('login.hasAccount') }}</span>
          <el-link type="primary" style="margin-left: 4px" @click="switchMode">
            {{ mode === 'login' ? $t('login.goRegister') : $t('login.goLogin') }}
          </el-link>
        </div>

        <div class="lang-row">
          <el-radio-group :model-value="locale" size="small" @update:model-value="changeLocale">
            <el-radio-button value="zh-CN">中文</el-radio-button>
            <el-radio-button value="en-US">EN</el-radio-button>
          </el-radio-group>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../api'
import { setLocale } from '../i18n'

const { t, locale } = useI18n()

const mode = ref('login')
const loading = ref(false)
const form = ref({ email: '', username: '', password: '' })

function changeLocale(lang) {
  setLocale(lang)
}

function switchMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
}

async function submit() {
  if (!form.value.email || !form.value.password) {
    ElMessage.warning(t('login.missingEmailPwd'))
    return
  }
  if (mode.value === 'register' && !form.value.username) {
    ElMessage.warning(t('login.missingUsername'))
    return
  }
  loading.value = true
  try {
    const url = mode.value === 'login' ? '/api/auth/login' : '/api/auth/register'
    const body = mode.value === 'login'
      ? { email: form.value.email, password: form.value.password }
      : { email: form.value.email, username: form.value.username, password: form.value.password }
    const resp = await api.post(url, body)
    localStorage.setItem('gw_token', resp.data.access_token)
    localStorage.setItem('gw_api_key', resp.data.api_key || '')
    localStorage.setItem('gw_username', resp.data.user?.username || form.value.email)
    localStorage.setItem('gw_email', form.value.email)
    if (mode.value === 'register') {
      ElMessage.success(t('login.registerOk'))
    } else {
      ElMessage.success(t('login.loginOk'))
    }
    window.location.hash = '#/dashboard'
    window.location.reload()
  } catch (e) {
    const detail = e.response?.data?.detail
    if (Array.isArray(detail)) {
      ElMessage.error(detail[0]?.msg || t('login.validationFailed'))
    } else {
      ElMessage.error(detail || t('common.operationFailed'))
    }
  } finally { loading.value = false }
}
</script>

<style scoped>
.login-split {
  display: flex;
  min-height: 100vh;
  width: 100%;
}

/* ---- 左侧品牌区 ---- */
.brand-panel {
  position: relative;
  flex: 0 0 46%;
  max-width: 560px;
  overflow: hidden;
  background: linear-gradient(150deg, #0A1A3F 0%, #122C6E 48%, #1A6CFF 100%);
  color: #fff;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 56px 52px;
}
.brand-decor {
  position: absolute;
  border-radius: 50%;
  filter: blur(60px);
  opacity: 0.5;
  pointer-events: none;
}
.brand-decor--1 {
  width: 360px; height: 360px;
  top: -120px; right: -100px;
  background: radial-gradient(circle, rgba(255, 138, 61, 0.55), transparent 70%);
}
.brand-decor--2 {
  width: 440px; height: 440px;
  bottom: -170px; left: -130px;
  background: radial-gradient(circle, rgba(64, 158, 255, 0.55), transparent 70%);
}
.brand-decor--grid {
  inset: 0;
  border-radius: 0;
  filter: none;
  opacity: 0.06;
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.6) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.6) 1px, transparent 1px);
  background-size: 32px 32px;
}

.brand-content { position: relative; z-index: 1; }
.brand-mark {
  width: 84px; height: 84px;
  border-radius: 22px;
  background: rgba(255, 255, 255, 0.12);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border: 1px solid rgba(255, 255, 255, 0.25);
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.25);
  margin-bottom: 28px;
}
.brand-mark-img { width: 54px; height: 54px; object-fit: contain; }
.brand-title { font-size: 34px; font-weight: 700; letter-spacing: 0.5px; margin: 0 0 10px; }
.brand-tagline { font-size: 15px; color: rgba(255, 255, 255, 0.78); margin: 0 0 36px; }

.brand-features { list-style: none; display: flex; flex-direction: column; gap: 16px; padding: 0; margin: 0; }
.brand-features li { display: flex; align-items: center; gap: 12px; font-size: 15px; color: rgba(255, 255, 255, 0.92); }
.feat-icon {
  flex-shrink: 0;
  width: 26px; height: 26px;
  border-radius: 50%;
  background: rgba(255, 138, 61, 0.18);
  border: 1px solid rgba(255, 138, 61, 0.5);
  color: #FFB277;
  display: inline-flex; align-items: center; justify-content: center;
}
.feat-icon :deep(.el-icon) { font-size: 15px; }

.brand-footer { position: relative; z-index: 1; font-size: 12px; color: rgba(255, 255, 255, 0.5); }

/* ---- 右侧表单区 ---- */
.form-panel {
  flex: 1 1 auto;
  display: flex; align-items: center; justify-content: center;
  padding: 40px;
  background: radial-gradient(1200px 600px at 70% -10%, #EAF2FF 0%, var(--gw-background) 55%);
}
.login-card {
  width: 100%;
  max-width: 400px;
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: 18px;
  padding: 40px 36px;
  box-shadow: var(--gw-shadow-popover);
}
.form-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 22px; }
.form-brand-mark {
  width: 40px; height: 40px;
  border-radius: 11px;
  background: var(--gw-primary-50);
  display: inline-flex; align-items: center; justify-content: center;
}
.form-brand-mark img { width: 26px; height: 26px; object-fit: contain; }
.form-brand-name { font-size: 18px; font-weight: 700; color: var(--gw-foreground); }

.form-title { font-size: 24px; font-weight: 700; color: var(--gw-foreground); margin: 0 0 6px; }
.form-subtitle { font-size: 13px; color: var(--gw-muted-foreground); margin: 0 0 26px; }

.login-card :deep(.el-form-item) { margin-bottom: 18px; }
.login-card :deep(.el-input__wrapper) { border-radius: 10px; }

/* 渐变主按钮 */
.login-card .submit-btn {
  width: 100%;
  height: 46px;
  margin-top: 6px;
  border: none;
  border-radius: 12px;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 2px;
  color: #fff;
  background: linear-gradient(135deg, #4096FF 0%, #1677FF 100%);
  box-shadow: 0 8px 20px -6px rgba(22, 119, 255, 0.55);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.login-card .submit-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 12px 26px -6px rgba(22, 119, 255, 0.6);
}

.mode-switch { text-align: center; margin-top: 18px; font-size: 13px; color: var(--gw-muted-foreground); }
.lang-row { text-align: center; margin-top: 22px; }

/* 响应式: 窄屏隐藏品牌区 */
@media (max-width: 860px) {
  .brand-panel { display: none; }
  .form-panel {
    background: linear-gradient(160deg, #EAF2FF 0%, var(--gw-background) 60%);
  }
}
</style>
