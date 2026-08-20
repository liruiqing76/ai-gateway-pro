<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">请求链路日志</h2>
    <p style="color:var(--gw-text-secondary);margin-bottom:20px;font-size:14px">
      记录每次请求的完整决策链路：走了哪个模型、延迟多少、为什么选它、熔断/冷却命中情况。
    </p>

    <!-- 统计卡片 -->
    <div class="stats-grid" v-if="stats">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total_requests }}</div>
        <div class="stat-label">总请求 ({{ stats.period_days }}天)</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" :class="stats.success_rate >= 95 ? 'text-green' : stats.success_rate >= 80 ? 'text-yellow' : 'text-red'">
          {{ stats.success_rate }}%
        </div>
        <div class="stat-label">成功率</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.avg_latency_ms }}ms</div>
        <div class="stat-label">平均延迟</div>
      </div>
      <div class="stat-card">
        <div class="stat-value text-orange">{{ stats.circuit_breaker_hits }}</div>
        <div class="stat-label">熔断命中</div>
      </div>
    </div>

    <!-- 操作栏 -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin:16px 0">
      <div style="display:flex;gap:8px">
        <el-button @click="loadTraces" :loading="loading">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
        <el-button @click="loadStats" :loading="statsLoading">
          <el-icon><DataAnalysis /></el-icon> 统计
        </el-button>
        <el-button type="danger" plain @click="cleanupTraces">
          <el-icon><Delete /></el-icon> 清理旧日志
        </el-button>
      </div>
      <div style="display:flex;gap:8px">
        <el-input v-model="filterModel" placeholder="按模型筛选..." style="width:200px" clearable @clear="loadTraces" @keyup.enter="loadTraces">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="filterSuccess" placeholder="状态" style="width:120px" clearable @change="loadTraces">
          <el-option label="全部" :value="null" />
          <el-option label="成功" :value="true" />
          <el-option label="失败" :value="false" />
        </el-select>
        <el-switch v-model="filterPipeline" active-text="编排" inactive-text="全部" @change="loadTraces" style="margin-left:8px" />
      </div>
    </div>

    <!-- 链路列表 -->
    <el-table :data="traces" style="width:100%" v-loading="loading" stripe row-key="id"
      :default-sort="{ prop: 'created_at', order: 'descending' }">
      <el-table-column prop="created_at" label="时间" width="170" sortable>
        <template #default="{ row }">
          <span style="font-size:12px;font-family:monospace">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="original_model" label="请求模型" min-width="140">
        <template #default="{ row }">
          <div>
            <span style="font-weight:500">{{ row.original_model }}</span>
            <el-tag v-if="row.is_pipeline" size="small" type="info" style="margin-left:4px">{{ row.pipeline_mode }}</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="provider_name" label="实际渠道" min-width="130">
        <template #default="{ row }">
          <span v-if="row.provider_name">{{ row.provider_name }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="model_name" label="实际模型" min-width="160">
        <template #default="{ row }">
          <span v-if="row.model_name" style="font-family:monospace;font-size:12px">{{ row.model_name }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="latency_ms" label="延迟" width="90" sortable>
        <template #default="{ row }">
          <span :class="latencyClass(row.latency_ms)">{{ row.latency_ms }}ms</span>
        </template>
      </el-table-column>
      <el-table-column prop="success" label="状态" width="70" align="center">
        <template #default="{ row }">
          <el-tag :type="row.success ? 'success' : 'danger'" size="small">
            {{ row.success ? '✓' : '✗' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="标记" width="120" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.circuit_breaker_hit" type="warning" size="small" style="margin:2px">熔断</el-tag>
          <el-tag v-if="row.rate_limit_hit" type="danger" size="small" style="margin:2px">限流</el-tag>
          <el-tag v-if="row.is_stream" type="info" size="small" style="margin:2px">流式</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Token" width="100">
        <template #default="{ row }">
          <span v-if="row.total_tokens" style="font-size:12px">{{ row.total_tokens.toLocaleString() }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="80" align="center">
        <template #default="{ row }">
          <el-button type="primary" link @click="showDetail(row)">详情</el-button>
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
        @current-change="loadTraces"
      />
    </div>

    <!-- 详情弹窗 -->
    <el-dialog v-model="detailVisible" title="请求链路详情" width="640px">
      <div v-if="detailRow" class="trace-detail">
        <div class="detail-section">
          <h4>请求信息</h4>
          <div class="detail-grid">
            <div><span class="detail-key">请求 ID:</span> <code>{{ detailRow.request_id }}</code></div>
            <div><span class="detail-key">时间:</span> {{ formatTime(detailRow.created_at) }}</div>
            <div><span class="detail-key">原始模型:</span> {{ detailRow.original_model }}</div>
            <div><span class="detail-key">解析目标:</span> {{ detailRow.resolved_model || '-' }}</div>
            <div><span class="detail-key">策略:</span> {{ detailRow.selection_strategy || '-' }}</div>
            <div><span class="detail-key">选择原因:</span> {{ detailRow.selection_reason || '-' }}</div>
          </div>
        </div>

        <div class="detail-section">
          <h4>执行结果</h4>
          <div class="detail-grid">
            <div><span class="detail-key">渠道:</span> {{ detailRow.provider_name || '-' }}</div>
            <div><span class="detail-key">模型:</span> {{ detailRow.model_name || '-' }}</div>
            <div><span class="detail-key">延迟:</span> <span :class="latencyClass(detailRow.latency_ms)">{{ detailRow.latency_ms }}ms</span></div>
            <div><span class="detail-key">状态:</span>
              <el-tag :type="detailRow.success ? 'success' : 'danger'" size="small">{{ detailRow.success ? '成功' : '失败' }}</el-tag>
            </div>
            <div v-if="detailRow.error_msg"><span class="detail-key">错误:</span> <span style="color:var(--gw-color-danger)">{{ detailRow.error_msg }}</span></div>
            <div><span class="detail-key">Token:</span> {{ detailRow.prompt_tokens || 0 }} → {{ detailRow.completion_tokens || 0 }} (共 {{ detailRow.total_tokens || 0 }})</div>
          </div>
        </div>

        <div class="detail-section" v-if="detailRow.circuit_breaker_hit || detailRow.rate_limit_hit">
          <h4>熔断/限流</h4>
          <div class="detail-grid">
            <div v-if="detailRow.circuit_breaker_hit"><el-tag type="warning" size="small">熔断命中</el-tag></div>
            <div v-if="detailRow.rate_limit_hit"><el-tag type="danger" size="small">限流命中, 冷却 {{ detailRow.cooldown_seconds }}s</el-tag></div>
          </div>
        </div>

        <div class="detail-section" v-if="detailRow.stages_detail && detailRow.stages_detail.length">
          <h4>编排阶段</h4>
          <el-table :data="detailRow.stages_detail" size="small" stripe>
            <el-table-column prop="stage" label="阶段" width="70" />
            <el-table-column prop="provider" label="渠道" width="120" />
            <el-table-column prop="model" label="模型" min-width="150" />
            <el-table-column prop="latency_ms" label="延迟" width="80">
              <template #default="{ row }">{{ row.latency_ms }}ms</template>
            </el-table-column>
            <el-table-column prop="success" label="状态" width="70" align="center">
              <template #default="{ row }">
                <el-tag :type="row.success ? 'success' : 'danger'" size="small">{{ row.success ? '✓' : '✗' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error" label="错误" min-width="120">
              <template #default="{ row }">
                <span style="color:var(--gw-text-secondary)">{{ row.error || '-' }}</span>
              </template>
            </el-table-column>
          </el-table>
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

const traces = ref([])
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

function latencyClass(ms) {
  if (!ms) return ''
  if (ms < 1000) return 'text-green'
  if (ms < 3000) return 'text-yellow'
  return 'text-red'
}

async function loadTraces() {
  loading.value = true
  try {
    const params = {
      limit: 50,
      offset: (currentPage.value - 1) * 50,
    }
    if (filterModel.value) params.model = filterModel.value
    if (filterSuccess.value !== null) params.success = filterSuccess.value
    if (filterPipeline.value) params.pipeline_only = true

    const resp = await api.get('/api/traces', { params })
    traces.value = resp.data.data || []
    totalCount.value = resp.data.total || 0
  } catch (e) {
    ElMessage.error('加载失败')
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  statsLoading.value = true
  try {
    const resp = await api.get('/api/traces/stats', { params: { days: 7 } })
    stats.value = resp.data
  } catch (e) {
    ElMessage.error('统计加载失败')
  } finally {
    statsLoading.value = false
  }
}

function showDetail(row) {
  detailRow.value = row
  detailVisible.value = true
}

async function cleanupTraces() {
  try {
    await ElMessageBox.confirm('将删除 30 天前的链路日志，确定？', '清理确认', { type: 'warning' })
    const resp = await api.delete('/api/traces/cleanup', { params: { days: 30 } })
    ElMessage.success(`已清理 ${resp.data.deleted} 条旧日志`)
    await loadTraces()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('清理失败')
  }
}

onMounted(() => {
  loadTraces()
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
.text-orange { color: #f97316; }

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
</style>
