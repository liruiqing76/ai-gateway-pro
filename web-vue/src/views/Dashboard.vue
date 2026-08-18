<template>
  <div>
    <div class="welcome-box">
      <h2>👋 欢迎使用 AI 模型网关 Pro</h2>
      <p>聚合多个 AI 平台的免费额度，一个 API 调用所有模型</p>
    </div>

    <el-row :gutter="16" style="margin-bottom:20px">
      <el-col :span="8">
        <el-card shadow="hover" class="page-card">
          <div class="stat-icon">📊</div>
          <div class="stat-value">{{ stats.used }} <span style="font-size:14px;color:#94a3b8">/ {{ stats.daily }}</span></div>
          <div class="stat-label">今日调用 · {{ stats.pct }}%</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover" class="page-card">
          <div class="stat-icon">⭐</div>
          <div class="stat-value">{{ stats.tierLabel }}</div>
          <div class="stat-label">{{ stats.tierExpires }}</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover" class="page-card">
          <div class="stat-icon">🕐</div>
          <div class="stat-value">{{ stats.resetTime }}</div>
          <div class="stat-label">额度重置</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="hover">
      <template #header>
        <span class="card-title">最近用量</span>
        <span class="card-sub" v-if="usageList.length">共 {{ usageList.length }} 条 · 今日 {{ usageSummary.today }} 次</span>
      </template>
      <el-table :data="usageList" empty-text="暂无调用记录" v-loading="loading">
        <el-table-column prop="time" label="时间" width="170" />
        <el-table-column prop="provider" label="渠道" />
        <el-table-column prop="model" label="模型" min-width="160" />
        <el-table-column prop="tokens" label="Token" width="110" align="right">
          <template #default="{ row }">{{ row.tokens.toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.success ? 'success' : 'danger'">{{ row.success ? '成功' : '失败' }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const loading = ref(false)
const stats = ref({ used: 0, daily: 100, pct: 0, tierLabel: '🆓 免费版', tierExpires: '永久有效', resetTime: '00:00' })
const usageList = ref([])
const usageSummary = ref({ total: 0, today: 0, today_tokens: 0, failed: 0 })

onMounted(async () => {
  try {
    const resp = await api.get('/api/billing/me')
    const d = resp.data
    const used = d.quota_used_today || 0
    const daily = d.quota_daily || 100
    stats.value = {
      used, daily,
      pct: daily ? Math.round(used / daily * 100) : 0,
      tierLabel: d.tier === 'free' ? '🆓 免费版' : d.tier === 'pro' ? '⭐ Pro' : '🏢 企业版',
      tierExpires: d.tier_expires_at ? '到期: ' + new Date(d.tier_expires_at).toLocaleDateString() : '永久有效',
      resetTime: d.quota_reset_at ? new Date(d.quota_reset_at).toLocaleTimeString() : '00:00',
    }
  } catch (e) { /* login first */ }

  // 读取用量明细 + 汇总(修复: 之前明细表永远为空)
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
.card-sub {
  float: right;
  font-size: 13px;
  font-weight: normal;
  color: #94a3b8;
}
</style>