<template>
  <div class="login-page">
    <div class="login-card">
      <!-- 品牌区: 与侧边栏 Logo 同一语言 -->
      <div class="brand-row">
        <span class="brand-icon"><el-icon :size="22"><Lightning /></el-icon></span>
        <h1 class="brand-name">{{ $t('app.title') }}</h1>
      </div>
      <p class="brand-subtitle">{{ $t('login.subtitle') }}</p>

      <el-form :model="form" label-position="top" @submit.prevent>
        <el-form-item :label="$t('login.email')">
          <el-input v-model="form.email" :placeholder="$t('login.emailPlaceholder')" />
        </el-form-item>
        <el-form-item :label="$t('login.username')" v-if="mode === 'register'">
          <el-input v-model="form.username" :placeholder="$t('login.username')" />
        </el-form-item>
        <el-form-item :label="$t('login.password')">
          <el-input v-model="form.password" type="password" :placeholder="$t('login.passwordPlaceholder')" show-password />
        </el-form-item>
        <el-button type="primary" class="submit-btn" @click="submit" :loading="loading">
          {{ mode === 'login' ? $t('login.loginBtn') : $t('login.registerBtn') }}
        </el-button>
        <div class="mode-switch">
          <span>{{ mode === 'login' ? $t('login.noAccount') : $t('login.hasAccount') }}</span>
          <el-link type="primary" style="margin-left:4px" @click="switchMode">
            {{ mode === 'login' ? $t('login.goRegister') : $t('login.goLogin') }}
          </el-link>
        </div>
      </el-form>

      <!-- 语言切换 -->
      <div class="lang-row">
        <el-radio-group :model-value="locale" size="small" @update:model-value="changeLocale">
          <el-radio-button value="zh-CN">中文</el-radio-button>
          <el-radio-button value="en-US">EN</el-radio-button>
        </el-radio-group>
      </div>
    </div>
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
.login-page {
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh;
  background: var(--gw-background);
}
.login-card {
  width: 400px;
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: var(--gw-radius-lg);
  padding: 36px;
  box-shadow: var(--gw-shadow-modal);
}
.brand-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
}
.brand-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: var(--gw-radius-md);
  background: var(--gw-primary-50);
  color: var(--gw-primary);
}
.brand-name {
  font-size: 22px;
  font-weight: 600;
  color: var(--gw-foreground);
}
.brand-subtitle {
  text-align: center;
  font-size: 13px;
  color: var(--gw-muted-foreground);
  margin: 8px 0 28px;
}
.submit-btn {
  width: 100%;
  height: 40px;
  font-size: 14px;
  font-weight: 500;
}
.mode-switch {
  text-align: center;
  margin-top: 16px;
  font-size: 13px;
  color: var(--gw-muted-foreground);
}
.lang-row {
  text-align: center;
  margin-top: 20px;
}
</style>
