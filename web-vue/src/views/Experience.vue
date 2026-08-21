<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">{{ $t('experience.title') }}</h2>
    <p style="color:var(--gw-text-secondary);margin-bottom:20px;font-size:14px">
      {{ $t('experience.description') }}
    </p>

    <!-- 统计卡片 -->
    <div class="stats-grid" v-if="stats">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total }}</div>
        <div class="stat-label">{{ $t('experience.totalCount') }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.lessons }}</div>
        <div class="stat-label">{{ $t('experience.kindLesson') }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.facts }}</div>
        <div class="stat-label">{{ $t('experience.kindFact') }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.week_added }}</div>
        <div class="stat-label">{{ $t('experience.addedLast7Days') }}</div>
      </div>
    </div>

    <!-- 操作栏 -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin:16px 0">
      <div style="display:flex;gap:8px">
        <el-button type="primary" :loading="distilling" @click="distill">
          <el-icon><MagicStick /></el-icon> {{ $t('experience.distillAction') }}
        </el-button>
        <el-button :loading="summarizing" @click="summarizeToday">
          <el-icon><DataAnalysis /></el-icon> {{ $t('experience.summarizeAction') }}
        </el-button>
        <el-button @click="loadAll">
          <el-icon><Refresh /></el-icon> {{ $t('common.refresh') }}
        </el-button>
      </div>
      <div style="display:flex;gap:8px">
        <el-select v-model="filterKind" :placeholder="$t('experience.filterType')" style="width:110px" clearable @change="loadAll">
          <el-option :label="$t('experience.filterAll')" :value="null" />
          <el-option :label="$t('experience.kindLesson')" value="lesson" />
          <el-option :label="$t('experience.kindFact')" value="fact" />
          <el-option :label="$t('experience.filterProcess')" value="sop" />
        </el-select>
      </div>
    </div>

    <!-- 今日总结 -->
    <div v-if="todaySummary" class="summary-box">
      <h3 style="margin:0 0 8px">📋 {{ $t('experience.summaryTitle', { date: todaySummary.date }) }}</h3>
      <div v-if="todaySummary.stats" style="font-size:13px;color:var(--gw-text-secondary);margin-bottom:8px">
        {{ $t('experience.summaryStats', {
          requests: todaySummary.stats.requests,
          success: todaySummary.stats.success,
          tokens: todaySummary.stats.total_tokens,
          chars: todaySummary.stats.total_chars
        }) }}
      </div>
      <div style="white-space:pre-wrap;line-height:1.7">{{ todaySummary.content }}</div>
    </div>

    <h3 style="font-size:15px;font-weight:600;margin:20px 0 8px">🧠 {{ $t('experience.libraryTitle') }}</h3>
    <el-table :data="experiences" style="width:100%" v-loading="loading" stripe row-key="id">
      <el-table-column prop="kind_label" :label="$t('experience.filterType')" width="80" align="center">
        <template #default="{ row }">
          <el-tag :type="row.kind === 'lesson' ? 'danger' : row.kind === 'fact' ? 'info' : 'success'" size="small">
            {{ row.kind_label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="title" :label="$t('experience.colTitle')" min-width="180">
        <template #default="{ row }">
          <span style="font-weight:500">{{ row.title }}</span>
          <el-tag v-if="row.pinned" size="small" type="warning" style="margin-left:6px">{{ $t('experience.pinned') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="content" :label="$t('experience.colContent')" min-width="320" show-overflow-tooltip>
        <template #default="{ row }">
          <span style="font-size:13px">{{ row.content }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" :label="$t('experience.colTime')" width="160" sortable>
        <template #default="{ row }">
          <span style="font-size:12px;font-family:monospace">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="$t('experience.colActions')" width="140" align="center">
        <template #default="{ row }">
          <el-button size="small" type="success" link @click="feedback(row, true)">👍{{ $t('experience.feedbackUseful') }}</el-button>
          <el-button size="small" type="danger" link @click="feedback(row, false)">👎{{ $t('experience.feedbackUseless') }}</el-button>
          <el-button size="small" type="info" link @click="remove(row)">{{ $t('common.delete') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 历史总结 -->
    <h3 style="font-size:15px;font-weight:600;margin:24px 0 8px">📅 {{ $t('experience.historyTitle') }}</h3>
    <el-timeline v-if="summaries.length">
      <el-timeline-item v-for="s in summaries" :key="s.id" :timestamp="s.date">
        <div style="white-space:pre-wrap;line-height:1.6">{{ s.content }}</div>
        <div v-if="s.stats" style="font-size:12px;color:var(--gw-text-secondary);margin-top:4px">
          {{ $t('experience.historyStats', {
            requests: s.stats.requests,
            tokens: s.stats.total_tokens
          }) }}
        </div>
      </el-timeline-item>
    </el-timeline>
    <el-empty v-else :description="$t('experience.noDailySummary')" :image-size="80" />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, DataAnalysis, MagicStick } from '@element-plus/icons-vue'
import api from '../api'

const experiences = ref([])
const summaries = ref([])
const stats = ref(null)
const todaySummary = ref(null)
const loading = ref(false)
const distilling = ref(false)
const summarizing = ref(false)
const filterKind = ref(null)

const { t } = useI18n()

function formatTime(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

async function loadAll() {
  loading.value = true
  try {
    const params = {}
    if (filterKind.value) params.kind = filterKind.value
    const exp = await api.get('/api/experience', { params })
    experiences.value = exp.data.data || []
    const st = await api.get('/api/experience/stats')
    stats.value = st.data
    const sum = await api.get('/api/experience/summaries', { params: { limit: 20 } })
    summaries.value = sum.data.data || []
    if (summaries.value.length) {
      todaySummary.value = summaries.value[0]
    }
  } catch (e) {
    ElMessage.error(t('experience.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function distill() {
  distilling.value = true
  try {
    const resp = await api.post('/api/experience/distill?limit=40')
    ElMessage.success(t('experience.distilledCount', { count: resp.data.created }))
    await loadAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('experience.distillFailed'))
  } finally {
    distilling.value = false
  }
}

async function summarizeToday() {
  summarizing.value = true
  try {
    const resp = await api.post('/api/experience/summarize')
    todaySummary.value = resp.data
    ElMessage.success(t('experience.summaryGenerated'))
    await loadAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('experience.summarizeFailed'))
  } finally {
    summarizing.value = false
  }
}

async function feedback(row, positive) {
  try {
    const resp = await api.post(`/api/experience/${row.id}/feedback?positive=${positive}`)
    ElMessage.success(positive ? t('experience.markedUsefulPinned') : t('experience.markedUseless'))
    await loadAll()
  } catch (e) {
    ElMessage.error(t('common.operationFailed'))
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(t('experience.deleteConfirm', { title: row.title }), t('experience.confirmTitle'), { type: 'warning' })
    await api.delete(`/api/experience/${row.id}`)
    ElMessage.success(t('experience.deleted'))
    await loadAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(t('experience.deleteFailed'))
  }
}

onMounted(loadAll)
</script>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.stat-card {
  background: var(--gw-surface);
  border: 1px solid var(--gw-border);
  border-radius: 12px;
  padding: 16px;
  text-align: center;
}
.stat-value {
  font-size: 24px;
  font-weight: 700;
  color: var(--gw-text-primary);
}
.stat-label {
  font-size: 12px;
  color: var(--gw-text-secondary);
  margin-top: 4px;
}
.summary-box {
  background: var(--gw-surface);
  border: 1px solid var(--gw-border);
  border-left: 4px solid #6366f1;
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 8px;
}
</style>
