<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">{{ $t('aliases.title') }}</h2>
    <p style="color:var(--gw-text-secondary);margin-bottom:20px;font-size:14px" v-html="$t('aliases.intro')"></p>

    <!-- 操作栏 -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div style="display:flex;gap:8px">
        <el-button type="primary" @click="showAddDialog">
          <el-icon><Plus /></el-icon> {{ $t('aliases.addAlias') }}
        </el-button>
        <el-button @click="loadAliases" :loading="loading">
          <el-icon><Refresh /></el-icon> {{ $t('common.refresh') }}
        </el-button>
      </div>
      <el-input
        v-model="searchText"
        :placeholder="$t('aliases.searchPlaceholder')"
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
      <el-table-column prop="alias" :label="$t('aliases.colAlias')" min-width="150">
        <template #default="{ row }">
          <span style="font-family:monospace;font-weight:500">{{ row.alias }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="target" :label="$t('aliases.colTarget')" min-width="200">
        <template #default="{ row }">
          <el-tag type="success" size="small">{{ row.target }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="description" :label="$t('aliases.colDescription')" min-width="180">
        <template #default="{ row }">
          <span style="color:var(--gw-text-secondary)">{{ row.description || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="enabled" :label="$t('aliases.colStatus')" width="100" align="center">
        <template #default="{ row }">
          <el-switch
            v-model="row.enabled"
            @change="toggleEnabled(row)"
            :loading="row._toggling"
          />
        </template>
      </el-table-column>
      <el-table-column :label="$t('aliases.colActions')" width="120" align="center">
        <template #default="{ row }">
          <el-button type="primary" link @click="showEditDialog(row)">{{ $t('common.edit') }}</el-button>
          <el-button type="danger" link @click="deleteAlias(row)">{{ $t('common.delete') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 空状态 -->
    <el-empty v-if="!loading && aliases.length === 0" :description="$t('aliases.emptyText')">
      <el-button type="primary" @click="showAddDialog">{{ $t('aliases.addFirstAlias') }}</el-button>
    </el-empty>

    <!-- 添加/编辑弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingAlias ? $t('aliases.editAlias') : $t('aliases.addAlias')"
      width="480px"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="100px" label-position="top">
        <el-form-item :label="$t('aliases.colAlias')" :required="!editingAlias">
          <el-input
            v-model="form.alias"
            :placeholder="$t('aliases.aliasPlaceholder')"
            :disabled="!!editingAlias"
            maxlength="128"
          />
          <div class="form-tip">{{ $t('aliases.aliasTip') }}</div>
        </el-form-item>
        <el-form-item :label="$t('aliases.colTarget')" required>
          <el-input
            v-model="form.target"
            :placeholder="$t('aliases.targetPlaceholder')"
            maxlength="128"
          />
          <div class="form-tip">{{ $t('aliases.targetTip') }}</div>
        </el-form-item>
        <el-form-item :label="$t('aliases.colDescription')">
          <el-input
            v-model="form.description"
            :placeholder="$t('aliases.descriptionPlaceholder')"
            maxlength="256"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" @click="saveAlias" :loading="saving">
          {{ editingAlias ? $t('common.save') : $t('common.add') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import api from '../api'

const { t } = useI18n()
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
    ElMessage.error(t('aliases.loadFailed'))
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
    ElMessage.warning(t('aliases.pleaseEnterAlias'))
    return
  }
  if (!form.value.target.trim()) {
    ElMessage.warning(t('aliases.pleaseEnterTarget'))
    return
  }

  saving.value = true
  try {
    if (editingAlias.value) {
      await api.put(`/api/aliases/${editingAlias.value.id}`, {
        target: form.value.target.trim(),
        description: form.value.description.trim() || null,
      })
      ElMessage.success(t('aliases.aliasUpdated'))
    } else {
      await api.post('/api/aliases', {
        alias: form.value.alias.trim(),
        target: form.value.target.trim(),
        description: form.value.description.trim() || null,
      })
      ElMessage.success(t('aliases.aliasAdded'))
    }
    dialogVisible.value = false
    await loadAliases()
  } catch (e) {
    const msg = e.response?.data?.detail || t('common.operationFailed')
    ElMessage.error(msg)
  } finally {
    saving.value = false
  }
}

async function deleteAlias(alias) {
  try {
    await ElMessageBox.confirm(
      t('aliases.deleteConfirmMsg', { name: alias.alias }),
      t('aliases.deleteConfirmTitle'),
      { type: 'warning' }
    )
    await api.delete(`/api/aliases/${alias.id}`)
    ElMessage.success(t('aliases.aliasDeleted'))
    await loadAliases()
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error(t('aliases.deleteFailed'))
    }
  }
}

async function toggleEnabled(alias) {
  alias._toggling = true
  try {
    await api.put(`/api/aliases/${alias.id}`, {
      enabled: alias.enabled,
    })
    ElMessage.success(alias.enabled ? t('aliases.aliasEnabled') : t('aliases.aliasDisabled'))
  } catch (e) {
    alias.enabled = !alias.enabled
    ElMessage.error(t('common.operationFailed'))
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
