<template>
  <div>
    <div class="page-header">
      <h2>{{ $t('channels.title') }}</h2>
      <div style="display:flex;gap:8px">
        <el-button @click="loadAll" :loading="loading">
          <el-icon style="margin-right:4px"><Refresh /></el-icon>{{ $t('common.refresh') }}
        </el-button>
        <el-button @click="importFromPlugin" :loading="importing">
          <el-icon style="margin-right:4px"><Download /></el-icon>{{ $t('channels.importPlugin') }}
        </el-button>
        <el-button type="primary" @click="checkinAll" :loading="checkingIn">
          <el-icon style="margin-right:4px"><Present /></el-icon>{{ $t('channels.checkinAll') }}
        </el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" style="margin-bottom:12px">
      <template #title>
        <span style="font-size:12px">{{ $t('channels.disclaimer') }}</span>
      </template>
    </el-alert>

    <div class="channel-grid" v-loading="loading">
      <el-card v-for="ch in channels" :key="ch.id" shadow="hover" class="channel-card">
        <div class="ch-head">
          <span class="ch-name">{{ ch.display_name }}</span>
          <el-tag :type="ch.connected ? 'success' : 'info'" size="small">
            {{ ch.connected ? $t('channels.connected') : $t('channels.notConnected') }}
          </el-tag>
        </div>
        <div class="ch-desc">{{ ch.description }}</div>

        <div class="ch-meta">
          <div><span class="k">{{ $t('channels.modelsNote') }}</span>{{ ch.models_note }}</div>
          <div><span class="k">{{ $t('channels.modelCount') }}</span>{{ ch.model_count }}</div>
        </div>

        <!-- 已连接: 展示真实模型名(工具里可直接选) -->
        <div v-if="ch.connected && ch.models.length" class="ch-models">
          <div class="ch-models-title">{{ $t('channels.availableModels') }}</div>
          <el-tag v-for="m in ch.models" :key="m" size="small" class="ch-model-tag"
                  @click="copyModel(ch.id + '-' + m)">
            {{ ch.id }}-{{ m }}
          </el-tag>
        </div>

        <div class="ch-actions">
          <!-- 免密车道(auto-sync): 无登录, 展示状态 + 手动同步 -->
          <template v-if="ch.read_only">
            <el-tag size="small" type="warning" effect="plain">{{ $t('channels.autoSync') }}</el-tag>
            <el-tag v-if="ch.lane_state === 'closed'" size="small" type="danger" effect="plain">
              {{ $t('channels.laneClosed') }}
            </el-tag>
            <el-tag v-else-if="ch.lane_state === 'open'" size="small" type="success" effect="plain">
              {{ $t('channels.laneOpen') }}
            </el-tag>
            <el-tag v-else-if="ch.lane_state" size="small" type="info" effect="plain">
              {{ $t('channels.laneUnknown') }}
            </el-tag>
            <el-tooltip v-if="ch.lane_resume && ch.lane_resume.at"
                        :content="ch.lane_resume.reason" placement="top">
              <el-tag size="small" type="warning" effect="dark">
                {{ $t('channels.laneResumeAt', { at: ch.lane_resume.at }) }}
              </el-tag>
            </el-tooltip>
            <el-button size="small" text :loading="syncingId === ch.id" @click="syncLane(ch)">
              {{ $t('channels.syncNow') }}
            </el-button>
          </template>
          <!-- 未连接: 登录按钮 -->
          <template v-else-if="!ch.connected">
            <el-button size="small" type="primary" @click="startLogin(ch)"
                       :loading="loggingIn === ch.id">
              {{ $t('channels.login') }}
            </el-button>
          </template>
          <!-- 已连接: 重新登录 + 单渠道签到 -->
          <template v-else>
            <el-button size="small" text @click="startLogin(ch)"
                       :loading="loggingIn === ch.id">
              {{ $t('channels.relogin') }}
            </el-button>
            <el-button size="small" text :disabled="!ch.has_checkin"
                       @click="checkinOne(ch)" :loading="checkingIn === ch.id">
              {{ $t('channels.checkin') }}
            </el-button>
            <el-button size="small" text type="danger" @click="disconnect(ch)">
              {{ $t('channels.disconnect') }}
            </el-button>
          </template>
        </div>
      </el-card>
    </div>

    <!-- 临时下架模型列表 -->
    <el-card v-if="downed.length || downedLoaded" shadow="never" style="margin-top:16px">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-weight:700">{{ $t('channels.downedTitle') }}</span>
          <el-button size="small" text @click="loadDowned">{{ $t('common.refresh') }}</el-button>
        </div>
      </template>
      <div style="font-size:12px;color:var(--gw-text-tertiary);margin-bottom:8px">
        {{ $t('channels.downedDesc') }}
      </div>
      <el-empty v-if="!downed.length" :description="$t('channels.downedEmpty')"
                :image-size="60" />
      <div v-else class="downed-list">
        <div v-for="d in downed" :key="d.provider_name + '||' + d.model" class="downed-item">
          <el-tag size="small" effect="plain" class="downed-name">
            {{ d.provider_name }}-{{ d.model }}
          </el-tag>
          <span class="downed-reason">{{ d.reason }}</span>
          <span class="downed-remaining">
            {{ $t('channels.downedRemaining', { time: fmtRemaining(d.remaining_seconds) }) }}
          </span>
          <el-button size="small" text type="primary"
                     :loading="recovering === d.provider_name + '||' + d.model"
                     @click="recoverDowned(d)">
            {{ $t('channels.downedRecover') }}
          </el-button>
        </div>
      </div>
    </el-card>

    <!-- 登录引导弹窗 -->
    <el-dialog v-model="loginDialog" :title="$t('channels.loginTitle')" width="480px"
               destroy-on-close>
      <div style="line-height:1.8;font-size:13px">
        <p>{{ $t('channels.loginStep1') }}</p>
        <el-input v-model="loginUrl" readonly>
          <template #append>
            <el-button @click="openAuthUrl">{{ $t('channels.openPage') }}</el-button>
          </template>
        </el-input>
        <!-- 设备码流(MiniMax 等): 需在授权页输入 user_code -->
        <div v-if="loginUserCode" class="device-code">
          <div class="device-code-label">{{ $t('channels.deviceCodeLabel') }}</div>
          <div class="device-code-value" @click="copyText(loginUserCode)">{{ loginUserCode }}</div>
          <div class="device-code-hint">{{ $t('channels.deviceCodeHint') }}</div>
        </div>
        <p style="margin-top:12px">{{ $t('channels.loginStep2') }}</p>
        <div class="login-status">
          <el-icon class="is-loading" v-if="loginPolling"><Loading /></el-icon>
          <span>{{ loginStatusText }}</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="loginDialog = false">{{ $t('common.close') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const { t } = useI18n()
const loading = ref(false)
const channels = ref([])
const loggingIn = ref(null)
const checkingIn = ref(null)
const syncingId = ref(null)
const importing = ref(false)

const loginDialog = ref(false)
const loginUrl = ref('')
const loginUserCode = ref('')
const loginPolling = ref(false)
const loginStatusText = ref('')
let loginTimer = null
let loginChannel = null

const downed = ref([])
const downedLoaded = ref(false)
const recovering = ref(null)

onMounted(() => { loadAll(); loadDowned() })
onBeforeUnmount(() => { if (loginTimer) clearTimeout(loginTimer) })

async function loadAll(retry = 0) {
  loading.value = true
  try {
    const resp = await api.get('/api/channels')
    channels.value = resp.data.channels || []
  } catch (e) {
    // 后端慢启动(打包版首次解包)时窗口已开但 API 未就绪: 自动重试最多 ~60s;
    // 401(未登录/令牌失效)不重试, 拦截器已负责跳登录页
    const is401 = e.response && e.response.status === 401
    if (!is401 && e.message !== 'Unauthorized' && retry < 20) {
      setTimeout(() => loadAll(retry + 1), 3000)
      return
    }
    if (!is401 && e.message !== 'Unauthorized') ElMessage.error(t('channels.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function startLogin(ch) {
  loggingIn.value = ch.id
  loginChannel = ch.id
  try {
    const resp = await api.post(`/api/channels/${ch.id}/login`)
    loginUrl.value = resp.data.auth_url
    loginUserCode.value = resp.data.user_code || ''
    const { login_id } = resp.data
    loginDialog.value = true
    loginPolling.value = true
    loginStatusText.value = t('channels.waitingAuth')
    pollLogin(login_id)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('channels.loginStartFailed'))
  } finally {
    loggingIn.value = null
  }
}

function pollLogin(login_id) {
  if (loginTimer) clearTimeout(loginTimer)
  loginTimer = setTimeout(async () => {
    if (!loginDialog.value) return
    try {
      const resp = await api.get(`/api/channels/login/${login_id}`)
      const d = resp.data
      if (d.status === 'done') {
        loginPolling.value = false
        loginStatusText.value = t('channels.loginDone', { n: d.model_count })
        ElMessage.success(t('channels.loginDone', { n: d.model_count }))
        await loadAll()
        setTimeout(() => { loginDialog.value = false }, 1500)
        return
      }
      if (d.status === 'failed') {
        loginPolling.value = false
        loginStatusText.value = d.error || t('channels.loginFailed')
        return
      }
      pollLogin(login_id)
    } catch (e) {
      loginPolling.value = false
      loginStatusText.value = t('channels.loginFailed')
    }
  }, 2000)
}

async function syncLane(ch) {
  syncingId.value = ch.id
  try {
    const resp = await api.post(`/api/channels/${ch.id}/sync`)
    const d = resp.data
    if (d.ok) {
      ElMessage.success(t('channels.syncDone', { n: (d.usable || []).length }))
      await loadAll()
    } else {
      ElMessage.warning(d.skipped || d.error || t('channels.syncFailed'))
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.error || t('channels.syncFailed'))
  } finally {
    syncingId.value = null
  }
}

function openAuthUrl() {
  window.open(loginUrl.value, '_blank')
}

async function importFromPlugin() {
  importing.value = true
  try {
    const scan = await api.get('/api/channels/import/scan')
    const accts = scan.data.accounts || []
    if (!accts.length) {
      ElMessage.warning(t('channels.importNone', { path: scan.data.credentials_path }))
      return
    }
    const lines = accts.map(a =>
      `${a.channel}${a.nickname ? ' (' + a.nickname + ')' : ''}`
      + (a.already_connected ? ' — ' + t('channels.importAlready') : '')
      + (a.error ? ' — ' + a.error : '')
    ).join('\n')
    await ElMessageBox.confirm(lines, t('channels.importTitle', { n: accts.length }),
      { confirmButtonText: t('channels.importConfirm'),
        cancelButtonText: t('common.cancel'), type: 'info' })
    const resp = await api.post('/api/channels/import/apply')
    const d = resp.data
    const failed = (d.results || []).filter(r => !r.ok)
    if (failed.length) {
      ElMessage.warning(t('channels.importPartial',
        { ok: d.imported, total: d.total,
          errs: failed.map(f => f.channel + ': ' + (f.error || '')).join('; ') }))
    } else {
      ElMessage.success(t('channels.importDone',
        { ok: d.imported, total: d.total }))
    }
    await loadAll()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') {
      ElMessage.error(e.response?.data?.error || e.response?.data?.detail
        || t('channels.importFailed'))
    }
  } finally {
    importing.value = false
  }
}

async function checkinAll() {
  checkingIn.value = true
  try {
    const resp = await api.post('/api/channels/checkin')
    const d = resp.data
    ElMessage.success(t('channels.checkinResult',
      { claimed: d.claimed, already: d.already, failed: d.failed, credit: d.total_credit }))
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('channels.checkinFailed'))
  } finally {
    if (checkingIn.value === true) checkingIn.value = false
  }
}

async function checkinOne(ch) {
  checkingIn.value = ch.id
  try {
    const resp = await api.post(`/api/channels/checkin/${ch.id}`)
    const r = resp.data
    if (r.kind === 'claimed') ElMessage.success(t('channels.claimed', { c: r.credit }))
    else if (r.kind === 'already-claimed') ElMessage.info(t('channels.alreadyClaimed'))
    else if (r.kind === 'inactive') ElMessage.warning(r.message)
    else ElMessage.error(r.message || t('channels.checkinFailed'))
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('channels.checkinFailed'))
  } finally {
    checkingIn.value = null
  }
}

async function disconnect(ch) {
  await ElMessageBox.confirm(t('channels.disconnectConfirm', { name: ch.display_name }),
    t('common.confirm'), { type: 'warning' })
  try {
    // 渠道 Provider 通过通用删除接口按 id 删; 先查 id
    const list = await api.get('/api/providers')
    const p = (list.data || []).find(x => x.name === ch.id)
    if (p) {
      await api.delete(`/api/providers/${p.id}`)
      ElMessage.success(t('channels.disconnected'))
      await loadAll()
    } else {
      ElMessage.info(t('channels.notConnected'))
    }
  } catch (e) {
    ElMessage.error(t('channels.disconnectFailed'))
  }
}

function copyModel(fullId) {
  navigator.clipboard?.writeText(fullId)
  ElMessage.success(t('common.copied'))
}

function copyText(text) {
  if (!text) return
  navigator.clipboard?.writeText(text)
  ElMessage.success(t('common.copied'))
}

async function loadDowned() {
  try {
    const resp = await api.get('/api/downed-models')
    downed.value = resp.data.items || []
    downedLoaded.value = true
  } catch (e) {
    if (e.message !== 'Unauthorized') ElMessage.error(t('channels.downedLoadFailed'))
  }
}

async function recoverDowned(d) {
  const k = d.provider_name + '||' + d.model
  recovering.value = k
  try {
    await api.post('/api/downed-models/recover', {
      provider_name: d.provider_name, model: d.model,
    })
    ElMessage.success(t('channels.downedRecovered', { name: k }))
    await loadDowned()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('channels.downedRecoverFailed'))
  } finally {
    recovering.value = null
  }
}

