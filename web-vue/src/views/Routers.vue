<template>
  <div>
    <div class="page-header">
      <h2>{{ $t('routers.title') }}</h2>
      <div style="display:flex;gap:8px">
        <el-button @click="checkAll" :loading="checking" :disabled="groups.length === 0">
          {{ checking ? $t('routers.checking') : $t('routers.detectAvailability') }}
        </el-button>
        <el-button type="primary" @click="showDialog = true">{{ $t('routers.routerConfig') }}</el-button>
      </div>
    </div>

    <!-- 检测结果 -->
    <div v-if="checkResults.length > 0" class="check-banner">
      <div class="check-banner-title">{{ $t('routers.recentCheckResults') }}</div>
      <div class="check-group">
        <div v-for="r in checkResults" :key="r.group_name" class="check-group-item">
          <div class="name">{{ r.group_name }}</div>
          <div class="count" :style="{color: r.available === r.total ? 'var(--gw-green)' : r.available > 0 ? 'var(--gw-yellow)' : 'var(--gw-red)'}">
            {{ r.available }}/{{ r.total }}
          </div>
          <div class="desc">{{ r.available === r.total ? $t('routers.allAvailable') : r.available > 0 ? $t('routers.partialAvailable') : $t('routers.noneAvailable') }}</div>
        </div>
      </div>
    </div>

    <el-card shadow="hover">
      <el-table :data="groups" :empty-text="$t('routers.emptyText')" v-loading="loading">
        <el-table-column prop="name" :label="$t('routers.groupName')" width="160">
          <template #default="{row}"><span style="font-weight:600">🔀 {{ row.name }}</span></template>
        </el-table-column>
        <el-table-column :label="$t('routers.strategy')" width="100" align="center">
          <template #default="{row}">
            <el-tag :type="row.strategy === 'fastest' ? 'success' : row.strategy === 'stable' ? 'warning' : 'info'" size="small">
              {{ STRATEGY_LABELS[row.strategy] || row.strategy }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('routers.status')" width="100" align="center">
          <template #default="{row}">
            <el-tag v-if="row.available === undefined" type="info" size="small">{{ $t('routers.notChecked') }}</el-tag>
            <el-tag v-else :type="row.available === row.total ? 'success' : row.available > 0 ? 'warning' : 'danger'" size="small">
              {{ row.available }}/{{ row.total }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('routers.modelCount')" width="70" align="center">
          <template #default="{row}">{{ (row.models || []).length }}</template>
        </el-table-column>
        <el-table-column :label="$t('routers.modelList')" min-width="260">
          <template #default="{row}">
            <el-tag v-for="m in (row.models || []).slice(0,6)" :key="m" size="small" style="margin:2px 4px 2px 0">{{ m }}</el-tag>
            <span v-if="(row.models || []).length > 6" style="color:var(--gw-text-tertiary);font-size:12px">+{{ (row.models || []).length - 6 }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('routers.actions')" width="150" align="center">
          <template #default="{row}">
            <el-button size="small" text type="primary" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button size="small" text type="danger" @click="del(row.name)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 路由配置弹窗 -->
    <el-dialog v-model="showDialog" :title="$t('routers.routerConfig')" width="720px" top="5vh" destroy-on-close @open="loadData">
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
        <el-input v-model="newName" :placeholder="$t('routers.routeNamePlaceholder')" clearable style="flex:1;min-width:200px" />
        <el-button @click="autoClassify" :disabled="allOptions.length === 0">{{ $t('routers.autoClassify') }}</el-button>
        <el-button @click="addGroup" :disabled="!newName.trim()">{{ $t('routers.addGroup') }}</el-button>
      </div>

      <div v-if="loadingData" v-loading="loadingData" style="min-height:200px" />
      <div v-else-if="Object.keys(routersData).length === 0" style="text-align:center;padding:40px;color:var(--gw-text-tertiary)">{{ $t('routers.emptyAddHint') }}</div>

      <div v-else style="max-height:480px;overflow-y:auto">
        <div v-for="(entry, name) in routersData" :key="name" class="router-group">
          <div class="router-group-header">
            <div>
              <span class="name">🔀 {{ name }}</span>
              <span class="hint">{{ $t('routers.clientModelHint') }}</span>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <el-select v-model="routerStrategies[name]" size="small" style="width:120px" @change="onStrategyChange(name)">
                <el-option :label="$t('routers.strategyQuality')" value="quality" />
                <el-option :label="$t('routers.strategyFastest')" value="fastest" />
                <el-option :label="$t('routers.strategyStable')" value="stable" />
                <el-option :label="$t('routers.strategyFailover')" value="failover" />
              </el-select>
              <el-button size="small" text type="danger" @click="removeGroup(name)">{{ $t('common.delete') }}</el-button>
            </div>
          </div>
          <div v-for="opt in allOptions" :key="opt.key" class="model-option">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;flex:1">
              <el-checkbox :model-value="selectedMap[name]?.has(opt.key) || false" @change="toggleModel(name, opt.key, $event)" />
              <span :class="'status-dot ' + (opt.available === true ? 'ok' : opt.available === false ? 'fail' : 'unknown')"></span>
              <el-tag size="small" type="info">{{ opt.provider }}</el-tag>
              <span>{{ opt.model }}</span>
              <span v-if="opt.latency_ms" style="font-size:11px;color:var(--gw-text-tertiary);margin-left:auto">{{ opt.latency_ms }}ms</span>
            </label>
          </div>
        </div>
      </div>

      <template #footer>
        <el-button @click="showDialog = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" @click="saveAll" :loading="saving">{{ $t('routers.saveAll') }}</el-button>
      </template>
    </el-dialog>

    <!-- 单个路由组编辑弹窗 -->
    <el-dialog v-model="showEdit" :title="$t('routers.editGroupTitle', { name: editName })" width="640px" top="8vh" destroy-on-close @open="loadEditOptions">
      <div v-if="editLoading" v-loading="editLoading" style="min-height:150px" />
      <div v-else style="max-height:480px;overflow-y:auto">
        <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <span style="font-weight:600">🔀 {{ editName }}</span>
          <el-select v-model="editStrategy" size="small" style="width:130px">
            <el-option :label="$t('routers.strategyQuality')" value="quality" />
            <el-option :label="$t('routers.strategyFastest')" value="fastest" />
            <el-option :label="$t('routers.strategyStable')" value="stable" />
            <el-option :label="$t('routers.strategyFailover')" value="failover" />
          </el-select>
          <span style="font-size:12px;color:var(--gw-text-tertiary)">{{ $t('routers.selectedCount', { selected: editSelected.size, total: editOptions.length }) }}</span>
        </div>
        <div v-if="editOptions.length === 0" style="text-align:center;padding:30px;color:var(--gw-text-tertiary)">{{ $t('routers.noAvailableModels') }}</div>
        <div v-for="opt in editOptions" :key="opt.key" class="model-option">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;flex:1">
            <el-checkbox :model-value="editSelected.has(opt.key)" @change="toggleEdit(opt.key, $event)" />
            <el-tag size="small" type="info">{{ opt.provider }}</el-tag>
            <span>{{ opt.model }}</span>
          </label>
        </div>
      </div>
      <template #footer>
        <el-button @click="showEdit = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" @click="saveEdit" :loading="editSaving">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const { t } = useI18n()

const STRATEGY_LABELS = { quality: t('routers.strategyQuality'), fastest: t('routers.strategyFastest'), stable: t('routers.strategyStable'), failover: t('routers.strategyFailover') }

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

// 单个路由组编辑
const showEdit = ref(false)
const editLoading = ref(false)
const editSaving = ref(false)
const editName = ref('')
const editStrategy = ref('quality')
const editOptions = ref([])
const editSelected = ref(new Set())

function openEdit(row) {
  editName.value = row.name
  editStrategy.value = row.strategy || 'quality'
  showEdit.value = true
}

async function loadEditOptions() {
  editLoading.value = true
  try {
    const [r, r2] = await Promise.all([api.get('/api/providers'), api.get('/api/routers')])
    const list = r.data || []
    editOptions.value = []
    list.forEach(p => {
      ;(p.models || []).forEach(m => {
        editOptions.value.push({ provider: p.name, model: m, key: `${p.name}-${m}` })
      })
    })
    const data = r2.data?.data || r2.data || {}
    const entry = data[editName.value] || {}
    const models = entry?.models || entry || []
    editStrategy.value = entry?.strategy || editStrategy.value
    editSelected.value = new Set(models)
  } catch (e) {
    ElMessage.error(t('routers.loadFailed') + e)
  } finally {
    editLoading.value = false
  }
}

function toggleEdit(key, checked) {
  const s = new Set(editSelected.value)
  if (checked) s.add(key)
  else s.delete(key)
  editSelected.value = s
}

async function saveEdit() {
  editSaving.value = true
  try {
    await api.post('/api/routers', {
      name: editName.value,
      models: [...editSelected.value],
      strategy: editStrategy.value,
    })
    ElMessage.success(t('routers.groupSaved'))
    showEdit.value = false
    loadGroups()
  } catch (e) {
    ElMessage.error(t('routers.saveFailed') + e)
  } finally {
    editSaving.value = false
  }
}

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
    if (e.message !== 'Unauthorized') ElMessage.error(t('routers.loadGroupsFailed'))
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
    ElMessage.success(t('routers.checkCompleted'))
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('routers.checkFailed'))
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
        // key 用「渠道名-模型名」: 与路由组存储/路由匹配的规范一致, 才能正确回显勾选
        allOptions.value.push({ provider: p.name, model: m, key: `${p.name}-${m}`, available: undefined, latency_ms: undefined })
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
    ElMessage.error(t('routers.loadDataFailed') + e)
  } finally {
    loadingData.value = false
  }
}

