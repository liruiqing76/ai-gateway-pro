<template>
  <div>
    <div class="page-header">
      <h2>{{ $t('models.title') }}</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <el-radio-group v-model="viewMode" size="small" @change="loadData">
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
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import api from '../api'

const { t } = useI18n()

const viewMode = ref('providers')
const loading = ref(false)
const testing = ref(false)
const providers = ref([])
const routerGroups = ref([])
const contextLengths = reactive({})

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

async function loadData() {
  loading.value = true
  try {
    if (viewMode.value === 'providers') {
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
</style>