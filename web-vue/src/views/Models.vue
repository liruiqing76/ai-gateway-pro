<template>
  <div>
    <div class="page-header">
      <h2>{{ $t('models.title') }}</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <el-radio-group v-model="viewMode" size="small" @change="loadData">
          <el-radio-button value="models">{{ $t('models.byModelName') }}</el-radio-button>
          <el-radio-button value="providers">{{ $t('models.byChannel') }}</el-radio-button>
          <el-radio-button value="routers">{{ $t('models.byRouterGroup') }}</el-radio-button>
        </el-radio-group>
        <el-button @click="runSpeedTest" :loading="testing" type="primary">
          {{ testing ? $t('models.speedTesting') : $t('models.routerGroupSpeedTest') }}
        </el-button>
      </div>
    </div>

    <!-- 路由组测速结果 -->
    <div v-if="groupSpeeds && Object.keys(groupSpeeds).length > 0" class="speed-banner">
      <div class="speed-banner-title">
        {{ $t('models.recentSpeedTest') }}
        <span v-if="lastCheckTime" class="last-check-time">{{ lastCheckTime }}</span>
        <span class="auto-refresh-hint">{{ $t('models.autoRefreshHint') }}</span>
      </div>
      <div class="speed-banner-grid">
        <div v-for="(info, name) in groupSpeeds" :key="name" class="speed-banner-item">
          <span class="name">🔀 {{ name }}</span>
          <el-tag size="small" :type="info.available ? 'success' : 'danger'">
            {{ info.available ? $t('models.available') : $t('models.unavailable') }}
          </el-tag>
          <span class="count" :class="speedColorClass(info)">
            {{ info.latency_ms ? info.latency_ms + 'ms' : '—' }}
          </span>
          <span v-if="info.selected_model" class="selected-model">{{ info.selected_model }}</span>
        </div>
      </div>
    </div>

    <!-- 按模型名分组: 同一模型名下展示所有提供它的渠道与实时状态 -->
    <template v-if="viewMode === 'models'">
      <div class="catalog-toolbar">
        <el-input v-model="catalogSearch" :placeholder="$t('models.catalogSearchPlaceholder')" clearable style="width:280px" />
        <span class="catalog-hint">{{ $t('models.catalogHint') }}</span>
      </div>
      <div v-if="loading" v-loading="loading" style="min-height:200px" />
      <div v-else-if="filteredCatalog.length === 0" style="text-align:center;padding:60px;color:var(--gw-text-tertiary)">
        {{ $t('models.noProviders') }}
      </div>
      <div v-else class="catalog-list">
        <el-card v-for="m in filteredCatalog" :key="m.model" shadow="hover" class="catalog-card">
          <template #header>
            <div class="card-header">
              <div class="card-header-left">
                <span class="model-code catalog-model-name">{{ m.model }}</span>
                <el-tag size="small" :type="m.usable_count > 0 ? 'success' : 'danger'" style="margin-left:8px">
                  {{ m.usable_count }}/{{ m.total_count }}
                </el-tag>
              </div>
              <div class="card-header-right">
                <span class="model-count">
                  {{ $t('models.modelCount', { count: m.total_count }) }}
                  <template v-if="m.min_rate === 0">· {{ $t('models.catalogHasFree') }}</template>
                </span>
              </div>
            </div>
          </template>
          <el-table :data="m.providers" :empty-text="$t('models.noModels')" stripe size="small" style="width:100%">
            <el-table-column :label="$t('models.catalogProvider')" min-width="150">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ row.provider }}</el-tag>
                <el-tag v-if="row.free" size="small" type="success" effect="plain" style="margin-left:4px">
                  {{ $t('models.catalogFree') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="$t('models.catalogStatus')" min-width="160">
              <template #default="{ row }">
                <el-tooltip v-if="row.downed" :content="row.downed.reason" placement="top">
                  <el-tag size="small" type="danger" effect="dark">
                    {{ $t('models.catalogStatusDowned', { at: row.downed.recovers_at }) }}
                  </el-tag>
                </el-tooltip>
                <el-tooltip v-else-if="row.circuit_open" :content="row.lane_resume?.reason || ''" placement="top">
                  <el-tag size="small" type="warning" effect="dark">
                    {{ $t('models.catalogStatusCooldown', { at: row.cooldown_until }) }}
                  </el-tag>
                </el-tooltip>
                <el-tag v-else size="small" type="success">{{ $t('models.catalogStatusAvailable') }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column min-width="150">
              <template #header>
                <span class="rate-header">
                  {{ $t('models.catalogRate') }}
                  <el-tooltip :content="$t('models.catalogRateTip')" placement="top">
                    <el-icon style="vertical-align:middle"><QuestionFilled /></el-icon>
                  </el-tooltip>
                </span>
              </template>
              <template #default="{ row }">
                <el-input-number
                  :model-value="row.rate"
                  :min="0" :max="10000" :step="0.5" :controls="false"
                  size="small" style="width:90px"
                  @change="val => saveRate(row, val)"
                />
                <el-tag v-if="row.rate_source === 'auto'" size="small" type="info" effect="plain"
                        style="margin-left:4px" :title="$t('models.rateAutoTip')">
                  {{ $t('models.rateAuto') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="$t('models.catalogQuality')" width="90" align="center">
              <template #default="{ row }">
                <span class="count" :class="scoreClass(row.quality_score)">{{ row.quality_score.toFixed(2) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="$t('models.catalogLatency')" width="100" align="center">
              <template #default="{ row }">
                <span class="count" :class="latencyClass(row.avg_latency_ms)">
                  {{ row.avg_latency_ms ? Math.round(row.avg_latency_ms) + 'ms' : '—' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column :label="$t('models.catalogToday')" width="130" align="center">
              <template #default="{ row }">
                <span v-if="row.today_calls > 0">
                  {{ row.today_calls }} · {{ formatTokens(row.today_tokens) }}
                </span>
                <span v-else style="color:#aeaeb2">—</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>
    </template>

    <!-- 按渠道分组 -->
    <template v-if="viewMode === 'providers'">
      <div v-if="loading" v-loading="loading" style="min-height:200px" />
      <div v-else-if="providers.length === 0" style="text-align:center;padding:60px;color:var(--gw-text-tertiary)">
        {{ $t('models.noProviders') }}
      </div>
      <div v-else class="provider-list">
        <el-card v-for="p in providers" :key="p.id" shadow="hover" class="provider-card">
          <template #header>
            <div class="card-header">
              <div class="card-header-left">
                <span class="provider-name">📡 {{ p.name }}</span>
                <el-tag size="small" :type="p.enabled ? 'success' : 'info'" style="margin-left:8px">
                  {{ p.enabled ? $t('common.enabled') : $t('common.disabled') }}
                </el-tag>
              </div>
              <div class="card-header-right">
                <span class="model-count">{{ $t('models.modelCount', { count: (p.models || []).length }) }}</span>
              </div>
            </div>
          </template>
          <el-table :data="p.models" :empty-text="$t('models.noModels')" stripe size="small" style="width:100%">
            <el-table-column :label="$t('models.modelId')" min-width="240">
              <template #default="{ row }">
                <span class="model-code">{{ row }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="$t('models.context')" width="120" align="center">
              <template #default="{ row }">
                {{ (contextLengths[row] || 32768).toLocaleString() }}
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>
    </template>

    <!-- 按路由组分组 -->
    <template v-if="viewMode === 'routers'">
      <div v-if="loading" v-loading="loading" style="min-height:200px" />
      <div v-else-if="routerGroups.length === 0" style="text-align:center;padding:60px;color:var(--gw-text-tertiary)">
        {{ $t('models.noRouterGroups') }}
      </div>
      <div v-else class="router-list">
        <el-card v-for="g in routerGroups" :key="g.name" shadow="hover" class="router-card">
          <template #header>
            <div class="card-header">
              <div class="card-header-left">
                <span class="router-name">🔀 {{ g.name }}</span>
                <el-tag size="small" :type="g.strategy === 'fastest' ? 'success' : g.strategy === 'stable' ? 'warning' : 'info'" style="margin-left:8px">
                  {{ $t('models.strategy.' + g.strategy) || g.strategy }}
                </el-tag>
                <!-- 测速结果 -->
                <span v-if="groupSpeeds[g.name]" class="group-speed-badge">
                  <el-tag v-if="groupSpeeds[g.name].available" size="small" type="success">
                    ✓ {{ groupSpeeds[g.name].latency_ms }}ms
                  </el-tag>
                  <el-tag v-else size="small" type="danger">
                    {{ $t('models.unavailable') }}
                  </el-tag>
                  <span v-if="groupSpeeds[g.name].selected_model" class="selected-model-inline">
                    → {{ groupSpeeds[g.name].selected_model }}
                  </span>
                </span>
              </div>
              <div class="card-header-right">
                <span class="model-count">{{ $t('models.modelCount', { count: (g.models || []).length }) }}</span>
              </div>
            </div>
          </template>
          <el-table :data="g.models" :empty-text="$t('models.noModels')" stripe size="small" style="width:100%">
            <el-table-column :label="$t('models.modelId')" min-width="320">
              <template #default="{ row }">
                <span class="model-code">{{ row }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import api from '../api'

const { t } = useI18n()

const viewMode = ref('models')
const loading = ref(false)
const testing = ref(false)
const providers = ref([])
const routerGroups = ref([])
const contextLengths = reactive({})

// 按模型名目录: [{ model, providers: [...], usable_count, total_count, min_rate }]
const catalog = ref([])
const catalogSearch = ref('')
const filteredCatalog = computed(() => {
  const q = catalogSearch.value.trim().toLowerCase()
  if (!q) return catalog.value
  return catalog.value.filter(m =>
    m.model.toLowerCase().includes(q) ||
    (m.providers || []).some(e => e.provider.toLowerCase().includes(q)))
})

// 路由组测速: { high_quality: { available, latency_ms, error, selected_model, strategy_label } }
const groupSpeeds = ref(null)
const lastCheckTime = ref('')
let autoRefreshTimer = null

function speedColorClass(info) {
  if (!info || !info.available) return 'red'
  if (!info.latency_ms) return ''
  if (info.latency_ms < 2000) return 'green'
  if (info.latency_ms < 10000) return 'yellow'
  return 'red'
}

function formatTime(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  } catch { return iso }
}

function latencyClass(ms) {
  if (!ms) return ''
  if (ms < 2000) return 'green'
  if (ms < 10000) return 'yellow'
  return 'red'
}

function scoreClass(score) {
  if (score >= 0.5) return 'green'
  if (score > 0) return 'yellow'
  return score < 0 ? 'red' : ''
}

function formatTokens(n) {
  if (!n) return '0'
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}

async function saveRate(row, val) {
  if (val == null) return
  try {
    await api.put('/api/models/rate', {
      provider: row.provider, model: row.model, rate: val,
    })
    row.rate = val
    ElMessage.success(t('models.catalogRateSaved'))
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('models.catalogRateFailed'))
  }
}

async function loadData() {
  loading.value = true
  try {
    if (viewMode.value === 'models') {
      const r = await api.get('/api/models/catalog')
      catalog.value = r.data?.models || []
      for (const m of catalog.value) {
        for (const e of (m.providers || [])) e.model = m.model
      }
      return
    } else if (viewMode.value === 'providers') {
      const r = await api.get('/api/providers')
      providers.value = r.data || []
      // 上下文长度由后端 /api/providers 统一返回, 不再需要前端硬编码表
      for (const p of providers.value) {
        const cl = p.context_lengths || {}
        for (const [m, len] of Object.entries(cl)) {
          if (!contextLengths[m]) contextLengths[m] = len
        }
      }
    } else {
      const r = await api.get('/api/routers')
      const data = r.data?.data || r.data || {}
      routerGroups.value = Object.entries(data).map(([name, entry]) => {
        const models = entry?.models || entry || []
        const strategy = entry?.strategy || 'quality'
        return { name, models, strategy }
      })
    }
    await loadSpeeds()
  } catch (e) {
    ElMessage.error(t('models.loadFailed', { error: (e.message || e) }))
  } finally {
    loading.value = false
  }
}

async function loadSpeeds() {
  try {
    const r = await api.get('/api/providers/model-speeds')
    const data = r.data || {}
    const speeds = {}
    if (data.speeds) {
      Object.entries(data.speeds).forEach(([key, val]) => {
        if (key.startsWith('router-group/')) {
          const groupName = key.replace('router-group/', '')
          speeds[groupName] = val
        }
      })
    }
    groupSpeeds.value = speeds
    lastCheckTime.value = formatTime(data.last_check)
  } catch {
    // 静默
  }
}

async function runSpeedTest() {
  testing.value = true
  groupSpeeds.value = null
  try {
    const r = await api.post('/api/providers/check-all-models')
    const data = r.data?.groups || {}
    const speeds = {}
    Object.entries(data).forEach(([name, info]) => {
      speeds[name] = {
        available: info.available,
        latency_ms: info.latency_ms,
        error: info.error,
        selected_model: info.selected_model,
        strategy_label: info.strategy_label,
      }
    })
    groupSpeeds.value = speeds
    lastCheckTime.value = formatTime(new Date().toISOString())
    ElMessage.success(t('models.speedTestDone'))
  } catch (e) {
    ElMessage.error(t('models.speedTestFailed', { error: (e.response?.data?.detail || e.message) }))
  } finally {
    testing.value = false
  }
}

function getContextLength(modelName) {
  // 旧硬编码表已废: 上下文长度由后端 CONTEXT_LIMITS 统一来源, 通过 /api/providers 返回
  return contextLengths[modelName] || 32768
}

onMounted(async () => {
  await loadData()
  autoRefreshTimer = setInterval(() => loadSpeeds(), 30000)
})

onUnmounted(() => {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer)
})
</script>

<style scoped>
.provider-list, .router-list {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.provider-card, .router-card {
  border-radius: 10px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.card-header-left {
  display: flex;
  align-items: center;
  gap: 4px;
}
.card-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.provider-name, .router-name {
  font-size: 15px;
  font-weight: 700;
}
.model-count {
  font-size: 12px;
  color: #86868b;
}
.model-code {
  font-family: ui-monospace, 'SF Mono', Consolas, monospace;
  font-size: 13px;
  background: #f5f5f7;
  padding: 2px 8px;
  border-radius: 4px;
}
.group-speed-badge {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: 4px;
}
.selected-model-inline {
  font-size: 11px;
  color: #86868b;
}
.speed-banner {
  margin-bottom: 20px;
  padding: 16px 20px;
  background: #fff;
  border: 1px solid #e5e5ea;
  border-radius: 10px;
}
.speed-banner-title {
  font-size: 12px;
  font-weight: 600;
  color: #86868b;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.last-check-time {
  color: #1d1d1f;
  text-transform: none;
  letter-spacing: 0;
}
.auto-refresh-hint {
  color: #aeaeb2;
  text-transform: none;
  letter-spacing: 0;
  font-weight: 400;
}
.speed-banner-grid {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.speed-banner-item {
  padding: 10px 16px;
  border-radius: 8px;
  background: #f5f5f7;
  min-width: 200px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.speed-banner-item .name {
  font-size: 13px;
  font-weight: 600;
}
.speed-banner-item .count {
  font-size: 16px;
  font-weight: 700;
}
.speed-banner-item .count.green { color: #34c759; }
.speed-banner-item .count.yellow { color: #ff9f0a; }
.speed-banner-item .count.red { color: #ff3b30; }
.speed-banner-item .selected-model {
  font-size: 11px;
  color: #86868b;
}
.catalog-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.catalog-hint {
  font-size: 12px;
  color: #86868b;
}
.catalog-list {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.catalog-card {
  border-radius: 10px;
}
.catalog-model-name {
  font-size: 14px;
  font-weight: 600;
}
.rate-header {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.count.green { color: #34c759; }
.count.yellow { color: #ff9f0a; }
.count.red { color: #ff3b30; }
</style>