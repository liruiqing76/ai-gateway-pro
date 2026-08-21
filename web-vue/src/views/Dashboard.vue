<template>
  <div>
    <div class="welcome-box">
      <h2>{{ $t('dashboard.welcomeTitle') }}</h2>
      <p>{{ $t('dashboard.welcomeDesc') }}</p>
    </div>

    <!-- 统计卡片组: 图标行 + 大数值 + 进度条/说明 -->
    <el-row :gutter="24" style="margin-bottom:24px">
      <el-col :span="8">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><DataLine /></el-icon>
            {{ $t('dashboard.todayCalls') }}
          </div>
          <div class="stat-value">{{ stats.used }} <span class="stat-unit">/ {{ stats.daily }}</span></div>
          <div class="stat-bar">
            <div class="stat-bar-fill" :style="{ width: Math.min(stats.pct, 100) + '%' }"></div>
          </div>
          <div class="stat-caption">{{ $t('dashboard.quotaUsage', { pct: stats.pct }) }}</div>
        </div>
      </el-col>
      <el-col :span="8">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><Box /></el-icon>
            {{ $t('billing.currentPlan') }}
          </div>
          <div class="stat-value">{{ stats.tierLabel }}</div>
          <div class="stat-caption">{{ stats.tierExpires }}</div>
        </div>
      </el-col>
      <el-col :span="8">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><Timer /></el-icon>
            {{ $t('dashboard.quotaReset') }}
          </div>
          <div class="stat-value">{{ stats.resetTime }}</div>
          <div class="stat-caption">{{ $t('dashboard.resetHint') }}</div>
        </div>
      </el-col>
    </el-row>

    <!-- 最近用量 -->
    <section class="usage-card">
      <div class="usage-head">
        <h3>{{ $t('dashboard.recentUsage') }}</h3>
        <span v-if="usageList.length" class="usage-meta">
          {{ $t('dashboard.usageSummary', { count: usageList.length, today: usageSummary.today }) }}
        </span>
      </div>
      <el-table :data="usageList" :empty-text="$t('dashboard.noRecords')" v-loading="loading">
        <el-table-column prop="time" :label="$t('dashboard.time')" width="170" />
        <el-table-column prop="provider" :label="$t('dashboard.provider')" />
        <el-table-column prop="model" :label="$t('dashboard.model')" min-width="160" />
        <el-table-column prop="tokens" label="Token" width="110" align="right">
          <template #default="{ row }">{{ row.tokens.toLocaleString() }}</template>
        </el-table-column>
        <el-table-column :label="$t('dashboard.status')" width="100">
          <template #default="{ row }">
            <el-tag :type="row.success ? 'success' : 'danger'" size="small">{{ row.success ? $t('common.success') : $t('common.failed') }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </section>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { DataLine, Box, Timer } from '@element-plus/icons-vue'
import api from '../api'

const { t } = useI18n()

const loading = ref(false)
const stats = ref({ used: 0, daily: 100, pct: 0, tierLabel: '', tierExpires: '', resetTime: '00:00' })
const usageList = ref([])
const usageSummary = ref({ total: 0, today: 0, today_tokens: 0, failed: 0 })

function tierLabel(tier) {
  if (tier === 'pro') return t('dashboard.tierPro')
  if (tier === 'enterprise') return t('dashboard.tierEnterprise')
  return t('dashboard.tierFree')
}

onMounted(async () => {
  try {
    const resp = await api.get('/api/billing/me')
    const d = resp.data
    const used = d.quota_used_today || 0
    const daily = d.quota_daily || 100
    stats.value = {
      used, daily,
      pct: daily ? Math.round(used / daily * 100) : 0,
      tierLabel: tierLabel(d.tier),
      tierExpires: d.tier_expires_at
        ? t('dashboard.expiresAt', { date: new Date(d.tier_expires_at).toLocaleDateString() })
        : t('dashboard.permanent'),
      resetTime: d.quota_reset_at ? new Date(d.quota_reset_at).toLocaleTimeString() : '00:00',
    }
  } catch (e) { /* login first */ }

  try {
    const [listResp, sumResp] = await Promise.all([
      api.get('/api/usage?limit=50'),
      api.get('/api/usage/summary'),
    ])
    usageList.value = listResp.data.data || []
    usageSummary.value = sumResp.data || usageSummary.value
  } catch (e) { /* usage 读取失败不阻塞主面板 */ }
})
</script>

<style scoped>
/* 统计卡: 对齐原型(图标标签行 / 大数值 / 进度条 / 说明) */
.stat-card-v2 {
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: var(--gw-radius-md);
  padding: 20px;
}
.stat-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--gw-muted-foreground);
}
.stat-head-icon { color: var(--gw-primary); }
.stat-value {
  margin-top: 8px;
  font-size: 24px;
  font-weight: 600;
  color: var(--gw-foreground);
}
.stat-unit {
  font-size: 16px;
  font-weight: 400;
  color: var(--gw-muted-foreground);
}
.stat-bar {
  margin-top: 12px;
  height: 6px;
  border-radius: var(--gw-radius-pill);
  background: var(--gw-primary-50);
  overflow: hidden;
}
.stat-bar-fill {
  height: 100%;
  border-radius: var(--gw-radius-pill);
  background: var(--gw-primary);
  transition: width 0.4s ease;
}
.stat-caption {
  margin-top: 8px;
  font-size: 12px;
  color: var(--gw-muted-foreground);
}

/* 最近用量卡: 标题行 + 右侧统计 */
.usage-card {
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: var(--gw-radius-md);
  overflow: hidden;
}
.usage-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px 0;
}
.usage-head h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--gw-foreground);
}
.usage-meta {
  font-size: 12px;
  color: var(--gw-muted-foreground);
}
.usage-card :deep(.el-table) {
  margin-top: 16px;
}
</style>
