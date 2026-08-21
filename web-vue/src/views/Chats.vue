<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">{{ $t('chats.title') }}</h2>
    <p style="color:var(--gw-text-secondary);margin-bottom:20px;font-size:14px">
      {{ $t('chats.subtitle') }}
    </p>

    <!-- 统计卡片 -->
    <div class="stats-grid" v-if="stats">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total_chats }}</div>
        <div class="stat-label">{{ $t('chats.totalChats', { days: stats.period_days }) }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" :class="stats.success_rate >= 95 ? 'text-green' : stats.success_rate >= 80 ? 'text-yellow' : 'text-red'">
          {{ stats.success_rate }}%
        </div>
        <div class="stat-label">{{ $t('chats.successRate') }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.avg_total_chars }}</div>
        <div class="stat-label">{{ $t('chats.avgTotalLength') }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value text-blue">{{ formatNum(stats.sum_total_chars) }}</div>
        <div class="stat-label">{{ $t('chats.totalIoLength') }}</div>
      </div>
    </div>

    <!-- 操作栏 -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin:16px 0">
      <div style="display:flex;gap:8px">
        <el-button @click="loadChats" :loading="loading">
          <el-icon><Refresh /></el-icon> {{ $t('common.refresh') }}
        </el-button>
        <el-button @click="loadStats" :loading="statsLoading">
          <el-icon><DataAnalysis /></el-icon> {{ $t('chats.statistics') }}
        </el-button>
        <el-button type="danger" plain @click="cleanupChats">
          <el-icon><Delete /></el-icon> {{ $t('chats.cleanupOldRecords') }}
        </el-button>
      </div>
      <div style="display:flex;gap:8px">
        <el-input v-model="filterModel" :placeholder="$t('chats.filterByModel')" style="width:200px" clearable @clear="loadChats" @keyup.enter="loadChats">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="filterSuccess" :placeholder="$t('chats.status')" style="width:120px" clearable @change="loadChats">
          <el-option :label="$t('chats.all')" :value="null" />
          <el-option :label="$t('common.success')" :value="true" />
          <el-option :label="$t('common.failed')" :value="false" />
        </el-select>
        <el-switch v-model="filterPipeline" :active-text="$t('chats.pipeline')" :inactive-text="$t('chats.all')" @change="loadChats" style="margin-left:8px" />
      </div>
    </div>

    <!-- 列表 -->
    <el-table :data="chats" style="width:100%" v-loading="loading" stripe row-key="id"
      :default-sort="{ prop: 'created_at', order: 'descending' }">
      <el-table-column prop="created_at" :label="$t('chats.time')" width="170" sortable>
        <template #default="{ row }">
          <span style="font-size:12px;font-family:monospace">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="original_model" :label="$t('chats.requestModel')" min-width="130">
        <template #default="{ row }">
          <div>
            <span style="font-weight:500">{{ row.original_model }}</span>
            <el-tag v-if="row.is_pipeline" size="small" type="info" style="margin-left:4px">{{ row.pipeline_mode }}</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="provider_name" :label="$t('chats.provider')" min-width="110">
        <template #default="{ row }">
          <span v-if="row.provider_name">{{ row.provider_name }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column :label="$t('chats.inputSummary')" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">
          <span style="font-size:12px;color:var(--gw-text-secondary)">{{ row.prompt_preview || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="$t('chats.lengthInOut')" width="160" align="center">
        <template #default="{ row }">
          <span style="font-size:12px">{{ row.prompt_chars }} / {{ row.completion_chars }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="total_chars" :label="$t('chats.totalLength')" width="90" sortable>
        <template #default="{ row }">
          <span style="font-size:12px;font-weight:500">{{ formatNum(row.total_chars) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="success" :label="$t('chats.status')" width="70" align="center">
        <template #default="{ row }">
          <el-tag :type="row.success ? 'success' : 'danger'" size="small">
            {{ row.success ? '✓' : '✗' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="$t('chats.actions')" width="80" align="center">
        <template #default="{ row }">
          <el-button type="primary" link @click="showDetail(row)">{{ $t('chats.details') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <div style="margin-top:16px;display:flex;justify-content:center">
      <el-pagination
        v-model:current-page="currentPage"
        :page-size="50"
        :total="totalCount"
        layout="prev, pager, next"
        @current-change="loadChats"
      />
    </div>

    <!-- 详情弹窗 -->
    <el-dialog :title="$t('chats.detailTitle')" v-model="detailVisible" width="780px" top="6vh">
      <div v-if="detailRow" class="chat-detail">
        <div class="detail-section">
          <h4>{{ $t('chats.requestInfo') }}</h4>
          <div class="detail-grid">
            <div><span class="detail-key">{{ $t('chats.time') }}:</span> {{ formatTime(detailRow.created_at) }}</div>
            <div><span class="detail-key">{{ $t('chats.requestId') }}:</span> <code>{{ detailRow.request_id || '-' }}</code></div>
            <div><span class="detail-key">{{ $t('chats.originalModel') }}:</span> {{ detailRow.original_model }}</div>
            <div><span class="detail-key">{{ $t('chats.actualProvider') }}:</span> {{ detailRow.provider_name || '-' }}</div>
            <div><span class="detail-key">{{ $t('chats.actualModel') }}:</span> {{ detailRow.model_name || '-' }}</div>
            <div><span class="detail-key">{{ $t('chats.stream') }}:</span> {{ detailRow.is_stream ? $t('chats.yes') : $t('chats.no') }}</div>
          </div>
        </div>

        <div class="detail-section">
          <h4>{{ $t('chats.lengthStats') }}</h4>
          <div class="detail-grid">
            <div>{{ $t('chats.inputChars') }}: {{ formatNum(detailRow.prompt_chars) }}</div>
            <div>{{ $t('chats.outputChars') }}: {{ formatNum(detailRow.completion_chars) }}</div>
            <div>{{ $t('chats.totalChars') }}: <b>{{ formatNum(detailRow.total_chars) }}</b></div>
            <div><span class="detail-key">Token:</span> {{ detailRow.prompt_tokens || 0 }} → {{ detailRow.completion_tokens || 0 }} ({{ $t('chats.total') }} {{ detailRow.total_tokens || 0 }})</div>
          </div>
        </div>

        <div class="detail-section">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h4 style="margin:0">{{ $t('chats.inputWithCount', { count: formatNum(detailRow.prompt_chars) }) }}</h4>
            <el-button size="small" text @click="copyText(detailRow.prompt_text)">{{ $t('common.copy') }}</el-button>
          </div>
          <div class="text-box">{{ detailRow.prompt_text || $t('chats.emptyContent') }}</div>
        </div>

        <div class="detail-section" v-if="detailRow.success">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h4 style="margin:0">{{ $t('chats.outputWithCount', { count: formatNum(detailRow.completion_chars) }) }}</h4>
            <el-button size="small" text @click="copyText(detailRow.completion_text)">{{ $t('common.copy') }}</el-button>
          </div>
          <div class="text-box">{{ detailRow.completion_text || $t('chats.emptyContent') }}</div>
        </div>

        <div class="detail-section" v-if="!detailRow.success">
          <h4>{{ $t('chats.error') }}</h4>
          <div style="color:var(--gw-color-danger)">{{ detailRow.error_msg || $t('common.failed') }}</div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search, Delete, DataAnalysis } from '@element-plus/icons-vue'
import api from '../api'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const chats = ref([])
const stats = ref(null)
const loading = ref(false)
const statsLoading = ref(false)
const totalCount = ref(0)
const currentPage = ref(1)
const filterModel = ref('')
const filterSuccess = ref(null)
const filterPipeline = ref(false)
const detailVisible = ref(false)
const detailRow = ref(null)

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', { hour12: false })
}
function formatNum(n) {
  return Number(n || 0).toLocaleString()
}

async function loadChats() {
  loading.value = true
  try {
    const params = { limit: 50, offset: (currentPage.value - 1) * 50 }
    if (filterModel.value) params.model = filterModel.value
    if (filterSuccess.value !== null) params.success = filterSuccess.value
    if (filterPipeline.value) params.pipeline_only = true
    const resp = await api.get('/api/chats', { params })
    chats.value = resp.data.data || []
    totalCount.value = resp.data.total || 0
  } catch (e) {
    ElMessage.error(t('chats.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  statsLoading.value = true
  try {
    const resp = await api.get('/api/chats/stats', { params: { days: 7 } })
    stats.value = resp.data
  } catch (e) {
    ElMessage.error(t('chats.statsLoadFailed'))
  } finally {
    statsLoading.value = false
  }
}

async function showDetail(row) {
  try {
    const resp = await api.get(`/api/chats/${row.id}`)
    detailRow.value = resp.data
    detailVisible.value = true
  } catch (e) {
    ElMessage.error(t('chats.detailLoadFailed'))
  }
}

async function cleanupChats() {
  try {
    await ElMessageBox.confirm(t('chats.cleanupConfirmMessage'), t('chats.cleanupConfirmTitle'), { type: 'warning' })
    const resp = await api.delete('/api/chats/cleanup', { params: { days: 30 } })
    ElMessage.success(t('chats.cleanedCount', { count: resp.data.deleted }))
    await loadChats()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(t('chats.cleanupFailed'))
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text || '')
    ElMessage.success(t('common.copied'))
  } catch (e) {
    ElMessage.warning(t('chats.copyFailed'))
  }
}

onMounted(() => {
  loadChats()
  loadStats()
})
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
.text-green { color: #22c55e; }
.text-yellow { color: #eab308; }
.text-red { color: #ef4444; }
.text-blue { color: #3b82f6; }

.chat-detail h4 {
  font-size: 14px;
  font-weight: 600;
  margin: 12px 0 8px;
  color: var(--gw-text-primary);
}
.detail-section {
  border-bottom: 1px solid var(--gw-border);
  padding-bottom: 12px;
  margin-bottom: 12px;
}
.detail-section:last-child {
  border-bottom: none;
}
.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  font-size: 13px;
}
.detail-key {
  color: var(--gw-text-secondary);
  margin-right: 4px;
}
.text-box {
  background: var(--gw-surface-secondary);
  border: 1px solid var(--gw-border);
  border-radius: 8px;
  padding: 12px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow-y: auto;
  margin-top: 8px;
}
code {
  background: var(--gw-surface-secondary);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}
</style>
