<template>
  <div class="login-page">
    <div class="login-card">
      <h2 style="text-align:center;margin:0 0 4px;font-size:22px;font-weight:700">⚡ 智能网关</h2>
      <p style="text-align:center;font-size:13px;color:var(--gw-text-secondary);margin-bottom:28px">
        聚合免费额度 · 一个 API 调所有模型
      </p>

      <el-form :model="form" label-position="top" @submit.prevent>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" placeholder="you@example.com" />
        </el-form-item>
        <el-form-item label="用户名" v-if="mode === 'register'">
          <el-input v-model="form.username" placeholder="用户名" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="form.password" type="password" placeholder="至少 8 位，含大小写+数字+符号" show-password />
        </el-form-item>
        <el-button type="primary" style="width:100%;height:40px" @click="submit" :loading="loading">
          {{ mode === 'login' ? '登 录' : '注 册' }}
        </el-button>
        <div style="text-align:center;margin-top:16px;font-size:13px">
          <span style="color:var(--gw-text-secondary)">{{ mode === 'login' ? '还没有账号?' : '已有账号?' }}</span>
          <el-link type="primary" style="margin-left:4px" @click="switchMode">
            {{ mode === 'login' ? '去注册' : '去登录' }}
          </el-link>
        </div>
      </el-form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const mode = ref('login')
const loading = ref(false)
const form = ref({ email: '', username: '', password: '' })

function switchMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
}

async function submit() {
  if (!form.value.email || !form.value.password) {
    ElMessage.warning('请填写邮箱和密码')
    return
  }
  if (mode.value === 'register' && !form.value.username) {
    ElMessage.warning('请填写用户名')
    return
  }
  loading.value = true
  try {
    const url = mode.value === 'login' ? '/api/auth/login' : '/api/auth/register'
    const body = mode.value === 'login'
      ? { email: form.value.email, password: form.value.password }
      : { email: form.value.email, username: form.value.username, password: form.value.password }
    const resp = await api.post(url, body)
    localStorage.setItem('gw_token', resp.data.access_token)
    localStorage.setItem('gw_api_key', resp.data.api_key || '')
    localStorage.setItem('gw_username', resp.data.user?.username || form.value.email)
    localStorage.setItem('gw_email', form.value.email)
    if (mode.value === 'register') {
      ElMessage.success('注册成功，本机设备已绑定该账号')
    } else {
      ElMessage.success('登录成功')
    }
    window.location.hash = '#/dashboard'
    window.location.reload()
  } catch (e) {
    const detail = e.response?.data?.detail
    if (Array.isArray(detail)) {
      ElMessage.error(detail[0]?.msg || '校验失败')
    } else {
      ElMessage.error(detail || '操作失败')
    }
  } finally { loading.value = false }
}
</script>

<style scoped>
.login-page {
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #f5f5f7 0%, #e8ecf4 50%, #f0f4ff 100%);
}
.login-card {
  width: 400px;
  background: rgba(255,255,255,0.9);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-radius: 16px;
  padding: 36px;
  box-shadow: 0 8px 40px rgba(0,0,0,0.08);
}
</style>