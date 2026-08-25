<template>
  <div>
    <div class="page-header">
      <h2>{{ $t('providers.channelManagement') }}</h2>
      <div style="display:flex;gap:8px">
        <el-button @click="openTemplateDialog">
          <el-icon style="margin-right:4px"><Zap /></el-icon>{{ $t('providers.quickAdd') }}
        </el-button>
        <el-button type="primary" @click="openAdd">
          <el-icon style="margin-right:4px"><Plus /></el-icon>{{ $t('providers.addChannel') }}
        </el-button>
      </div>
    </div>

    <el-card shadow="hover" style="margin-bottom:12px">
      <el-table :data="providers" :empty-text="$t('providers.noChannels')" v-loading="loading">
        <el-table-column prop="name" :label="$t('providers.name')" width="160">
          <template #default="{row}"><span style="font-weight:600">{{ row.name }}</span></template>
        </el-table-column>
        <el-table-column prop="base_url" label="Base URL" min-width="260">
          <template #default="{row}"><code style="font-size:11px;color:var(--gw-accent)">{{ row.base_url }}</code></template>
        </el-table-column>
        <el-table-column :label="$t('providers.modelCount')" width="80" align="center">
          <template #default="{row}">{{ (row.models || []).length }}</template>
        </el-table-column>
        <el-table-column :label="$t('providers.status')" width="80" align="center">
          <template #default="{row}">
            <el-tag :type="row.enabled !== false ? 'success' : 'info'" size="small">{{ row.enabled !== false ? $t('common.enabled') : $t('common.disabled') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('providers.actions')" width="200" align="center">
          <template #default="{row}">
            <el-button size="small" text @click="edit(row)">{{ $t('common.edit') }}</el-button>
            <el-button size="small" text :loading="refreshingId === row.id" @click="refreshModels(row)">{{ $t('providers.refreshModels') }}</el-button>
            <el-button size="small" text type="danger" @click="del(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div style="padding:14px 6px 2px;font-size:12px;color:var(--gw-text-secondary)">
        {{ $t('providers.footerSummary', { count: providers.length, models: totalModels }) }}
      </div>
    </el-card>

    <!-- 手动添加/编辑渠道 -->
    <el-dialog v-model="dialogVisible" :title="editing ? $t('providers.editChannel') : $t('providers.addChannelTitle')" width="520px" top="8vh" destroy-on-close>
      <el-form :model="form" label-position="top">
        <el-form-item :label="$t('providers.name')">
          <el-input v-model="form.name" :placeholder="$t('providers.namePlaceholder')" />
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" placeholder="https://integrate.api.nvidia.com/v1" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" type="password" placeholder="sk-..." show-password />
        </el-form-item>
        <el-form-item :label="$t('providers.modelList')">
          <el-input v-model="form.models_str" placeholder="model-a, model-b" />
          <el-button size="small" style="margin-top:8px" @click="fetchModels" :loading="fetching">
            {{ fetching ? $t('providers.fetchingModels') : $t('providers.fetchModelList') }}
          </el-button>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" @click="save" :loading="saving">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 模板一键添加 -->
    <el-dialog v-model="templateDialogVisible" :title="$t('providers.templateQuickAdd')" width="560px" top="8vh" destroy-on-close @open="loadTemplates">
      <div v-if="templatesLoading" v-loading="templatesLoading" style="min-height:160px" />
      <div v-else class="template-grid">
        <div v-for="tpl in templates" :key="tpl.name" class="template-card">
          <div class="template-header">
            <span class="template-name">{{ tpl.name }}</span>
            <el-tag v-if="tpl.tags" size="small" :type="tpl.tags.includes('付费') ? 'warning' : 'success'" style="margin-left:auto">
              {{ tpl.tags.includes('付费') ? $t('providers.paid') : $t('providers.free') }}
            </el-tag>
          </div>
          <div class="template-notes">{{ tpl.notes }}</div>
          <div class="template-models">
            <el-tag v-for="m in (tpl.models || []).slice(0,4)" :key="m" size="small" style="margin:2px 4px 2px 0">{{ m }}</el-tag>
            <span v-if="(tpl.models || []).length > 4" style="font-size:11px;color:var(--gw-text-tertiary)">+{{ (tpl.models || []).length - 4 }}</span>
          </div>
          <el-input v-model="tplKeys[tpl.name]" type="password" :placeholder="$t('providers.fillApiKeyPlaceholder')" show-password size="small" style="margin:8px 0" />
          <el-button size="small" type="primary" style="width:100%" @click="applyTemplate(tpl)" :disabled="!tplKeys[tpl.name]">{{ $t('common.add') }}</el-button>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const { t } = useI18n()

const loading = ref(false)
const saving = ref(false)
const fetching = ref(false)
const providers = ref([])
const totalModels = computed(() => providers.value.reduce((n, p) => n + ((p.models || []).length), 0))
const dialogVisible = ref(false)
const editing = ref(false)
const refreshingId = ref(null)
const editId = ref(null)
const form = ref({ name: '', base_url: '', api_key: '', models_str: '' })

const templateDialogVisible = ref(false)
const templatesLoading = ref(false)
const templates = ref([])
const tplKeys = reactive({})

onMounted(loadProviders)

async function loadProviders() {
  loading.value = true
  try {
    const resp = await api.get('/api/providers')
    providers.value = resp.data || []
  } catch (e) {
    if (e.message !== 'Unauthorized') ElMessage.error(t('providers.loadProvidersFailed'))
  } finally {
    loading.value = false
  }
}

function openAdd() {
  editing.value = false
  editId.value = null
  form.value = { name: '', base_url: '', api_key: '', models_str: '' }
  dialogVisible.value = true
}

async function openTemplateDialog() {
  templateDialogVisible.value = true
  await loadTemplates()
}

async function loadTemplates() {
  templatesLoading.value = true
  try {
    const resp = await api.get('/api/providers/templates/all')
    templates.value = resp.data || []
  } catch (e) {
    ElMessage.error(t('providers.loadTemplatesFailed') + e)
  } finally {
    templatesLoading.value = false
  }
}

async function applyTemplate(tpl) {
  const key = tplKeys[tpl.name]
  if (!key) return
  try {
    const resp = await api.post('/api/providers/apply-template', {
      template_name: tpl.name,
      api_key: key,
    })
    ElMessage.success(t('providers.addedTemplate', { name: tpl.name }))
    delete tplKeys[tpl.name]
    await loadProviders()
    templateDialogVisible.value = false
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('providers.addFailed'))
  }
}

function edit(row) {
  editing.value = true
  editId.value = row.id
  form.value = {
    name: row.name,
    base_url: row.base_url,
    api_key: '',
    models_str: (row.models || []).join(', '),
  }
  dialogVisible.value = true
}

async function fetchModels() {
  if (!form.value.base_url || !form.value.api_key) {
    ElMessage.warning(t('providers.pleaseFillBaseUrlAndKey'))
    return
  }
  fetching.value = true
  try {
    const resp = await api.post('/api/providers/fetch-models', {
      base_url: form.value.base_url,
      api_key: form.value.api_key,
    })
    const models = resp.data.models || []
    form.value.models_str = models.join(', ')
    ElMessage.success(t('providers.fetchedModels', { count: models.length }))
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('providers.fetchModelsFailed'))
  } finally {
    fetching.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const body = {
      name: form.value.name,
      base_url: form.value.base_url,
      api_key: form.value.api_key,
      models: form.value.models_str.split(',').map(s => s.trim()).filter(Boolean),
    }
    if (editing.value) {
      await api.put(`/api/providers/${editId.value}`, body)
    } else {
      await api.post('/api/providers', body)
    }
    ElMessage.success(editing.value ? t('providers.updated') : t('providers.added'))
    dialogVisible.value = false
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('providers.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function refreshModels(row) {
  refreshingId.value = row.id
  try {
    const resp = await api.post(`/api/providers/${row.id}/refresh-models`)
    ElMessage.success(resp.data?.msg || t('providers.refreshModels'))
    await loadProviders()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('providers.refreshFailed'))
  } finally {
    refreshingId.value = null
  }
}

function del(row) {
  ElMessageBox.confirm(t('providers.confirmDeleteChannel', { name: row.name }), t('providers.confirmTitle'), { type: 'warning' }).then(async () => {
    try {
      await api.delete(`/api/providers/${row.id}`)
      ElMessage.success(t('providers.deleted'))
      providers.value = providers.value.filter(p => p.id !== row.id)
    } catch (e) {
      ElMessage.error(t('providers.deleteFailed') + e)
    }
  }).catch(() => {})
}
</script>

<style scoped>
.template-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 480px;
  overflow-y: auto;
}
.template-card {
  border: 1px solid var(--gw-border);
  border-radius: 10px;
  padding: 12px;
  background: var(--gw-surface);
}
.template-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.template-name {
  font-weight: 600;
  font-size: 14px;
}
.template-notes {
  font-size: 12px;
  color: var(--gw-text-secondary);
  margin: 6px 0;
  line-height: 1.5;
}
.template-models {
  display: flex;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
</style>
