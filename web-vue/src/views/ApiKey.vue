<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">{{ $t('apiKey.title') }}</h2>

    <el-card shadow="hover" class="page-card">
      <template #header><span class="card-title">{{ $t('apiKey.yourKey') }}</span></template>
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <el-input v-model="apiKey" readonly style="max-width:420px;font-family:monospace" />
        <el-button @click="copy">{{ $t('common.copy') }}</el-button>
        <el-button type="danger" @click="rotate">{{ $t('apiKey.rotate') }}</el-button>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--gw-text-secondary)">
        {{ $t('apiKey.endpoint') }}: <code class="model-code">http://127.0.0.1:8765/v1</code>
      </div>
    </el-card>

    <el-card shadow="hover">
      <template #header><span class="card-title">{{ $t('apiKey.curlExample') }}</span></template>
      <pre style="background:var(--gw-muted);padding:14px;border-radius:8px;font-family:var(--gw-font-mono);font-size:12px;overflow-x:auto;color:var(--gw-foreground)"><code>curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"NVIDIA-z-ai/glm-5.2","messages":[{"role":"user","content":"Hello"}]}'</code></pre>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../api'

const { t } = useI18n()
const apiKey = ref('')

onMounted(() => {
  apiKey.value = localStorage.getItem('gw_api_key') || '••••••••'
})

function copy() {
  navigator.clipboard.writeText(apiKey.value)
  ElMessage.success(t('common.copied'))
}

async function rotate() {
  try {
    await ElMessageBox.confirm(t('apiKey.rotateConfirm'), t('common.warning'), { type: 'warning' })
    const resp = await api.post('/api/auth/rotate-key')
    const key = resp.data.api_key
    localStorage.setItem('gw_api_key', key)
    apiKey.value = key
    ElMessage.success(t('apiKey.rotated'))
  } catch (e) { /* cancelled */ }
}
</script>
