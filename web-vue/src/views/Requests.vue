<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">{{ $t('requests.pageTitle') }}</h2>
    <p style="color:var(--gw-text-secondary);margin-bottom:20px;font-size:14px">
      {{ $t('requests.pageDesc') }}
    </p>

    <!-- 统计卡片 -->
    <div class="stats-grid" v-if="stats">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total_requests }}</div>
        <div class="stat-label">{{ $t('traces.totalRequests', { days: stats.period_days }) }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" :class="stats.success_rate >= 95 ? 'text-green' : stats.success_rate >= 80 ? 'text-yellow' : 'text-red'">
          {{ stats.success_rate }}%
        </div>
        <div class="stat-label">{{ $t('traces.successRate') }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value text-blue">{{ formatNum(stats.sum_total_tokens) }}</div>
        <div class="stat-label">{{ $t('requests.totalTokensCard') }}</div>
        <div class="stat-sublabel">{{ $t('requests.charsCaption', { chars: formatNum(stats.sum_total_chars) }) }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" style="color:var(--gw-muted-foreground)">{{ stats.probe_count ?? 0 }}</div>
        <div class="stat-label">{{ $t('traces.probeRequests', { days: stats.period_days }) }}</div>
      </div>
    </div>

    <!-- 操作栏 -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin:16px 0">
      <div style="display:flex;gap:8px">
        <el-button @click="loadRequests" :loading="loading">
          <el-icon><Refresh /></el-icon> {{ $t('common.refresh') }}
        </el-button>
        <el-button type="danger" plain @click="cleanupAll" :loading="cleanupLoading">
          <el-icon><Delete /></el-icon> {{ $t('requests.cleanup') }}
        </el-button>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <el-select v-model="filterRequestType" style="width:130px" @change="onFilterChange">
          <el-option :label="$t('traces.typeAll')" value="" />
          <el-option :label="$t('traces.typeReal')" value="real" />
          <el-option :label="$t('traces.typeProbe')" value="speed_test" />
        </el-select>
        <el-input v-model="filterModel" :placeholder="$t('traces.filterByModel')" style="width:200px" clearable @clear="onFilterChange" @keyup.enter="onFilterChange">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="filterSuccess" :placeholder="$t('traces.status')" style="width:120px" clearable @change="onFilterChange">
          <el-option :label="$t('traces.all')" :value="null" />
          <el-option :label="$t('common.success')" :value="true" />
          <el-option :label="$t('common.failed')" :value="false" />
        </el-select>
        <el-switch v-model="filterPipeline" :active-text="$t('traces.orchestration')" :inactive-text="$t('traces.all')" @change="onFilterChange" style="margin-left:8px" />
      </div>
    </div>

    <!-- 合并列表 -->
    <el-table :data="rows" style="width:100%" v-loading="loading" stripe row-key="rowKey"
      :default-sort="{ prop: 'created_at', order: 'descending' }">
      <el-table-column prop="created_at" :label="$t('traces.time')" width="170" sortable>
        <template #default="{ row }">
          <span style="font-size:12px;font-family:monospace">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="original_model" :label="$t('traces.requestModel')" min-width="170">
        <template #default="{ row }">
          <div>
            <span style="font-weight:500">{{ row.original_model || '-' }}</span>
            <el-tag v-if="row.request_type === 'speed_test'" type="info" size="small" style="margin-left:4px">{{ $t('traces.probeTag') }}</el-tag>
            <el-tag v-if="row.is_pipeline" size="small" type="info" style="margin-left:4px">{{ row.pipeline_mode }}</el-tag>
            <div v-if="row.resolved_model && row.resolved_model !== row.original_model"
                 style="font-size:11px;color:var(--gw-text-secondary);font-family:monospace">
              → {{ row.resolved_model }}
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="$t('chats.inputSummary')" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.prompt_preview" style="font-size:12px">{{ row.prompt_preview }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="provider_name" :label="$t('traces.actualProvider')" min-width="120">
        <template #default="{ row }">
          <span v-if="row.provider_name">{{ row.provider_name }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="model_name" :label="$t('traces.actualModel')" min-width="150">
        <template #default="{ row }">
          <span v-if="row.model_name" style="font-family:monospace;font-size:12px">{{ row.model_name }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="total_tokens" label="Token" width="110" align="right">
        <template #default="{ row }">
          <span v-if="row.total_tokens" :title="$t('requests.charsCaption', { chars: Number(row.total_chars || 0).toLocaleString() })"
                style="font-size:12px">{{ Number(row.total_tokens).toLocaleString() }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="latency_ms" :label="$t('traces.latency')" width="90" sortable>
        <template #default="{ row }">
          <span :class="latencyClass(row.latency_ms)">{{ row.latency_ms ? row.latency_ms + 'ms' : '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="success" :label="$t('traces.status')" width="70" align="center">
        <template #default="{ row }">
          <el-tag :type="row.success ? 'success' : 'danger'" size="small">
            {{ row.success ? '✓' : '✗' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="$t('traces.tags')" width="140" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.circuit_breaker_hit" type="warning" size="small" style="margin:2px">{{ $t('traces.circuitBreaker') }}</el-tag>
          <el-tag v-if="row.rate_limit_hit" type="danger" size="small" style="margin:2px">{{ $t('traces.rateLimit') }}</el-tag>
          <el-tag v-if="row.is_stream" type="info" size="small" style="margin:2px">{{ $t('traces.streaming') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="$t('traces.actions')" width="80" align="center">
        <template #default="{ row }">
          <el-button type="primary" link @click="showDetail(row)">{{ $t('traces.detail') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <div style="margin-top:16px;display:flex;justify-content:center">
      <el-pagination
        v-model:current-page="currentPage"
        :page-size="20"
        :total="totalCount"
        layout="prev, pager, next"
        @current-change="loadRequests"
      />
    </div>

    <!-- 详情弹窗: 链路信息 + 输入输出内容 -->
    <el-dialog v-model="detailVisible" :title="$t('requests.detailTitle')" width="720px"
      append-to-body class="gw-detail-dialog">
      <div v-if="detailRow" class="trace-detail" v-loading="detailLoading">
        <div class="detail-section">
          <h4>{{ $t('traces.requestInfo') }}</h4>
          <div class="detail-grid">
            <div><span class="detail-key">{{ $t('traces.reqId') }}:</span> <code>{{ detailRow.request_id || '-' }}</code></div>
            <div><span class="detail-key">{{ $t('traces.time') }}:</span> {{ formatTime(detailRow.created_at) }}</div>
            <div><span class="detail-key">{{ $t('traces.originalModel') }}:</span> {{ detailRow.original_model || '-' }}</div>
            <div><span class="detail-key">{{ $t('traces.resolvedTarget') }}:</span> {{ detailRow.resolved_model || detailRow.model_name || '-' }}</div>
            <div><span class="detail-key">{{ $t('traces.strategy') }}:</span> {{ detailRow.selection_strategy || '-' }}</div>
            <div><span class="detail-key">{{ $t('traces.selectionReason') }}:</span> {{ detailRow.selection_reason || '-' }}</div>
            <div><span class="detail-key">Token:</span> {{ detailRow.prompt_tokens || 0 }} → {{ detailRow.completion_tokens || 0 }} ({{ $t('traces.totalTokens', { total: detailRow.total_tokens || 0 }) }})</div>
            <div v-if="detailRow.total_chars"><span class="detail-key">{{ $t('requests.totalChars') }}:</span> {{ Number(detailRow.total_chars).toLocaleString() }}</div>
          </div>
        </div>

        <div class="detail-section">
          <h4>{{ $t('traces.executionResult') }}</h4>
          <div class="detail-grid">
            <div><span class="detail-key">{{ $t('traces.provider') }}:</span> {{ detailRow.provider_name || '-' }}</div>
            <div><span class="detail-key">{{ $t('traces.model') }}:</span> {{ detailRow.model_name || '-' }}</div>
            <div><span class="detail-key">{{ $t('traces.latency') }}:</span> <span :class="latencyClass(detailRow.latency_ms)">{{ detailRow.latency_ms ? detailRow.latency_ms + 'ms' : '-' }}</span></div>
            <div><span class="detail-key">{{ $t('traces.status') }}:</span>
              <el-tag :type="detailRow.success ? 'success' : 'danger'" size="small">{{ detailRow.success ? $t('common.success') : $t('common.failed') }}</el-tag>
            </div>
            <div v-if="detailRow.error_msg" style="grid-column:1/-1"><span class="detail-key">{{ $t('traces.error') }}:</span> <span style="color:var(--gw-color-danger)">{{ detailRow.error_msg }}</span></div>
          </div>
        </div>

        <div class="detail-section" v-if="detailRow.circuit_breaker_hit || detailRow.rate_limit_hit">
          <h4>{{ $t('traces.circuitLimit') }}</h4>
          <div class="detail-grid">
            <div v-if="detailRow.circuit_breaker_hit"><el-tag type="warning" size="small">{{ $t('traces.circuitBreakerHit') }}</el-tag></div>
            <div v-if="detailRow.rate_limit_hit"><el-tag type="danger" size="small">{{ $t('traces.rateLimitHitCooldown', { seconds: detailRow.cooldown_seconds }) }}</el-tag></div>
          </div>
        </div>

        <div class="detail-section" v-if="detailRow.stages_detail && detailRow.stages_detail.length">
          <h4>{{ $t('traces.orchestrationStages') }}</h4>
          <el-table :data="detailRow.stages_detail" size="small" stripe>
            <el-table-column prop="stage" :label="$t('traces.stage')" width="70" />
            <el-table-column prop="provider" :label="$t('traces.provider')" width="120" />
            <el-table-column prop="model" :label="$t('traces.model')" min-width="150" />
            <el-table-column prop="latency_ms" :label="$t('traces.latency')" width="80">
              <template #default="{ row }">{{ row.latency_ms }}ms</template>
            </el-table-column>
            <el-table-column prop="success" :label="$t('traces.status')" width="70" align="center">
              <template #default="{ row }">
                <el-tag :type="row.success ? 'success' : 'danger'" size="small">{{ row.success ? '✓' : '✗' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error" :label="$t('traces.error')" min-width="120">
              <template #default="{ row }">
                <span style="color:var(--gw-text-secondary)">{{ row.error || '-' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div class="detail-section" v-if="detailRow.prompt_text !== undefined">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h4 style="margin:0">{{ $t('chats.inputWithCount', { count: formatNum(detailRow.prompt_chars || (detailRow.prompt_text || '').length) }) }}</h4>
            <el-button size="small" text @click="copyText(detailRow.prompt_text)">{{ $t('common.copy') }}</el-button>
          </div>
          <div class="text-box">{{ detailRow.prompt_text || $t('chats.emptyContent') }}</div>
        </div>

        <div class="detail-section" v-if="detailRow.completion_text !== undefined && detailRow.success">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h4 style="margin:0">{{ $t('chats.outputWithCount', { count: formatNum(detailRow.completion_chars || (detailRow.completion_text || '').length) }) }}</h4>
            <el-button size="small" text @click="copyText(detailRow.completion_text)">{{ $t('common.copy') }}</el-button>
          </div>
          <div class="text-box">{{ detailRow.completion_text || $t('chats.emptyContent') }}</div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search, Delete } from '@element-plus/icons-vue'
import api from '../api'

const { t } = useI18n()

const rows = ref([])
const stats = ref(null)
const loading = ref(false)
const cleanupLoading = ref(false)
const totalCount = ref(0)
const currentPage = ref(1)
const filterModel = ref('')
const filterSuccess = ref(null)
const filterPipeline = ref(false)
const filterRequestType = ref('')
const detailVisible = ref(false)
const detailRow = ref(null)
const detailLoading = ref(false)

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', { hour12: false })
}

function formatNum(n) {
  return Number(n || 0).toLocaleString()
}

function latencyClass(ms) {
  if (!ms) return ''
  if (ms < 1000) return 'text-green'
  if (ms < 3000) return 'text-yellow'
  return 'text-red'
}

function onFilterChange() {
  currentPage.value = 1
  loadRequests()
}

async function loadStats() {
  try {
    const resp = await api.get('/api/requests/stats', { params: { days: 7 } })
    stats.value = resp.data
  } catch (e) { /* 统计失败不阻塞列表 */ }
}

async function loadRequests() {
  loading.value = true
  try {
    const params = {
      limit: 20,
      offset: (currentPage.value - 1) * 20,
    }
    if (filterModel.value) params.model = filterModel.value
    if (filterSuccess.value !== null && filterSuccess.value !== '') params.success = filterSuccess.value
    if (filterRequestType.value) params.request_type = filterRequestType.value
    if (filterPipeline.value) params.pipeline_only = true
    const resp = await api.get('/api/requests', { params })
    rows.value = (resp.data.data || []).map(r => ({ ...r, rowKey: `${r.trace_id || 't'}-${r.chat_id || 'c'}` }))
    totalCount.value = resp.data.total || 0
  } catch (e) {
    console.error('loadRequests failed', e)
  } finally {
    loading.value = false
  }
}

/** 详情懒加载: 列表行只有摘要, 打开时按需拉链路明细与聊天全文 */
async function showDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detailRow.value = { ...row }
  const jobs = []
  if (row.has_trace && row.trace_id) {
    jobs.push(api.get(`/api/traces/${row.trace_id}`).then(resp => {
      Object.assign(detailRow.value, resp.data)
    }).catch(() => {}))
  }
  if (row.has_chat && row.chat_id) {
    jobs.push(api.get(`/api/chats/${row.chat_id}`).then(resp => {
      Object.assign(detailRow.value, resp.data)
    }).catch(() => {}))
  }
  await Promise.all(jobs)
  detailLoading.value = false
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text || '')
    ElMessage.success(t('common.copied') || 'Copied')
  } catch (e) {
    ElMessage.error(t('common.copyFailed') || 'Copy failed')
  }
}

async function cleanupAll() {
  try {
    const { value } = await ElMessageBox.prompt(
      t('requests.cleanupPrompt'), t('requests.cleanup'), {
        confirmButtonText: t('common.confirm') || 'OK',
        cancelButtonText: t('common.cancel') || 'Cancel',
        inputValue: '30',
        inputPattern: /^\d+$/,
        inputErrorMessage: t('requests.cleanupInvalid'),
      })
    const days = Math.max(1, Math.min(parseInt(value || '30', 10), 365))
    cleanupLoading.value = true
    const [tr, cr] = await Promise.all([
      api.delete('/api/traces/cleanup', { params: { days } }),
      api.delete('/api/chats/cleanup', { params: { days } }),
    ])
    ElMessage.success(t('requests.cleanupDone', {
      traces: tr.data.deleted || 0, chats: cr.data.deleted || 0,
    }))
    currentPage.value = 1
    await Promise.all([loadRequests(), loadStats()])
  } catch (e) {
    if (e !== 'cancel' && e?.toString() !== 'cancel') {
      // 用户取消不提示; 其余失败静默(删除类操作前端已二次确认)
    }
  } finally {
    cleanupLoading.value = false
  }
}

onMounted(() => {
  loadRequests()
  loadStats()
})
</script>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
}
.stat-card {
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: var(--gw-radius-md);
  padding: 16px;
}
.stat-value {
  font-size: 24px;
  font-weight: 600;
}
.stat-label {
  font-size: 12px;
  color: var(--gw-text-secondary);
  margin-top: 4px;
}
.stat-sublabel {
  font-size: 11px;
  color: var(--gw-muted-foreground);
  margin-top: 2px;
}
.text-green { color: #22c55e; }
.text-yellow { color: #eab308; }
.text-red { color: #ef4444; }
.text-blue { color: var(--gw-primary); }

.trace-detail h4 {
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
code {
  background: var(--gw-surface-secondary);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}
.text-box {
  background: var(--gw-surface-secondary);
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  max-height: 260px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin-top: 8px;
}
</style>
