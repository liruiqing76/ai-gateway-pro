<template>
  <Login v-if="showLogin" @logged-in="onLoggedIn" />

  <div class="app-container" v-else>
    <aside class="sidebar" :class="{ collapsed: isCollapse }">
      <div class="sidebar-logo">
        <span class="sidebar-logo-icon">⚡</span>
        <span class="sidebar-logo-text" v-if="!isCollapse">智能网关</span>
      </div>
      <nav class="sidebar-nav">
        <el-menu
          :default-active="activePage"
          :collapse="isCollapse"
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
          <el-menu-item index="aliases">
            <el-icon><Connection /></el-icon><span>模型别名</span>
          </el-menu-item>
          <el-menu-item index="traces">
            <el-icon><List /></el-icon><span>请求链路</span>
          </el-menu-item>
          <el-menu-item index="chats">
            <el-icon><ChatLineRound /></el-icon><span>聊天记录</span>
          </el-menu-item>
          <el-menu-item index="experience">
            <el-icon><MagicStick /></el-icon><span>经验积累</span>
          </el-menu-item>
        </el-menu>
      </nav>
    </aside>

    <div class="main-area">
      <header class="header">
        <div class="header-left">
          <el-icon class="collapse-btn" :size="18" @click="isCollapse = !isCollapse">
            <Fold v-if="!isCollapse" /><Expand v-else />
          </el-icon>
          <span class="header-title">{{ pageTitle[activePage] || '控制台' }}</span>
        </div>
        <div class="header-right">
          <el-tag size="small" type="info" v-if="userEmail" style="border-radius:12px">{{ userEmail }}</el-tag>
          <el-button size="small" text @click="logout">退出</el-button>
        </div>
      </header>

      <main class="main-content">
        <Dashboard v-if="activePage === 'dashboard'" />
        <Providers v-if="activePage === 'providers'" />
        <Models v-if="activePage === 'models'" />
        <ApiKey v-if="activePage === 'apikey'" />
        <Billing v-if="activePage === 'billing'" />
        <Checkin v-if="activePage === 'checkin'" />
        <Routers v-if="activePage === 'routers'" />
        <Aliases v-if="activePage === 'aliases'" />
        <Traces v-if="activePage === 'traces'" />
        <Chats v-if="activePage === 'chats'" />
        <Experience v-if="activePage === 'experience'" />
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'

import Login from './views/Login.vue'
import Dashboard from './views/Dashboard.vue'
import Providers from './views/Providers.vue'
import Models from './views/Models.vue'
import ApiKey from './views/ApiKey.vue'
import Billing from './views/Billing.vue'
import Checkin from './views/Checkin.vue'
import Routers from './views/Routers.vue'
import Aliases from './views/Aliases.vue'
import Traces from './views/Traces.vue'
import Chats from './views/Chats.vue'
import Experience from './views/Experience.vue'

const pageTitle = {
  dashboard: '仪表盘',
  providers: '渠道管理',
  models: '模型列表',
  apikey: 'API 密钥',
  billing: '计费中心',
  checkin: '自动签到',
  routers: '路由组',
  aliases: '模型别名',
  traces: '请求链路',
  chats: '聊天记录',
  experience: '经验积累',
}

const showLogin = ref(true)
const activePage = ref('dashboard')
const isCollapse = ref(false)
const userEmail = ref('')

onMounted(() => {
  if (localStorage.getItem('gw_token')) showLogin.value = false
  const hash = window.location.hash.replace('#/', '')
  if (hash) activePage.value = hash
  userEmail.value = localStorage.getItem('gw_email') || ''
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
  localStorage.removeItem('gw_username')
  showLogin.value = true
}
</script>