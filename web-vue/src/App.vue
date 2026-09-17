<template>
  <el-config-provider :locale="elementLocale">
    <Login v-if="showLogin" @logged-in="onLoggedIn" />

    <div class="app-container" v-else>
      <aside class="sidebar" :class="{ collapsed: isCollapse }">
        <div class="sidebar-logo">
          <img class="sidebar-logo-icon" src="/frenchie-logo.png" alt="AI 网关" />
          <span class="sidebar-logo-text" v-if="!isCollapse">{{ $t('app.title') }}</span>
        </div>
        <nav class="sidebar-nav">
          <el-menu
            :default-active="activePage"
            :collapse="isCollapse"
            @select="onSelect"
          >
            <el-menu-item index="dashboard">
              <el-icon><Odometer /></el-icon><span>{{ $t('app.nav.dashboard') }}</span>
            </el-menu-item>
            <el-menu-item index="providers">
              <el-icon><Connection /></el-icon><span>{{ $t('app.nav.providers') }}</span>
            </el-menu-item>
            <el-menu-item index="models">
              <el-icon><Cpu /></el-icon><span>{{ $t('app.nav.models') }}</span>
            </el-menu-item>
            <el-menu-item index="apikey">
              <el-icon><Key /></el-icon><span>{{ $t('app.nav.apikey') }}</span>
            </el-menu-item>
            <el-menu-item index="billing">
              <el-icon><Wallet /></el-icon><span>{{ $t('app.nav.billing') }}</span>
            </el-menu-item>
            <el-menu-item index="checkin">
              <el-icon><Present /></el-icon><span>{{ $t('app.nav.checkin') }}</span>
            </el-menu-item>
            <el-menu-item index="routers">
              <el-icon><Coin /></el-icon><span>{{ $t('app.nav.routers') }}</span>
            </el-menu-item>
            <el-menu-item index="aliases">
              <el-icon><Connection /></el-icon><span>{{ $t('app.nav.aliases') }}</span>
            </el-menu-item>
            <el-menu-item index="requests">
              <el-icon><List /></el-icon><span>{{ $t('app.nav.requests') }}</span>
            </el-menu-item>
            <el-menu-item index="free-model-search">
              <el-icon><Search /></el-icon><span>免费模型搜索</span>
            </el-menu-item>
            <el-menu-item index="settings">
              <el-icon><Setting /></el-icon><span>{{ $t('app.nav.settings') }}</span>
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
            <span class="header-title">{{ pageTitle }}</span>
          </div>
          <div class="header-right">
            <!-- 语言切换 -->
            <el-dropdown trigger="click" @command="changeLocale">
              <span class="lang-switcher" title="Language / 语言">
                <el-icon :size="16"><Operation /></el-icon>
                <span class="lang-label">{{ currentLangLabel }}</span>
                <el-icon :size="12"><ArrowDown /></el-icon>
              </span>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="zh-CN" :class="{ active: locale === 'zh-CN' }">简体中文</el-dropdown-item>
                  <el-dropdown-item command="en-US" :class="{ active: locale === 'en-US' }">English</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <div v-if="userEmail" class="header-user">
              <el-icon :size="14"><Message /></el-icon>
              <span>{{ userEmail }}</span>
            </div>
            <button type="button" class="logout-link" @click="logout">{{ $t('app.logout') }}</button>
          </div>
        </header>

        <main class="main-content">
          <div class="content-shell">
            <Dashboard v-if="activePage === 'dashboard'" />
            <Providers v-if="activePage === 'providers'" />
            <Models v-if="activePage === 'models'" />
            <ApiKey v-if="activePage === 'apikey'" />
            <Billing v-if="activePage === 'billing'" />
            <Checkin v-if="activePage === 'checkin'" />
            <Routers v-if="activePage === 'routers'" />
            <Aliases v-if="activePage === 'aliases'" />
            <Requests v-if="activePage === 'requests'" />
            <FreeModelSearch v-if="activePage === 'free-model-search'" />
            <Settings v-if="activePage === 'settings'" />
          </div>
        </main>
      </div>
    </div>
  </el-config-provider>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import enUs from 'element-plus/es/locale/lang/en'
import { setLocale } from './i18n'

import Login from './views/Login.vue'
import Dashboard from './views/Dashboard.vue'
import Providers from './views/Providers.vue'
import Models from './views/Models.vue'
import ApiKey from './views/ApiKey.vue'
import Billing from './views/Billing.vue'
import Checkin from './views/Checkin.vue'
import Routers from './views/Routers.vue'
import Aliases from './views/Aliases.vue'
import Requests from './views/Requests.vue'
import Settings from './views/Settings.vue'
import FreeModelSearch from './views/FreeModelSearch.vue'

const { t, locale } = useI18n()

const showLogin = ref(true)
const activePage = ref('dashboard')
const isCollapse = ref(false)
const userEmail = ref('')

// Element Plus 内置组件(表格空态/时间选择器等)跟随界面语言
const elementLocale = computed(() => (locale.value === 'zh-CN' ? zhCn : enUs))
const currentLangLabel = computed(() => (locale.value === 'zh-CN' ? '中文' : 'EN'))
const pageTitle = computed(() => t(`app.nav.${activePage.value}`) || t('app.console'))

function changeLocale(lang) {
  setLocale(lang)
}

watch(locale, () => {
  document.title = `${t('app.title')}`
})

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
  localStorage.removeItem('gw_refresh_token')
  localStorage.removeItem('gw_api_key')
  localStorage.removeItem('gw_email')
  localStorage.removeItem('gw_username')
  showLogin.value = true
}
</script>

<style scoped>
.lang-switcher {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 8px;
  color: var(--gw-text-secondary, #64748b);
  font-size: 13px;
  user-select: none;
}
.lang-switcher:hover {
  background: rgba(100, 116, 139, 0.08);
}
.header-user {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--gw-muted-foreground);
}
.logout-link {
  font-size: 14px;
  font-weight: 500;
  color: var(--gw-primary);
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
}
.logout-link:hover {
  opacity: 0.8;
}
</style>
