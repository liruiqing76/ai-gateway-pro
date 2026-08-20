<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">经验积累</h2>
    <p style="color:var(--gw-text-secondary);margin-bottom:20px;font-size:14px">
      从每次聊天的输入输出中自动提炼经验与教训，每日定时总结，并在下次调用时注入上下文。
    </p>

    <!-- 统计卡片 -->
    <div class="stats-grid" v-if="stats">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total }}</div>
        <div class="stat-label">经验总数</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.lessons }}</div>
        <div class="stat-label">教训</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.facts }}</div>
        <div class="stat-label">事实</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.week_added }}</div>
        <div class="stat-label">近7天新增</div>
      </div>
    </div>

    <!-- 操作栏 -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin:16px 0">
      <div style="display:flex;gap:8px">
        <el-button type="primary" :loading="distilling" @click="distill">
          <el-icon><MagicStick /></el-icon> 提炼经验
        </el-button>
        <el-button :loading="summarizing" @click="summarizeToday">
          <el-icon><DataAnalysis /></el-icon> 今日总结
        </el-button>
        <el-button @click="loadAll">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
      </div>
      <div style="display:flex;gap:8px">
        <el-select v-model="filterKind" placeholder="类型" style="width:110px" clearable @change="loadAll">
          <el-option label="全部" :value="null" />
          <el-option label="教训" value="lesson" />
          <el-option label="事实" value="fact" />
          <el-option label="流程" value="sop" />
        </el-select>
      </div>
    </div>

    <!-- 今日总结 -->
    <div v-if="todaySummary" class="summary-box">
      <h3 style="margin:0 0 8px">📋 {{ todaySummary.date }} 经验总结</h3>
      <div v-if="todaySummary.stats" style="font-size:13px;color:var(--gw-text-secondary);margin-bottom:8px">
        请求 {{ todaySummary.stats.requests }} · 成功 {{ todaySummary.stats.success }} ·
        Token {{ todaySummary.stats.total_tokens }} · 总字符 {{ todaySummary.stats.total_chars }}
      </div>
      <div style="white-space:pre-wrap;line-height:1.7">{{ todaySummary.content }}</div>
    </div>

    <h3 style="font-size:15px;font-weight:600;margin:20px 0 8px">🧠 经验库</h3>
    <el-table :data="experiences" style="width:100%" v-loading="loading" stripe row-key="id">
      <el-table-column prop="kind_label" label="类型" width="80" align="center">
        <template #default="{ row }">
          <el-tag :type="row.kind === 'lesson' ? 'danger' : row.kind === 'fact' ? 'info' : 'success'" size="small">
            {{ row.kind_label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="180">
        <template #default="{ row }">
          <span style="font-weight:500">{{ row.title }}</span>
          <el-tag v-if="row.pinned" size="small" type="warning" style="margin-left:6px">已钉</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="content" label="内容" min-width="320" show-overflow-tooltip>
        <template #default="{ row }">
          <span style="font-size:13px">{{ row.content }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="时间" width="160" sortable>
        <template #default="{ row }">
          <span style="font-size:12px;font-family:monospace">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140" align="center">
        <template #default="{ row }">
          <el-button size="small" type="success" link @click="feedback(row, true)">👍有效</el-button>
          <el-button size="small" type="danger" link @click="feedback(row, false)">👎无效</el-button>
          <el-button size="small" type="info" link @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 历史总结 -->
    <h3 style="font-size:15px;font-weight:600;margin:24px 0 8px">📅 历史每日总结</h3>
    <el-timeline v-if="summaries.length">
      <el-timeline-item v-for="s in summaries" :key="s.id" :timestamp="s.date">
        <div style="white-space:pre-wrap;line-height:1.6">{{ s.content }}</div>
        <div v-if="s.stats" style="font-size:12px;color:var(--gw-text-secondary);margin-top:4px">
          请求 {{ s.stats.requests }} · Token {{ s.stats.total_tokens }}
        </div>
      </el-timeline-item>
    </el-timeline>
    <el-empty v-else description="暂无每日总结" :image-size="80" />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
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
    ElMessage.error('加载失败')
  } finally {
    loading.value = false
  }
}

async function distill() {
  distilling.value = true
  try {
    const resp = await api.post('/api/experience/distill?limit=40')
    ElMessage.success(`已提炼 ${resp.data.created} 条经验`)
    await loadAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '提炼失败')
  } finally {
    distilling.value = false
  }
}

async function summarizeToday() {
  summarizing.value = true
  try {
    const resp = await api.post('/api/experience/summarize')
    todaySummary.value = resp.data
    ElMessage.success('今日总结已生成')
    await loadAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '总结失败')
  } finally {
    summarizing.value = false
  }
}

async function feedback(row, positive) {
  try {
    const resp = await api.post(`/api/experience/${row.id}/feedback?positive=${positive}`)
    ElMessage.success(positive ? '已标记有效并钉住' : '已标记无效')
    await loadAll()
  } catch (e) {
    ElMessage.error('操作失败')
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`删除经验「${row.title}」？`, '确认', { type: 'warning' })
    await api.delete(`/api/experience/${row.id}`)
    ElMessage.success('已删除')
    await loadAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败')
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
