<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">聊天记录</h2>
    <p style="color:var(--gw-text-secondary);margin-bottom:20px;font-size:14px">
      记录每次请求的完整输入与输出全文，以及字符长度统计。可点击「详情」查看一对一对话原文。
    </p>

    <!-- 统计卡片 -->
    <div class="stats-grid" v-if="stats">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total_chats }}</div>
        <div class="stat-label">总对话 ({{ stats.period_days }}天)</div>
      </div>
      <div class="stat-card">
        <div class="stat-value" :class="stats.success_rate >= 95 ? 'text-green' : stats.success_rate >= 80 ? 'text-yellow' : 'text-red'">
          {{ stats.success_rate }}%
        </div>
        <div class="stat-label">成功率</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.avg_total_chars }}</div>
        <div class="stat-label">平均总长度 (字符)</div>
      </div>
      <div class="stat-card">
        <div class="stat-value text-blue">{{ formatNum(stats.sum_total_chars) }}</div>
        <div class="stat-label">累计 IO 长度</div>
      </div>
    </div>

    <!-- 操作栏 -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin:16px 0">
      <div style="display:flex;gap:8px">
        <el-button @click="loadChats" :loading="loading">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
        <el-button @click="loadStats" :loading="statsLoading">
          <el-icon><DataAnalysis /></el-icon> 统计
        </el-button>
        <el-button type="danger" plain @click="cleanupChats">
          <el-icon><Delete /></el-icon> 清理旧记录
        </el-button>
      </div>
      <div style="display:flex;gap:8px">
        <el-input v-model="filterModel" placeholder="按模型筛选..." style="width:200px" clearable @clear="loadChats" @keyup.enter="loadChats">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="filterSuccess" placeholder="状态" style="width:120px" clearable @change="loadChats">
          <el-option label="全部" :value="null" />
          <el-option label="成功" :value="true" />
          <el-option label="失败" :value="false" />
        </el-select>
        <el-switch v-model="filterPipeline" active-text="编排" inactive-text="全部" @change="loadChats" style="margin-left:8px" />
      </div>
    </div>

    <!-- 列表 -->
    <el-table :data="chats" style="width:100%" v-loading="loading" stripe row-key="id"
      :default-sort="{ prop: 'created_at', order: 'descending' }">
      <el-table-column prop="created_at" label="时间" width="170" sortable>
        <template #default="{ row }">
          <span style="font-size:12px;font-family:monospace">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="original_model" label="请求模型" min-width="130">
        <template #default="{ row }">
          <div>
            <span style="font-weight:500">{{ row.original_model }}</span>
            <el-tag v-if="row.is_pipeline" size="small" type="info" style="margin-left:4px">{{ row.pipeline_mode }}</el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="provider_name" label="渠道" min-width="110">
        <template #default="{ row }">
          <span v-if="row.provider_name">{{ row.provider_name }}</span>
          <span v-else style="color:var(--gw-text-secondary)">-</span>
        </template>
      </el-table-column>
      <el-table-column label="输入摘要" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">
          <span style="font-size:12px;color:var(--gw-text-secondary)">{{ row.prompt_preview || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="长度(输入/输出)" width="160" align="center">
        <template #default="{ row }">
          <span style="font-size:12px">{{ row.prompt_chars }} / {{ row.completion_chars }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="total_chars" label="总长度" width="90" sortable>
        <template #default="{ row }">
          <span style="font-size:12px;font-weight:500">{{ formatNum(row.total_chars) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="success" label="状态" width="70" align="center">
        <template #default="{ row }">
          <el-tag :type="row.success ? 'success' : 'danger'" size="small">
            {{ row.success ? '✓' : '✗' }}
          </el-tag>
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
        @current-change="loadChats"
      />
    </div>

    <!-- 详情弹窗 -->
    <el-dialog v-model="detailVisible" title="聊天记录详情" width="780px" top="6vh">
      <div v-if="detailRow" class="chat-detail">
        <div class="detail-section">
          <h4>请求信息</h4>
          <div class="detail-grid">
            <div><span class="detail-key">时间:</span> {{ formatTime(detailRow.created_at) }}</div>
            <div><span class="detail-key">请求 ID:</span> <code>{{ detailRow.request_id || '-' }}</code></div>
            <div><span class="detail-key">原始模型:</span> {{ detailRow.original_model }}</div>
            <div><span class="detail-key">实际渠道:</span> {{ detailRow.provider_name || '-' }}</div>
            <div><span class="detail-key">实际模型:</span> {{ detailRow.model_name || '-' }}</div>
            <div><span class="detail-key">流式:</span> {{ detailRow.is_stream ? '是' : '否' }}</div>
          </div>
        </div>

        <div class="detail-section">
          <h4>长度统计</h4>
          <div class="detail-grid">
            <div><span class="detail-key">输入字符:</span> {{ formatNum(detailRow.prompt_chars) }}</div>
            <div><span class="detail-key">输出字符:</span> {{ formatNum(detailRow.completion_chars) }}</div>
            <div><span class="detail-key">总字符:</span> <b>{{ formatNum(detailRow.total_chars) }}</b></div>
            <div><span class="detail-key">Token:</span> {{ detailRow.prompt_tokens || 0 }} → {{ detailRow.completion_tokens || 0 }} (共 {{ detailRow.total_tokens || 0 }})</div>
          </div>
        </div>

        <div class="detail-section">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h4 style="margin:0">输入 ({{ formatNum(detailRow.prompt_chars) }} 字符)</h4>
            <el-button size="small" text @click="copyText(detailRow.prompt_text)">复制</el-button>
          </div>
          <div class="text-box">{{ detailRow.prompt_text || '(空)' }}</div>
        </div>

        <div class="detail-section" v-if="detailRow.success">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h4 style="margin:0">输出 ({{ formatNum(detailRow.completion_chars) }} 字符)</h4>
            <el-button size="small" text @click="copyText(detailRow.completion_text)">复制</el-button>
          </div>
          <div class="text-box">{{ detailRow.completion_text || '(空)' }}</div>
        </div>

        <div class="detail-section" v-if="!detailRow.success">
          <h4>错误</h4>
          <div style="color:var(--gw-color-danger)">{{ detailRow.error_msg || '失败' }}</div>
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
    ElMessage.error('加载失败')
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
    ElMessage.error('统计加载失败')
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
    ElMessage.error('加载详情失败')
  }
}

async function cleanupChats() {
  try {
    await ElMessageBox.confirm('将删除 30 天前的聊天记录，确定？', '清理确认', { type: 'warning' })
    const resp = await api.delete('/api/chats/cleanup', { params: { days: 30 } })
    ElMessage.success(`已清理 ${resp.data.deleted} 条旧记录`)
    await loadChats()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('清理失败')
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text || '')
    ElMessage.success('已复制')
  } catch (e) {
    ElMessage.warning('复制失败')
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
