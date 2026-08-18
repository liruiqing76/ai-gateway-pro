<template>
  <div>
    <div class="toolbar">
      <h2 style="font-size:18px;font-weight:600;margin:0">渠道管理</h2>
      <div style="display:flex;gap:8px">
        <el-button @click="openTemplateDialog">📋 一键添加</el-button>
        <el-button type="primary" @click="openEdit(null)">+ 添加渠道</el-button>
      </div>
    </div>

    <el-card shadow="hover">
      <el-table :data="providers" empty-text="暂无渠道" v-loading="loading">
        <el-table-column prop="name" label="名称" width="160" />
        <el-table-column prop="base_url" label="URL" show-overflow-tooltip />
        <el-table-column label="模型数" width="80" align="center">
          <template #default="{ row }">{{ (row.models || []).length }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'danger'" size="small">{{ row.enabled ? '启用' : '禁用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" align="center">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="del(row.id)">
              <template #reference><el-button size="small" type="danger">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="editId ? '编辑渠道' : '添加渠道'" width="460px" destroy-on-close>
      <el-form :model="form" label-width="80px" label-position="top">
        <el-form-item label="渠道名称"><el-input v-model="form.name" placeholder="如 NVIDIA" /></el-form-item>
        <el-form-item label="Base URL"><el-input v-model="form.base_url" placeholder="https://integrate.api.nvidia.com/v1" /></el-form-item>
        <el-form-item label="API Key"><el-input v-model="form.api_key" placeholder="nvapi-..." type="password" /></el-form-item>
        <el-form-item label="模型列表">
          <el-input v-model="form.models_str" placeholder="model-a, model-b" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="save" :loading="saving">保存</el-button>
      </template>
    </el-dialog>

    <!-- 模板弹窗 -->
    <el-dialog v-model="templateVisible" title="一键添加预设渠道" width="520px">
      <div v-for="t in templates" :key="t.name" style="background:#1e293b;padding:12px;border-radius:8px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;gap:12px">
        <div style="min-width:0">
          <strong style="font-size:13px">{{ t.name }}</strong>
          <div style="font-size:11px;color:#94a3b8">{{ t.models.length }} 个模型 · {{ t.tags.join(' · ') }}</div>
          <div style="font-size:10px;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ t.notes }}</div>
        </div>
        <el-button size="small" type="primary" @click="applyTemplate(t.name)">添加</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const loading = ref(false)
const saving = ref(false)
const providers = ref([])
const templates = ref([])
const dialogVisible = ref(false)
const templateVisible = ref(false)
const editId = ref(null)
const form = ref({ name: '', base_url: '', api_key: '', models_str: '' })

async function load() {
  loading.value = true
  try {
    const resp = await api.get('/api/providers')
    providers.value = resp.data
  } catch (e) { /* ignore */ }
  finally { loading.value = false }
}

function openEdit(row) {
  editId.value = row ? row.id : null
  form.value = row ? { name: row.name, base_url: row.base_url, api_key: '', models_str: (row.models || []).join(', ') }
    : { name: '', base_url: '', api_key: '', models_str: '' }
  dialogVisible.value = true
}

async function save() {
  saving.value = true
  const data = {
    name: form.value.name,
    base_url: form.value.base_url,
    api_key: form.value.api_key,
    models: form.value.models_str.split(',').map(s => s.trim()).filter(Boolean),
  }
  try {
    if (editId.value) {
      await api.put('/api/providers/' + editId.value, data)
    } else {
      await api.post('/api/providers', data)
    }
    ElMessage.success(editId.value ? '更新成功' : '添加成功')
    dialogVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '操作失败')
  } finally { saving.value = false }
}

async function del(id) {
  try {
    await api.delete('/api/providers/' + id)
    ElMessage.success('已删除')
    load()
  } catch (e) { ElMessage.error('删除失败') }
}

async function openTemplateDialog() {
  try {
    const resp = await api.get('/api/providers/templates/all')
    templates.value = resp.data
    templateVisible.value = true
  } catch (e) { ElMessage.error('加载模板失败') }
}

async function applyTemplate(name) {
  try {
    const { value: key } = await ElMessageBox.prompt('请输入 API Key (从 ' + name + ' 获取)', '添加渠道', {
      confirmButtonText: '添加', inputType: 'password'
    })
    if (!key) return
    const resp = await api.post('/api/providers/apply-template', { template_name: name, api_key: key })
    ElMessage.success(resp.data.msg)
    templateVisible.value = false
    load()
  } catch (e) { /* cancelled or error */ }
}

onMounted(load)
</script>