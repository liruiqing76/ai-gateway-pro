<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">模型别名</h2>
    <p style="color:var(--gw-text-secondary);margin-bottom:20px;font-size:14px">
      自定义模型别名映射，让请求更简洁。例如：设置 <code>gpt-4</code> → <code>high_quality</code>，之后请求 <code>model: "gpt-4"</code> 会自动路由到高质量模型组。
    </p>

    <!-- 操作栏 -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div style="display:flex;gap:8px">
        <el-button type="primary" @click="showAddDialog">
          <el-icon><Plus /></el-icon> 添加别名
        </el-button>
        <el-button @click="loadAliases" :loading="loading">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
      </div>
      <el-input
        v-model="searchText"
        placeholder="搜索别名..."
        style="width:240px"
        clearable
        @clear="searchText = ''"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
    </div>

    <!-- 别名列表 -->
    <el-table :data="filteredAliases" style="width:100%" v-loading="loading" stripe>
      <el-table-column prop="alias" label="别名" min-width="150">
        <template #default="{ row }">
          <span style="font-family:monospace;font-weight:500">{{ row.alias }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="target" label="目标模型/路由组" min-width="200">
        <template #default="{ row }">
          <el-tag type="success" size="small">{{ row.target }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="180">
        <template #default="{ row }">
          <span style="color:var(--gw-text-secondary)">{{ row.description || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="enabled" label="状态" width="100" align="center">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            @change="toggleEnabled(row)"
            :loading="row._toggling"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120" align="center">
        <template #default="{ row }">
          <el-button type="primary" link @click="showEditDialog(row)">编辑</el-button>
          <el-button type="danger" link @click="deleteAlias(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 空状态 -->
    <el-empty v-if="!loading && aliases.length === 0" description="暂无别名配置">
      <el-button type="primary" @click="showAddDialog">添加第一个别名</el-button>
    </el-empty>

    <!-- 添加/编辑弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingAlias ? '编辑别名' : '添加别名'"
      width="480px"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="100px" label-position="top">
        <el-form-item label="别名" :required="!editingAlias">
          <el-input
            v-model="form.alias"
            placeholder="例如: gpt-4, claude-3, 快速模型"
            :disabled="!!editingAlias"
            maxlength="128"
          />
          <div class="form-tip">用户请求时使用的名称</div>
        </el-form-item>
        <el-form-item label="目标模型/路由组" required>
          <el-input
            v-model="form.target"
            placeholder="例如: high_quality, NVIDIA-deepseek-v3"
            maxlength="128"
          />
          <div class="form-tip">可以是路由组名(如 high_quality)或具体模型名</div>
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="form.description"
            placeholder="可选描述"
            maxlength="256"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveAlias" :loading="saving">
          {{ editingAlias ? '保存' : '添加' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import api from '../api'

const aliases = ref([])
const loading = ref(false)
const saving = ref(false)
const searchText = ref('')
const dialogVisible = ref(false)
const editingAlias = ref(null)
const form = ref({
  alias: '',
  target: '',
  description: '',
})

const filteredAliases = computed(() => {
  if (!searchText.value) return aliases.value
  const keyword = searchText.value.toLowerCase()
  return aliases.value.filter(a =>
    a.alias.toLowerCase().includes(keyword) ||
    a.target.toLowerCase().includes(keyword) ||
    (a.description && a.description.toLowerCase().includes(keyword))
  )
})

async function loadAliases() {
  loading.value = true
  try {
    const resp = await api.get('/api/aliases')
    aliases.value = resp.data.data || []
  } catch (e) {
    ElMessage.error('加载别名失败')
  } finally {
    loading.value = false
  }
}

function showAddDialog() {
  editingAlias.value = null
  form.value = { alias: '', target: '', description: '' }
  dialogVisible.value = true
}

function showEditDialog(alias) {
  editingAlias.value = alias
  form.value = {
    alias: alias.alias,
    target: alias.target,
    description: alias.description || '',
  }
  dialogVisible.value = true
}

async function saveAlias() {
  if (!form.value.alias.trim()) {
    ElMessage.warning('请输入别名')
    return
  }
  if (!form.value.target.trim()) {
    ElMessage.warning('请输入目标模型/路由组')
    return
  }

  saving.value = true
  try {
    if (editingAlias.value) {
      await api.put(`/api/aliases/${editingAlias.value.id}`, {
        target: form.value.target.trim(),
        description: form.value.description.trim() || null,
      })
      ElMessage.success('别名已更新')
    } else {
      await api.post('/api/aliases', {
        alias: form.value.alias.trim(),
        target: form.value.target.trim(),
        description: form.value.description.trim() || null,
      })
      ElMessage.success('别名已添加')
    }
    dialogVisible.value = false
    await loadAliases()
  } catch (e) {
    const msg = e.response?.data?.detail || '操作失败'
    ElMessage.error(msg)
  } finally {
    saving.value = false
  }
}

async function deleteAlias(alias) {
  try {
    await ElMessageBox.confirm(
      `确定要删除别名 "${alias.alias}" 吗？`,
      '删除确认',
      { type: 'warning' }
    )
    await api.delete(`/api/aliases/${alias.id}`)
    ElMessage.success('别名已删除')
    await loadAliases()
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

async function toggleEnabled(alias) {
  alias._toggling = true
  try {
    await api.put(`/api/aliases/${alias.id}`, {
      enabled: alias.enabled,
    })
    ElMessage.success(alias.enabled ? '别名已启用' : '别名已禁用')
  } catch (e) {
    alias.enabled = !alias.enabled
    ElMessage.error('操作失败')
  } finally {
    alias._toggling = false
  }
}

onMounted(() => {
  loadAliases()
})
</script>

<style scoped>
.form-tip {
  font-size: 12px;
  color: var(--gw-text-secondary);
  margin-top: 4px;
}
</style>
