<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">可用模型</h2>
    <el-card shadow="hover">
      <el-table :data="models" empty-text="请先添加渠道" v-loading="loading">
        <el-table-column label="模型 ID">
          <template #default="{ row }"><span class="model-code">{{ row.id }}</span></template>
        </el-table-column>
        <el-table-column prop="owned_by" label="渠道" width="160" />
        <el-table-column label="上下文" width="120" align="center">
          <template #default="{ row }">{{ (row.context_length || 32768).toLocaleString() }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const loading = ref(false)
const models = ref([])

onMounted(async () => {
  loading.value = true
  try {
    const resp = await api.get('/v1/models')
    models.value = resp.data.data || []
  } catch (e) { /* */ }
  finally { loading.value = false }
})
</script>