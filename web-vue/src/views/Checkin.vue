<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">自动签到</h2>

    <!-- 仅在 pywebview 壳内可见 -->
    <el-alert
      v-if="!isDesktop"
      type="warning"
      title="此页面仅在桌面客户端内可用"
      description="请运行 desktop.py 打开桌面客户端，这里会显示自动签到状态与定时设置。"
      :closable="false"
      style="margin-bottom:16px"
    />

    <!-- 定时设置 -->
    <el-card shadow="hover" style="margin-bottom:20px">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span class="card-title">⏰ 定时签到</span>
          <el-tag :type="schedule.enabled ? 'success' : 'info'" size="small">
            {{ schedule.enabled ? '已启用' : '已停用' }}
          </el-tag>
        </div>
      </template>

      <el-form label-width="110px" style="max-width:480px">
        <el-form-item label="总开关">
          <el-switch v-model="schedule.enabled" @change="saveSchedule" />
        </el-form-item>
        <el-form-item label="WorkBuddy 时间">
          <el-time-picker
            v-model="wbTime"
            format="HH:mm"
            value-format="HH:mm"
            placeholder="选择签到时间"
            :disabled="!schedule.enabled"
            @change="saveSchedule"
          />
        </el-form-item>
        <el-form-item label="Trae 时间">
          <el-time-picker
            v-model="traeTime"
            format="HH:mm"
            value-format="HH:mm"
            placeholder="选择签到时间"
            :disabled="!schedule.enabled"
            @change="saveSchedule"
          />
        </el-form-item>
        <el-form-item label="下次执行">
          <div style="font-size:13px;color:#94a3b8">
            WorkBuddy: {{ schedule.next_run?.workbuddy ? formatTime(schedule.next_run.workbuddy) : '—' }} &nbsp;·&nbsp;
            Trae: {{ schedule.next_run?.trae ? formatTime(schedule.next_run.trae) : '—' }}
          </div>
        </el-form-item>
      </el-form>
      <div style="font-size:12px;color:#64748b">
        💡 桌面客户端开着就会自动签到。如果不常开客户端，可配合 Windows 计划任务或开机自启。
      </div>
    </el-card>

    <el-row :gutter="16" style="margin-bottom:20px">
      <!-- WorkBuddy -->
      <el-col :span="12">
        <el-card shadow="hover">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span class="card-title">🤖 WorkBuddy 签到</span>
              <el-tag :type="wb.signed ? 'success' : 'info'" size="small">{{ wb.signed ? '今日已签' : '未签到' }}</el-tag>
            </div>
          </template>
          <div style="font-size:13px;color:#94a3b8;margin-bottom:12px">
            最后签到: {{ wb.date || '从未' }}
          </div>
          <el-button type="primary" :loading="running === 'workbuddy'" @click="run('workbuddy')" :disabled="wb.signed">
            {{ wb.signed ? '✅ 今日已完成' : '立即签到' }}
          </el-button>
        </el-card>
      </el-col>

      <!-- Trae Work -->
      <el-col :span="12">
        <el-card shadow="hover">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span class="card-title">🎯 Trae Work 签到</span>
              <el-tag :type="trae.signed ? 'success' : 'info'" size="small">{{ trae.signed ? '今日已签' : '未签到' }}</el-tag>
            </div>
          </template>
          <div style="font-size:13px;color:#94a3b8;margin-bottom:12px">
            最后签到: {{ trae.date || '从未' }}
          </div>
          <el-button type="primary" :loading="running === 'trae'" @click="run('trae')" :disabled="trae.signed">
            {{ trae.signed ? '✅ 今日已完成' : '立即签到' }}
          </el-button>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="hover" v-if="log">
      <template #header><span class="card-title">签到日志</span></template>
      <pre style="background:#1e293b;padding:14px;border-radius:8px;font-size:12px;max-height:300px;overflow-y:auto;color:#cbd5e1;white-space:pre-wrap">{{ log }}</pre>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'

const isDesktop = ref(typeof window.pywebview !== 'undefined')
const wb = ref({ signed: false, date: '' })
const trae = ref({ signed: false, date: '' })
const running = ref('')
const log = ref('')
const schedule = ref({
  enabled: true,
  workbuddy_time: '09:00',
  trae_time: '09:05',
  next_run: null,
})
const wbTime = ref('09:00')
const traeTime = ref('09:05')

function formatTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function refresh() {
  if (!isDesktop.value) return
  try {
    const status = await window.pywebview.api.checkin.get_status()
    const d = JSON.parse(status)
    wb.value = d.workbuddy
    trae.value = d.trae
    if (d.schedule) {
      schedule.value = d.schedule
      wbTime.value = d.schedule.workbuddy_time
      traeTime.value = d.schedule.trae_time
    }
  } catch (e) {
    ElMessage.error('获取签到状态失败: ' + e)
  }
}

async function saveSchedule() {
  if (!isDesktop.value) return
  try {
    const data = {
      enabled: schedule.value.enabled,
      workbuddy_time: wbTime.value || '09:00',
      trae_time: traeTime.value || '09:05',
    }
    const resp = await window.pywebview.api.checkin.save_schedule(JSON.stringify(data))
    const d = JSON.parse(resp)
    if (d.ok) {
      ElMessage.success('定时设置已保存')
      schedule.value.enabled = d.config.enabled ?? schedule.value.enabled
      refresh()
    } else {
      ElMessage.error('保存失败: ' + (d.error || '未知错误'))
    }
  } catch (e) {
    ElMessage.error('保存失败: ' + e)
  }
}

async function run(app) {
  running.value = app
  log.value = '签到中...'
  try {
    const resp = await window.pywebview.api.checkin.run_checkin(app)
    const d = JSON.parse(resp)
    const result = d[app] || resp
    log.value = result
    ElMessage.info(result.includes('✅') ? '签到成功' : '签到完成，查看日志')
  } catch (e) {
    log.value = '执行失败: ' + e
    ElMessage.error('签到执行失败')
  } finally {
    running.value = ''
    refresh()
  }
}

onMounted(refresh)
</script>