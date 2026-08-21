<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">{{ $t('checkin.autoCheckinTitle') }}</h2>

    <!-- 仅在 pywebview 壳内可见 -->
    <el-alert
      v-if="!isDesktop"
      type="warning"
      :title="$t('checkin.desktopOnlyTitle')"
      :description="$t('checkin.desktopOnlyDesc')"
      :closable="false"
      style="margin-bottom:16px"
    />

    <!-- 定时设置 -->
    <el-card shadow="hover" style="margin-bottom:20px">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span class="card-title">{{ $t('checkin.scheduledCheckin') }}</span>
          <el-tag :type="schedule.enabled ? 'success' : 'info'" size="small">
            {{ schedule.enabled ? $t('common.enabled') : $t('common.disabled') }}
          </el-tag>
        </div>
      </template>

      <el-form label-width="110px" style="max-width:480px">
        <el-form-item :label="$t('checkin.masterSwitch')">
          <el-switch v-model="schedule.enabled" @change="saveSchedule" />
        </el-form-item>
        <el-form-item :label="$t('checkin.workbuddyTime')">
          <el-time-picker
            v-model="wbTime"
            format="HH:mm"
            value-format="HH:mm"
            :placeholder="$t('checkin.selectCheckinTime')"
            :disabled="!schedule.enabled"
            @change="saveSchedule"
          />
        </el-form-item>
        <el-form-item :label="$t('checkin.traeTime')">
          <el-time-picker
            v-model="traeTime"
            format="HH:mm"
            value-format="HH:mm"
            :placeholder="$t('checkin.selectCheckinTime')"
            :disabled="!schedule.enabled"
            @change="saveSchedule"
          />
        </el-form-item>
        <el-form-item :label="$t('checkin.nextRun')">
          <div style="font-size:13px;color:var(--gw-text-secondary)">
            WorkBuddy: {{ schedule.next_run?.workbuddy ? formatTime(schedule.next_run.workbuddy) : '—' }} &nbsp;·&nbsp;
            Trae: {{ schedule.next_run?.trae ? formatTime(schedule.next_run.trae) : '—' }}
          </div>
        </el-form-item>
      </el-form>
      <div style="font-size:12px;color:#64748b">
        {{ $t('checkin.tipDesktopAuto') }}
      </div>
    </el-card>

    <el-row :gutter="16" style="margin-bottom:20px">
      <!-- WorkBuddy -->
      <el-col :span="12">
        <el-card shadow="hover">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span class="card-title">{{ $t('checkin.workbuddyCardTitle') }}</span>
              <el-tag :type="wb.signed ? 'success' : 'info'" size="small">{{ wb.signed ? $t('checkin.signedToday') : $t('checkin.notSigned') }}</el-tag>
            </div>
          </template>
          <div style="font-size:13px;color:var(--gw-text-secondary);margin-bottom:12px">
            {{ $t('checkin.lastCheckin') }}: {{ wb.date || $t('common.never') }}
          </div>
          <el-button type="primary" :loading="running === 'workbuddy'" @click="run('workbuddy')" :disabled="wb.signed">
            {{ wb.signed ? $t('checkin.completedToday') : $t('checkin.checkinNow') }}
          </el-button>
        </el-card>
      </el-col>

      <!-- Trae Work -->
      <el-col :span="12">
        <el-card shadow="hover">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span class="card-title">{{ $t('checkin.traeCardTitle') }}</span>
              <el-tag :type="trae.signed ? 'success' : 'info'" size="small">{{ trae.signed ? $t('checkin.signedToday') : $t('checkin.notSigned') }}</el-tag>
            </div>
          </template>
          <div style="font-size:13px;color:var(--gw-text-secondary);margin-bottom:12px">
            {{ $t('checkin.lastCheckin') }}: {{ trae.date || $t('common.never') }}
          </div>
          <el-button type="primary" :loading="running === 'trae'" @click="run('trae')" :disabled="trae.signed">
            {{ trae.signed ? $t('checkin.completedToday') : $t('checkin.checkinNow') }}
          </el-button>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="hover" v-if="log">
      <template #header><span class="card-title">{{ $t('checkin.checkinLog') }}</span></template>
      <pre style="background:var(--gw-muted);padding:14px;border-radius:8px;font-family:var(--gw-font-mono);font-size:12px;max-height:300px;overflow-y:auto;color:var(--gw-foreground);white-space:pre-wrap">{{ log }}</pre>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const isDesktop = ref(typeof window.pywebview !== 'undefined')
// 不能在这里一次性取快照: pywebview 的 JS 桥是异步注入的, 页面先于桥就绪时
// window.pywebview?.api?.checkin 会是 undefined, 点击就静默 return(表现为"没反应")。
// 改为每次调用前动态获取。
const getCheckinApi = () => window.pywebview?.api?.checkin
const wb = ref({ signed: false, date: '' })
const trae = ref({ signed: false, date: '' })
const running = ref('')
const log = ref('')
const schedule = ref({
  enabled: true,
  workbuddy_time: '02:00',
  trae_time: '02:05',
  next_run: null,
})
const wbTime = ref('02:00')
const traeTime = ref('02:05')

function formatTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function refresh() {
  const checkinApi = getCheckinApi()
  if (!isDesktop.value || !checkinApi) return
  try {
    const status = await checkinApi.get_status()
    const d = JSON.parse(status)
    wb.value = d.workbuddy
    trae.value = d.trae
    if (d.schedule) {
      schedule.value = d.schedule
      wbTime.value = d.schedule.workbuddy_time
      traeTime.value = d.schedule.trae_time
    }
  } catch (e) {
    ElMessage.error(t('checkin.getStatusFailed') + e)
  }
}

async function saveSchedule() {
  const checkinApi = getCheckinApi()
  if (!isDesktop.value || !checkinApi) return
  try {
    const data = {
      enabled: schedule.value.enabled,
      workbuddy_time: wbTime.value || '02:00',
      trae_time: traeTime.value || '02:05',
    }
    const resp = await checkinApi.save_schedule(JSON.stringify(data))
    const d = JSON.parse(resp)
    if (d.ok) {
      ElMessage.success(t('checkin.scheduleSaved'))
      schedule.value.enabled = d.config.enabled ?? schedule.value.enabled
      refresh()
    } else {
      ElMessage.error(t('checkin.saveFailed') + (d.error || t('common.unknownError')))
    }
  } catch (e) {
    ElMessage.error(t('checkin.saveFailed') + e)
  }
}

async function run(app) {
  const checkinApi = getCheckinApi()
  if (!checkinApi) return
  running.value = app
  log.value = t('checkin.checkingIn')
  try {
    const resp = await checkinApi.run_checkin(app)
    const d = JSON.parse(resp)
    const result = d[app] || resp
    log.value = result
    ElMessage.info(result.includes('✅') ? t('checkin.checkinSuccess') : t('checkin.checkinDoneViewLog'))
  } catch (e) {
    log.value = t('checkin.execFailed') + e
    ElMessage.error(t('checkin.checkinExecFailed'))
  } finally {
    running.value = ''
    refresh()
  }
}

onMounted(refresh)
</script>
