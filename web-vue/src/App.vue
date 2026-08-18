<template>
  <Login v-if="showLogin" @logged-in="onLoggedIn" />

  <el-container class="app-container" v-else>
    <el-aside :width="isCollapse ? '64px' : '220px'" class="sidebar">
      <div class="logo">
        <span v-if="isCollapse">⚡</span>
        <span v-else class="logo-text">⚡ 网关 Pro</span>
      </div>
      <el-menu
        :default-active="activePage"
        :collapse="isCollapse"
        background-color="#0f172a"
        text-color="#94a3b8"
        active-text-color="#818cf8"
        class="menu"
        @select="onSelect"
      >
        <el-menu-item index="dashboard">
          <el-icon><Odometer /></el-icon><span>仪表盘</span>
        </el-menu-item>
        <el-menu-item index="providers">
          <el-icon><Connection /></el-icon><span>渠道管理</span>
        </el-menu-item>
        <el-menu-item index="models">
          <el-icon><Cpu /></el-icon><span>模型列表</span>
        </el-menu-item>
        <el-menu-item index="apikey">
          <el-icon><Key /></el-icon><span>API 密钥</span>
        </el-menu-item>
        <el-menu-item index="billing">
          <el-icon><Wallet /></el-icon><span>计费中心</span>
        </el-menu-item>
        <el-menu-item index="checkin">
          <el-icon><Present /></el-icon><span>自动签到</span>
        </el-menu-item>
        <el-menu-item index="routers">
          <el-icon><Coin /></el-icon><span>路由组</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div class="header-left">
          <el-icon class="collapse-btn" @click="isCollapse = !isCollapse">
            <Fold v-if="!isCollapse" /><Expand v-else />
          </el-icon>
          <span class="page-title">{{ pageTitle[activePage] || '控制台' }}</span>
        </div>
        <div class="header-right">
          <el-tag size="small" type="info" v-if="userEmail">{{ userEmail }}</el-tag>
          <el-button size="small" text @click="logout">退出</el-button>
        </div>
      </el-header>

      <el-main class="main">
        <Dashboard v-if="activePage === 'dashboard'" />
        <Providers v-if="activePage === 'providers'" />
        <Models v-if="activePage === 'models'" />
        <ApiKey v-if="activePage === 'apikey'" />
        <Billing v-if="activePage === 'billing'" />
        <Checkin v-if="activePage === 'checkin'" />
        <Routers v-if="activePage === 'routers'" />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'

import Login from './views/Login.vue'
import Dashboard from './views/Dashboard.vue'
import Providers from './views/Providers.vue'
import Models from './views/Models.vue'
import ApiKey from './views/ApiKey.vue'
import Billing from './views/Billing.vue'
import Checkin from './views/Checkin.vue'
import Routers from './views/Routers.vue'

const pageTitle = {
  dashboard: '仪表盘',
  providers: '渠道管理',
  models: '模型列表',
  apikey: 'API 密钥',
  billing: '计费中心',
  checkin: '自动签到',
  routers: '路由组',
}

const showLogin = ref(true)
const activePage = ref('dashboard')
const isCollapse = ref(false)
const userEmail = ref('')

onMounted(() => {
  if (localStorage.getItem('gw_token')) showLogin.value = false
  const hash = window.location.hash.replace('#/', '')
  if (hash) activePage.value = hash
})

function onSelect(index) {
  activePage.value = index
  window.location.hash = index
}

function onLoggedIn() {
  showLogin.value = false
  userEmail.value = localStorage.getItem('gw_email') || ''
}

function logout() {
  localStorage.removeItem('gw_token')
  localStorage.removeItem('gw_api_key')
  localStorage.removeItem('gw_email')
  showLogin.value = true
}
</script>

<style scoped>
.app-container {
  height: 100vh;
  background: #0b1120;
}
.sidebar {
  background: #0f172a;
  border-right: 1px solid #1e293b;
  overflow-y: auto;
}
.logo {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 700;
  color: #818cf8;
  border-bottom: 1px solid #1e293b;
}
.menu { border-right: none; }
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #0f172a;
  border-bottom: 1px solid #1e293b;
  padding: 0 20px;
  height: 48px;
}
.header-left { display: flex; align-items: center; gap: 12px; }
.page-title { font-size: 14px; font-weight: 600; color: #e2e8f0; }
.main { background: #0b1120; padding: 20px; color: #e2e8f0; }
</style>