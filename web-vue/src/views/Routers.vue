<template>
  <div>
    <div class="toolbar">
      <h2 style="font-size:18px;font-weight:600;margin:0">路由组管理</h2>
      <el-button type="primary" @click="showDialog = true">🔀 路由配置</el-button>
    </div>

    <el-card shadow="hover">
      <el-table :data="groups" empty-text="暂无路由组，点上方按钮配置" v-loading="loading">
        <el-table-column prop="name" label="组名" width="160">
          <template #default="{row}"><code style="font-size:13px;color:#818cf8">🔀 {{ row.name }}</code></template>
        </el-table-column>
        <el-table-column label="模型数" width="80" align="center">
          <template #default="{row}">{{ (row.models || []).length }}</template>
        </el-table-column>
        <el-table-column label="模型列表" min-width="260">
          <template #default="{row}">
            <el-tag v-for="m in (row.models || []).slice(0,6)" :key="m" size="small" style="margin:2px 4px 2px 0">{{ m }}</el-tag>
            <span v-if="(row.models || []).length > 6" style="color:#94a3b8;font-size:12px">+{{ (row.models || []).length - 6 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center">
          <template #default="{row}">
            <el-button size="small" type="danger" text @click="del(row.name)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 路由配置弹窗 -->
    <el-dialog v-model="showDialog" title="🔀 路由配置" width="600px" top="5vh" destroy-on-close @open="loadData">
      <div style="display:flex;gap:8px;margin-bottom:12px">
        <el-input v-model="newName" placeholder="路由名称 (如 quick / high_quality)" clearable />
        <el-button @click="addGroup" :disabled="!newName.trim()">+ 添加</el-button>
      </div>

      <div v-if="loadingData" v-loading="loadingData" style="min-height:200px" />

      <div v-else-if="Object.keys(routersData).length === 0" class="empty">暂无路由组，请在上方输入名称后点击"+ 添加"</div>

      <div v-else style="max-height:420px;overflow-y:auto">
        <div v-for="(models, name) in routersData" :key="name" style="border:1px solid #334155;border-radius:8px;padding:12px;margin-bottom:12px;background:#1e293b">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <div style="font-weight:600;font-size:14px">
              🔀 {{ name }}
              <span style="font-size:11px;color:#94a3b8;font-weight:400;margin-left:8px">(客户端 model 填这个名称即可命中)</span>
            </div>
            <el-button size="small" text type="danger" @click="removeGroup(name)">删除</el-button>
          </div>
          <div v-for="opt in allOptions" :key="opt.model" style="padding:4px 0">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px">
              <el-checkbox :model-value="selectedMap[name]?.has(opt.model) || false" @change="toggleModel(name, opt.model, $event)" />
              <el-tag size="small" type="info">{{ opt.provider }}</el-tag>
              <span>{{ opt.model }}</span>
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

const groups = ref([])
const loading = ref(false)
const showDialog = ref(false)
const loadingData = ref(false)
const saving = ref(false)
const allOptions = ref([])
const routersData = reactive({})      // {组名: [模型名]}
const selectedMap = reactive({})       // {组名: Set<模型名>}
const newName = ref('')

async function loadGroups() {
  loading.value = true
  try {
    const r = await api.get('/api/routers')
    const data = r.data?.data || r.data || {}
    groups.value = Object.entries(data).map(([name, models]) => ({ name, models }))
  } catch (e) {
    if (e.message !== 'Unauthorized') ElMessage.error('加载路由组失败')
  } finally {
    loading.value = false
  }
}

async function loadData() {
  loadingData.value = true
  try {
    // 1. 拉渠道模型
    const r = await api.get('/api/providers')
    const list = r.data || []
    allOptions.value = []
    list.forEach(p => {
      ;(p.models || []).forEach(m => {
        allOptions.value.push({ provider: p.name, model: m })
      })
    })
    // 2. 拉路由组
    const r2 = await api.get('/api/routers')
    const data = r2.data?.data || r2.data || {}
    Object.keys(routersData).forEach(k => delete routersData[k])
    Object.keys(selectedMap).forEach(k => delete selectedMap[k])
    Object.entries(data).forEach(([name, models]) => {
      routersData[name] = models
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
  selectedMap[name] = new Set()
  newName.value = ''
}

function removeGroup(name) {
  ElMessageBox.confirm(`确定删除路由组「${name}」？`, '提示', { type: 'warning' }).then(() => {
    delete routersData[name]
    delete selectedMap[name]
  }).catch(() => {})
}

function toggleModel(name, model, checked) {
  if (!selectedMap[name]) selectedMap[name] = new Set()
  if (checked) selectedMap[name].add(model)
  else selectedMap[name].delete(model)
  routersData[name] = [...selectedMap[name]]
}

async function saveAll() {
  saving.value = true
  try {
    // 批量保存每组
    for (const [name, models] of Object.entries(routersData)) {
      await api.post('/api/routers', { name, models })
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