<template>
  <div>
    <div class="page-header">
      <h2>{{ $t('checkin.title') }}</h2>
      <div style="display:flex;gap:8px">
        <el-button @click="load" :loading="loading">
          <el-icon style="margin-right:4px"><Refresh /></el-icon>{{ $t('common.refresh') }}
        </el-button>
        <el-button type="primary" @click="checkinAll" :loading="checkingAll"
                   :disabled="!connectedChannels.length">
          <el-icon style="margin-right:4px"><Present /></el-icon>{{ $t('checkin.checkinAllBtn') }}
        </el-button>
      </div>
    </div>

    <!-- 自动签到日程(后端 CHANNEL_CHECKIN_* 配置, UTC+8) -->
    <el-card shadow="never" class="sched-card">
      <div class="sched-row">
        <el-icon :class="{ 'is-loading': loading }"><Clock /></el-icon>
        <span class="sched-title">{{ $t('checkin.scheduleTitle') }}</span>
        <el-tag :type="schedule.enabled ? 'success' : 'info'" size="small">
          {{ schedule.enabled ? $t('checkin.enabled') : $t('checkin.disabled') }}
        </el-tag>
        <span class="sched-txt" v-if="schedule.enabled">
          {{ $t('checkin.scheduleDaily', { time: schedule.time, tz: schedule.timezone }) }}
        </span>
        <span class="sched-txt" v-if="schedule.next_run_at">
          {{ $t('checkin.nextRunAt', { at: fmtTime(schedule.next_run_at) }) }}
        </span>
      </div>
      <div class="sched-hint">{{ $t('checkin.scheduleHint') }}</div>
    </el-card>

    <el-alert v-if="!connectedChannels.length" type="warning" :closable="false"
              style="margin-bottom:12px">
      <template #title>
        <span style="font-size:12px">{{ $t('checkin.noneConnected') }}</span>
      </template>
    </el-alert>

    <!-- 逐个渠道: 今日真实状态 + 单渠道立即签到 -->
    <div class="ck-grid" v-loading="loading">
      <el-card v-for="ch in cards" :key="ch.id" shadow="hover" class="ck-card">
        <div class="ck-head">
          <span class="ck-name">{{ ch.display_name }}</span>
          <el-tag :type="tagType(ch)" size="small">{{ tagText(ch) }}</el-tag>
        </div>

        <div class="ck-meta">
          <span class="k">{{ $t('checkin.creditLabel') }}</span>
          <span class="v">{{ ch.credit || 0 }}</span>
          <template v-if="ch.at">
            <span class="k" style="margin-left:12px">{{ $t('checkin.lastAt') }}</span>
            <span class="v">{{ fmtTime(ch.at * 1000) }}</span>
          </template>
        </div>

        <div v-if="ch.reason" class="ck-reason">
          <el-icon><WarningFilled /></el-icon><span>{{ ch.reason }}</span>
        </div>
        <div v-if="ch.models_note" class="ck-note">{{ ch.models_note }}</div>

        <div class="ck-actions">
          <el-button size="small" type="primary" :loading="checking === ch.id"
                     :disabled="isDone(ch) || !ch.connected"
                     @click="checkinOne(ch)">
            {{ isDone(ch) ? $t('checkin.claimedBtn') : $t('checkin.checkinNow') }}
          </el-button>
          <el-button v-if="!ch.connected" size="small" text @click="goChannels">
            {{ $t('checkin.goConnect') }}
          </el-button>
        </div>
      </el-card>
    </div>

    <!-- 上次一键签到的逐渠道明细 -->
    <el-card v-if="lastRun.length" shadow="never" style="margin-top:12px">
      <template #header>
        <span style="font-size:13px">{{ $t('checkin.lastRunTitle') }}</span>
      </template>
      <el-table :data="lastRun" size="small">
        <el-table-column prop="channel" :label="$t('checkin.colChannel')" width="160" />
        <el-table-column :label="$t('checkin.colResult')" width="160">
          <template #default="{ row }">
            <el-tag :type="kindType(row.kind)" size="small">{{ kindText(row.kind) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="credit" :label="$t('checkin.colCredit')" width="100" />
        <el-table-column prop="message" :label="$t('checkin.colMessage')" show-overflow-tooltip />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import api from '../api'

const { t } = useI18n()

const loading = ref(false)
const checkingAll = ref(false)
const checking = ref(null)
const supporting = ref([])
const connected = ref([])
const today = ref({ channels: {} })
const schedule = ref({ enabled: false, time: '09:30', timezone: 'UTC+8', next_run_at: null })
const catalog = ref([])
const lastRun = ref([])

const connectedChannels = computed(() => connected.value || [])

// 只展示"支持签到"的渠道(免密车道没有账号, 也就没有签到)
const cards = computed(() => {
  const cat = new Map(catalog.value.map(c => [c.id, c]))
  return (supporting.value || []).map(id => {
    const c = cat.get(id) || {}
    const st = (today.value.channels || {})[id] || {}
    return {
      id,
      display_name: c.display_name || id,
      models_note: c.models_note || '',
      has_checkin: c.has_checkin !== false,
      connected: connectedChannels.value.includes(id),
      kind: st.kind || '',
      credit: st.credit || 0,
      reason: st.reason || '',
      at: st.at || 0,
    }
  })
})

onMounted(load)

async function load() {
  loading.value = true
  try {
    const resp = await api.get('/api/channels/checkin/status')
    const d = resp.data || {}
    supporting.value = d.supporting || []
    connected.value = d.connected || []
    today.value = d.today || { channels: {} }
    if (d.schedule) schedule.value = d.schedule
    catalog.value = d.catalog || []
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('checkin.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function checkinAll() {
  checkingAll.value = true
  try {
    const resp = await api.post('/api/channels/checkin')
    const d = resp.data || {}
    lastRun.value = d.results || []
    if (d.results && d.results.length) {
      ElMessage.success(t('checkin.runDone', {
        claimed: d.claimed || 0, already: d.already || 0,
        failed: d.failed || 0, credit: d.total_credit || 0,
      }))
    } else {
      ElMessage.warning(t('checkin.runEmpty'))
    }
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('checkin.runFailed'))
  } finally {
    checkingAll.value = false
  }
}

async function checkinOne(ch) {
  checking.value = ch.id
  try {
    const resp = await api.post(`/api/channels/checkin/${ch.id}`)
    const d = resp.data || {}
    if (d.kind === 'claimed') {
      ElMessage.success(t('checkin.oneClaimed', { ch: ch.display_name, credit: d.credit || 0 }))
    } else if (d.kind === 'already-claimed') {
      ElMessage.info(t('checkin.oneAlready', { ch: ch.display_name }))
    } else if (d.kind === 'unsupported') {
      ElMessage.warning(d.message || t('checkin.oneUnsupported'))
    } else {
      ElMessage.warning(`${ch.display_name}: ${d.message || t('checkin.oneFailed')}`)
    }
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('checkin.oneFailed'))
  } finally {
    checking.value = null
  }
}

function isDone(ch) {
  return ch.kind === 'claimed' || ch.kind === 'already-claimed'
}

function tagType(ch) {
  if (!ch.connected) return 'info'
  if (ch.kind === 'claimed') return 'success'
  if (ch.kind === 'already-claimed') return 'success'
  if (ch.kind === 'failed') return 'danger'
  if (ch.kind === 'inactive') return 'warning'
  return 'info'
}

function tagText(ch) {
  if (!ch.connected) return t('checkin.stNotConnected')
  if (ch.kind === 'claimed') return t('checkin.stClaimed')
  if (ch.kind === 'already-claimed') return t('checkin.stAlready')
  if (ch.kind === 'failed') return t('checkin.stFailed')
  if (ch.kind === 'inactive') return t('checkin.stInactive')
  return t('checkin.stPending')
}

function kindType(kind) {
  return { claimed: 'success', 'already-claimed': 'success', skipped: 'success',
    failed: 'danger', inactive: 'warning', unsupported: 'info' }[kind] || 'info'
}

function kindText(kind) {
  return { claimed: t('checkin.stClaimed'), 'already-claimed': t('checkin.stAlready'),
    skipped: t('checkin.kSkipped'), failed: t('checkin.stFailed'),
    inactive: t('checkin.stInactive'), unsupported: t('checkin.oneUnsupported') }[kind] || kind || '-'
}

function fmtTime(v) {
  if (!v) return '-'
  let d
  if (typeof v === 'string') {
    d = new Date(v.length === 19 ? v : v)
  } else {
    d = new Date(v)
  }
  if (Number.isNaN(d.getTime())) return String(v)
  const p = n => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function goChannels() {
  window.location.hash = '#channels'
  window.location.reload()
}
</script>

<style scoped>
.page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.page-header h2 { margin: 0; font-size: 18px; }
.sched-card { margin-bottom: 12px; }
.sched-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sched-title { font-weight: 600; font-size: 13px; }
.sched-txt { font-size: 12px; color: #606266; }
.sched-hint { margin-top: 6px; font-size: 12px; color: #909399; }
.ck-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
.ck-card { display: flex; flex-direction: column; }
.ck-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.ck-name { font-weight: 600; font-size: 14px; }
.ck-meta { margin: 8px 0 4px; font-size: 12px; color: #606266; }
.ck-meta .k { color: #909399; margin-right: 4px; }
.ck-meta .v { font-weight: 600; color: #303133; margin-right: 14px; }
.ck-reason { display: flex; align-items: flex-start; gap: 4px; margin-top: 6px; padding: 6px 8px;
  background: #fef0f0; border-radius: 4px; color: #f56c6c; font-size: 12px; line-height: 1.5; }
.ck-note { margin-top: 6px; font-size: 12px; color: #909399; }
.ck-actions { margin-top: 10px; display: flex; align-items: center; gap: 6px; }
</style>
