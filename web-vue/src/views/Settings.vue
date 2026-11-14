<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">{{ $t('settings.title') }}</h2>

    <!-- 仅在 pywebview 壳内可见 -->
    <el-alert
      v-if="!isDesktop"
      type="warning"
      :title="$t('settings.desktopOnlyTitle')"
      :description="$t('settings.desktopOnlyDesc')"
      :closable="false"
      style="margin-bottom:16px"
    />

    <!-- 通用设置 -->
    <el-card shadow="hover" class="page-card" style="margin-bottom:20px">
      <template #header><span class="card-title">{{ $t('settings.generalCard') }}</span></template>
      <el-form label-width="140px" label-position="left" style="max-width:560px">
        <el-form-item :label="$t('settings.language')">
          <el-radio-group :model-value="locale" @update:model-value="changeLocale">
            <el-radio-button value="zh-CN">简体中文</el-radio-button>
            <el-radio-button value="en-US">English</el-radio-button>
          </el-radio-group>
          <div class="setting-desc">{{ $t('settings.languageDesc') }}</div>
        </el-form-item>
        <el-form-item :label="$t('settings.localMode')">
          <el-switch
            v-model="authDisabled"
            :loading="authSwitching"
            @change="toggleLocalMode"
          />
          <div class="setting-desc">{{ $t('settings.localModeDesc') }}</div>
          <div class="setting-desc" style="color:var(--el-color-warning)">
            ⚠️ {{ $t('settings.localModeWarn') }}
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 桌面客户端设置(开机自启等) -->
    <el-card shadow="hover" style="margin-bottom:20px">
      <template #header><span class="card-title">{{ $t('settings.autoStartCard') }}</span></template>
      <template v-if="isDesktop">
        <el-form label-width="140px" label-position="left" style="max-width:640px">
          <el-form-item :label="$t('settings.autoStart')">
            <el-switch
              v-model="autoStart"
              :loading="switching"
              :disabled="!info.auto_start_supported"
              @change="toggleAutoStart"
            />
            <div class="setting-desc">
              {{ $t('settings.autoStartDesc') }}
              <template v-if="!info.auto_start_supported">
                <br />💡 {{ $t('settings.notPackagedTip') }}
              </template>
            </div>
          </el-form-item>
        </el-form>

        <!-- 应用信息 -->
        <el-descriptions :column="1" border size="small" style="max-width:640px;margin-top:8px">
          <el-descriptions-item :label="$t('settings.version')">v{{ info.version || '—' }}</el-descriptions-item>
          <el-descriptions-item :label="$t('settings.executable')">
            <span class="mono-path">{{ info.executable || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="info.exe_size_mb" :label="$t('settings.installSize')">
            {{ info.exe_size_mb }} MB
          </el-descriptions-item>
        </el-descriptions>
      </template>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { setLocale } from '../i18n'
import api from '../api'

const { t, locale } = useI18n()

const isDesktop = ref(typeof window.pywebview !== 'undefined')
const info = ref({ version: '', executable: '', auto_start_supported: false, auto_start_enabled: false })
const autoStart = ref(false)
const switching = ref(false)

// 免登录模式(单机自用): 关闭后网关全部接口免凭证, 面板打开即用
const authDisabled = ref(false)
const authSwitching = ref(false)

async function refreshLocalMode() {
  try {
    const resp = await api.get('/api/auth/local-mode')
    authDisabled.value = !!resp.data?.auth_disabled
  } catch { /* 后端不可达时保持默认关 */ }
}

async function toggleLocalMode(disabled) {
  authSwitching.value = true
  try {
    const resp = await api.post('/api/auth/local-mode', { auth_disabled: disabled })
    authDisabled.value = !!resp.data?.auth_disabled
    ElMessage.success(t(authDisabled.value ? 'settings.localModeOn' : 'settings.localModeOff'))
  } catch (e) {
    authDisabled.value = !disabled
    ElMessage.error(t('common.operationFailed') + ': ' + (e?.response?.data?.detail || e))
  } finally {
    authSwitching.value = false
  }
}

// 与 Checkin.vue 相同的约定: pywebview 的 JS 桥是异步注入的,
// 页面先于桥就绪时 window.pywebview?.api?.settings 为 undefined, 必须动态获取。
const getSettingsApi = () => window.pywebview?.api?.settings

/** 桥可能晚于页面就绪, 轻量重试直到拿到数据或超时 */
async function refreshWithRetry(retries = 10, intervalMs = 400) {
  for (let i = 0; i < retries; i++) {
    const api = getSettingsApi()
    if (api) {
      await refresh(api)
      if (info.value.executable) return
    }
    await new Promise(r => setTimeout(r, intervalMs))
  }
}

async function refresh(api = getSettingsApi()) {
  if (!api) return
  try {
    const d = JSON.parse(await api.get_settings())
    info.value = d
    autoStart.value = !!d.auto_start_enabled
  } catch (e) {
    ElMessage.error(t('settings.loadFailed', { error: e }))
  }
}

async function toggleAutoStart(enabled) {
  const api = getSettingsApi()
  if (!api) return
  switching.value = true
  try {
    const d = JSON.parse(await api.set_auto_start(JSON.stringify({ enabled })))
    if (d.ok) {
      autoStart.value = !!d.enabled
      ElMessage.success(t(d.enabled ? 'settings.autoStartOn' : 'settings.autoStartOff'))
    } else {
      autoStart.value = false
      // 后端返回 error_code 时本地化, 否则原样展示后端消息
      let msg
      if (d.error_code === 'not_packaged') msg = t('settings.errors.not_packaged')
      else if (d.error_code === 'exception') msg = t('settings.errors.exception', { error: d.error })
      else msg = d.error || t('common.unknownError')
      ElMessage.error(msg)
    }
  } catch (e) {
    autoStart.value = !autoStart.value
    ElMessage.error(t('common.operationFailed') + ': ' + e)
  } finally {
    switching.value = false
    refresh()
  }
}

function changeLocale(lang) {
  setLocale(lang)
}

onMounted(() => {
  if (isDesktop.value) refreshWithRetry()
  refreshLocalMode()
})
</script>

<style scoped>
.setting-desc {
  font-size: 12px;
  color: var(--gw-text-secondary);
  line-height: 1.6;
  margin-top: 2px;
}
.mono-path {
  font-family: monospace;
  font-size: 12px;
  word-break: break-all;
}
</style>
