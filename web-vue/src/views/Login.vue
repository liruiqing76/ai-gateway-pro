<template>
  <div class="login-page">
    <el-card class="login-card">
      <template #header>
        <div style="text-align:center">
          <h2 style="margin:0">⚡ AI 模型网关 Pro</h2>
          <p style="font-size:12px;color:#94a3b8;margin:6px 0 0">聚合免费额度 · 一个 API 调所有模型</p>
        </div>
      </template>

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
        <el-button type="primary" style="width:100%" @click="submit" :loading="loading">
          {{ mode === 'login' ? '登 录' : '注 册' }}
        </el-button>
        <div style="text-align:center;margin-top:12px;font-size:13px">
          <span style="color:#94a3b8">{{ mode === 'login' ? '还没有账号?' : '已有账号?' }}</span>
          <el-link type="primary" style="margin-left:6px" @click="switchMode">
            {{ mode === 'login' ? '去注册' : '去登录' }}
          </el-link>
        </div>
      </el-form>
    </el-card>
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
    ElMessage.success(mode.value === 'login' ? '登录成功' : '注册成功')
    // 回到仪表盘
    window.location.hash = '#/dashboard'
    window.location.reload()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '操作失败')
  } finally { loading.value = false }
}
</script>

<style scoped>
.login-page {
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh; background: #0f172a;
}
.login-card { width: 380px; }
</style>