function fmtRemaining(sec) {
  if (!sec && sec !== 0) return ''
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return h > 0 ? `${h}h${m}m` : `${m}m`
}
</script>

<style scoped>
.channel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}
.channel-card { display: flex; flex-direction: column; gap: 8px; }
.ch-head { display: flex; justify-content: space-between; align-items: center; }
.ch-name { font-weight: 700; font-size: 15px; }
.ch-desc { font-size: 12px; color: var(--gw-text-secondary); line-height: 1.5; }
.ch-meta { font-size: 12px; color: var(--gw-text-tertiary); display: flex; flex-direction: column; gap: 4px; }
.ch-meta .k { color: var(--gw-text-secondary); margin-right: 6px; }
.ch-models { border-top: 1px solid var(--gw-border, #eef0f4); padding-top: 8px; }
.ch-models-title { font-size: 12px; color: var(--gw-text-secondary); margin-bottom: 6px; }
.ch-model-tag { margin: 0 6px 6px 0; cursor: pointer; font-family: monospace; }
.ch-actions { display: flex; gap: 8px; align-items: center; margin-top: auto; }
.login-status { display: flex; align-items: center; gap: 6px; color: var(--gw-accent); }
.device-code { margin-top: 12px; padding: 10px 12px; border-radius: 8px;
  background: var(--gw-bg-secondary, #f7f8fa); text-align: center; }
.device-code-label { font-size: 12px; color: var(--gw-text-secondary); margin-bottom: 6px; }
.device-code-value { font-family: monospace; font-size: 22px; font-weight: 700;
  letter-spacing: 3px; cursor: pointer; color: var(--gw-accent); }
.device-code-hint { font-size: 11px; color: var(--gw-text-tertiary); margin-top: 6px; }
.downed-list { display: flex; flex-direction: column; gap: 8px; }
.downed-item { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.downed-name { font-family: monospace; }
.downed-reason { color: var(--gw-text-tertiary); flex: 0 0 auto; }
.downed-remaining { color: var(--gw-text-secondary); margin-left: auto; }
</style>
