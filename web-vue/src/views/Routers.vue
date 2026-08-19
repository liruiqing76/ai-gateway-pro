<template>
  <div>
    <div class="page-header">
      <h2>路由组管理</h2>
      <div style="display:flex;gap:8px">
        <el-button @click="checkAll" :loading="checking" :disabled="groups.length === 0">
          {{ checking ? '检测中...' : '🔍 检测可用性' }}
        </el-button>
        <el-button type="primary" @click="showDialog = true">🔀 路由配置</el-button>
      </div>
    </div>

    <!-- 检测结果 -->
    <div v-if="checkResults.length > 0" class="check-banner">
      <div class="check-banner-title">最近检测结果</div>
      <div class="check-group">
        <div v-for="r in checkResults" :key="r.group_name" class="check-group-item">
          <div class="name">{{ r.group_name }}</div>
          <div class="count" :style="{color: r.available === r.total ? 'var(--gw-green)' : r.available > 0 ? 'var(--gw-yellow)' : 'var(--gw-red)'}">
            {{ r.available }}/{{ r.total }}
          </div>
          <div class="desc">{{ r.available === r.total ? '全部可用' : r.available > 0 ? '部分可用' : '全部不可用' }}</div>
        </div>
      </div>
    </div>

    <el-card shadow="hover">
      <el-table :data="groups" empty-text="暂无路由组，点击上方按钮配置" v-loading="loading">
        <el-table-column prop="name" label="组名" width="160">
          <template #default="{row}"><span style="font-weight:600">🔀 {{ row.name }}</span></template>
        </el-table-column>
        <el-table-column label="策略" width="100" align="center">
          <template #default="{row}">
            <el-tag :type="row.strategy === 'fastest' ? 'success' : row.strategy === 'stable' ? 'warning' : 'info'" size="small">
              {{ STRATEGY_LABELS[row.strategy] || row.strategy }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{row}">
            <el-tag v-if="row.available === undefined" type="info" size="small">未检测</el-tag>
            <el-tag v-else :type="row.available === row.total ? 'success' : row.available > 0 ? 'warning' : 'danger'" size="small">
              {{ row.available }}/{{ row.total }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="模型数" width="70" align="center">
          <template #default="{row}">{{ (row.models || []).length }}</template>
        </el-table-column>
        <el-table-column label="模型列表" min-width="260">
          <template #default="{row}">
            <el-tag v-for="m in (row.models || []).slice(0,6)" :key="m" size="small" style="margin:2px 4px 2px 0">{{ m }}</el-tag>
            <span v-if="(row.models || []).length > 6" style="color:var(--gw-text-tertiary);font-size:12px">+{{ (row.models || []).length - 6 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center">
          <template #default="{row}">
            <el-button size="small" text type="danger" @click="del(row.name)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 路由配置弹窗 -->
    <el-dialog v-model="showDialog" title="🔀 路由配置" width="720px" top="5vh" destroy-on-close @open="loadData">
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
        <el-input v-model="newName" placeholder="路由名称 (如 大模型 / 小模型)" clearable style="flex:1;min-width:200px" />
        <el-button @click="autoClassify" :disabled="allOptions.length === 0">📊 自动分类</el-button>
        <el-button @click="addGroup" :disabled="!newName.trim()">+ 添加</el-button>
      </div>

      <div v-if="loadingData" v-loading="loadingData" style="min-height:200px" />
      <div v-else-if="Object.keys(routersData).length === 0" style="text-align:center;padding:40px;color:var(--gw-text-tertiary)">暂无路由组，请在上方输入名称后点击"+ 添加"</div>

      <div v-else style="max-height:480px;overflow-y:auto">
        <div v-for="(entry, name) in routersData" :key="name" class="router-group">
          <div class="router-group-header">
            <div>
              <span class="name">🔀 {{ name }}</span>
              <span class="hint">(客户端 model 填此名称)</span>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <el-select v-model="routerStrategies[name]" size="small" style="width:120px" @change="onStrategyChange(name)">
                <el-option label="质量优先" value="quality" />
                <el-option label="最快优先" value="fastest" />
                <el-option label="最稳优先" value="stable" />
              </el-select>
              <el-button size="small" text type="danger" @click="removeGroup(name)">删除</el-button>
            </div>
          </div>
          <div v-for="opt in allOptions" :key="opt.model" class="model-option">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;flex:1">
              <el-checkbox :model-value="selectedMap[name]?.has(opt.model) || false" @change="toggleModel(name, opt.model, $event)" />
              <span :class="'status-dot ' + (opt.available === true ? 'ok' : opt.available === false ? 'fail' : 'unknown')"></span>
              <el-tag size="small" type="info">{{ opt.provider }}</el-tag>
              <span>{{ opt.model }}</span>
              <span v-if="opt.latency_ms" style="font-size:11px;color:var(--gw-text-tertiary);margin-left:auto">{{ opt.latency_ms }}ms</span>
            </label>
          </div>
        </div>
      </div>

      <template #footer>
        <el-button @click="showDialog = false">取消</el-button>
        <el-button type="primary" @click="saveAll" :loading="saving">保存所有路由</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const STRATEGY_LABELS = { quality: '质量优先', fastest: '最快优先', stable: '最稳优先' }

const groups = ref([])
const loading = ref(false)
const checking = ref(false)
const checkResults = ref([])

const showDialog = ref(false)
const loadingData = ref(false)
const saving = ref(false)
const allOptions = ref([])
const routersData = reactive({})
const routerStrategies = reactive({})
const selectedMap = reactive({})
const newName = ref('')

async function loadGroups() {
  loading.value = true
  try {
    const r = await api.get('/api/routers')
    const data = r.data?.data || r.data || {}
    groups.value = Object.entries(data).map(([name, entry]) => {
      const models = entry?.models || entry || []
      const strategy = entry?.strategy || 'quality'
      const existing = groups.value.find(g => g.name === name)
      return { name, models, strategy, available: existing?.available }
    })
  } catch (e) {
    if (e.message !== 'Unauthorized') ElMessage.error('加载路由组失败')
  } finally {
    loading.value = false
  }
}

async function checkAll() {
  checking.value = true
  checkResults.value = []
  try {
    const r = await api.post('/api/routers/check-availability')
    const groupsData = r.data.groups || {}
    const results = []
    Object.entries(groupsData).forEach(([name, info]) => {
      results.push({
        group_name: info.group_name,
        total: info.total,
        available: info.available,
        results: info.results,
      })
      const g = groups.value.find(gg => gg.name === name)
      if (g) g.available = info.available
    })
    checkResults.value = results
    ElMessage.success('检测完成')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '检测失败')
  } finally {
    checking.value = false
  }
}

async function loadData() {
  loadingData.value = true
  try {
    const r = await api.get('/api/providers')
    const list = r.data || []
    allOptions.value = []
    list.forEach(p => {
      ;(p.models || []).forEach(m => {
        allOptions.value.push({ provider: p.name, model: m, available: undefined, latency_ms: undefined })
      })
    })
    const r2 = await api.get('/api/routers')
    const data = r2.data?.data || r2.data || {}
    Object.keys(routersData).forEach(k => delete routersData[k])
    Object.keys(selectedMap).forEach(k => delete selectedMap[k])
    Object.keys(routerStrategies).forEach(k => delete routerStrategies[k])
    Object.entries(data).forEach(([name, entry]) => {
      const models = entry?.models || entry || []
      const strategy = entry?.strategy || 'quality'
      routersData[name] = models
      routerStrategies[name] = strategy
      selectedMap[name] = new Set(models)
    })
  } catch (e) {
    ElMessage.error('加载数据失败: ' + e)
  } finally {
    loadingData.value = false
  }
}

function addGroup() {
  const name = newName.value.trim()
  if (!name) return
  if (routersData[name]) {
    ElMessage.warning('该路由名称已存在')
    return
  }
  routersData[name] = []
  routerStrategies[name] = 'quality'
  selectedMap[name] = new Set()
  newName.value = ''
}

function removeGroup(name) {
  ElMessageBox.confirm(`确定删除路由组「${name}」？`, '提示', { type: 'warning' }).then(() => {
    delete routersData[name]
    delete routerStrategies[name]
    delete selectedMap[name]
  }).catch(() => {})
}

function onStrategyChange(name) {
  // 策略改了即时反映
}

function toggleModel(name, model, checked) {
  if (!selectedMap[name]) selectedMap[name] = new Set()
  if (checked) selectedMap[name].add(model)
  else selectedMap[name].delete(model)
  routersData[name] = [...selectedMap[name]]
}

function autoClassify() {
  const groups = { quick: [], high_quality: [], vision: [] }
  allOptions.value.forEach(opt => {
    const m = opt.model.toLowerCase()
    if (m.includes('vl') || m.includes('vision') || m.includes('qvq') || m.includes('internvl')) {
      groups.vision.push(opt.model)
    } else if (m.includes('8b') || m.includes('7b') || m.includes('3b') || m.includes('flash') || m.includes('lite')) {
      groups.quick.push(opt.model)
    } else {
      groups.high_quality.push(opt.model)
    }
  })
  Object.entries(groups).forEach(([name, models]) => {
    if (!routersData[name]) {
      routersData[name] = []
      routerStrategies[name] = 'quality'
      selectedMap[name] = new Set()
    }
    models.forEach(m => selectedMap[name].add(m))
    routersData[name] = [...selectedMap[name]]
  })
  ElMessage.success('自动分类完成，可手动调整勾选')
}

async function saveAll() {
  saving.value = true
  try {
    for (const [name, models] of Object.entries(routersData)) {
      await api.post('/api/routers', { name, models, strategy: routerStrategies[name] || 'quality' })
    }
    ElMessage.success('路由组已保存')
    showDialog.value = false
    loadGroups()
  } catch (e) {
    ElMessage.error('保存失败: ' + e)
  } finally {
    saving.value = false
  }
}

function del(name) {
  ElMessageBox.confirm(`确定删除路由组「${name}」？`, '提示', { type: 'warning' }).then(async () => {
    try {
      await api.delete(`/api/routers/${encodeURIComponent(name)}`)
      ElMessage.success('已删除')
      loadGroups()
    } catch (e) {
      ElMessage.error('删除失败: ' + e)
    }
  }).catch(() => {})
}

onMounted(loadGroups)
</script>