function addGroup() {
  const name = newName.value.trim()
  if (!name) return
  if (routersData[name]) {
    ElMessage.warning(t('routers.routeNameExists'))
    return
  }
  routersData[name] = []
  routerStrategies[name] = 'quality'
  selectedMap[name] = new Set()
  newName.value = ''
}

function removeGroup(name) {
  ElMessageBox.confirm(t('routers.confirmDeleteGroup', { name }), t('routers.promptTitle'), { type: 'warning' }).then(() => {
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
      groups.vision.push(opt.key)
    } else if (m.includes('8b') || m.includes('7b') || m.includes('3b') || m.includes('flash') || m.includes('lite')) {
      groups.quick.push(opt.key)
    } else {
      groups.high_quality.push(opt.key)
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
  ElMessage.success(t('routers.autoClassifyDone'))
}

async function saveAll() {
  saving.value = true
  try {
    for (const [name, models] of Object.entries(routersData)) {
      await api.post('/api/routers', { name, models, strategy: routerStrategies[name] || 'quality' })
    }
    ElMessage.success(t('routers.groupSaved'))
    showDialog.value = false
    loadGroups()
  } catch (e) {
    ElMessage.error(t('routers.saveFailed') + e)
  } finally {
    saving.value = false
  }
}

function del(name) {
  ElMessageBox.confirm(t('routers.confirmDeleteGroup', { name }), t('routers.promptTitle'), { type: 'warning' }).then(async () => {
    try {
      await api.delete(`/api/routers/${encodeURIComponent(name)}`)
      ElMessage.success(t('routers.deleted'))
      loadGroups()
    } catch (e) {
      ElMessage.error(t('routers.deleteFailed') + e)
    }
  }).catch(() => {})
}

onMounted(loadGroups)
</script>