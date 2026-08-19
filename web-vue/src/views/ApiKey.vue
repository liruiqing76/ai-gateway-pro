<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">API 密钥</h2>

    <el-card shadow="hover" class="page-card">
      <template #header><span class="card-title">你的 API Key</span></template>
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <el-input v-model="apiKey" readonly style="max-width:420px;font-family:monospace" />
        <el-button @click="copy">复制</el-button>
        <el-button type="danger" @click="rotate">重置</el-button>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--gw-text-secondary)">
        端点: <code class="model-code">http://127.0.0.1:8765/v1</code>
      </div>
    </el-card>

    <el-card shadow="hover">
      <template #header><span class="card-title">使用示例 (curl)</span></template>
      <pre style="background:var(--gw-surface-secondary);padding:14px;border-radius:8px;font-size:12px;overflow-x:auto;color:#cbd5e1"><code>curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"NVIDIA-z-ai/glm-5.2","messages":[{"role":"user","content":"你好"}]}'</code></pre>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const apiKey = ref('')

onMounted(() => {
  apiKey.value = localStorage.getItem('gw_api_key') || '••••••••'
})

function copy() {
  navigator.clipboard.writeText(apiKey.value)
  ElMessage.success('已复制')
}

async function rotate() {
  try {
    await ElMessageBox.confirm('重置后旧密钥立即失效，确定？', '警告', { type: 'warning' })
    const resp = await api.post('/api/auth/rotate-key')
    const key = resp.data.api_key
    localStorage.setItem('gw_api_key', key)
    apiKey.value = key
    ElMessage.success('API Key 已重置')
  } catch (e) { /* cancelled */ }
}
</script>