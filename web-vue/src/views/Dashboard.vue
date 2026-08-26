<template>
  <div>
    <div class="welcome-box">
      <h2>{{ $t('dashboard.welcomeTitle') }}</h2>
      <p>{{ $t('dashboard.welcomeDesc') }}</p>
    </div>

    <!-- 核心统计: 6 张卡 — 今日/总调用、今日/总Token、可用模型、分组 -->
    <el-row :gutter="16" style="margin-bottom:24px">
      <el-col :span="4">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><Clock /></el-icon>
            {{ $t('dashboard.todayCalls') }}
          </div>
          <div class="stat-value">{{ stats.today.toLocaleString() }}</div>
          <div class="stat-caption">{{ $t('dashboard.todayCallsCaption', { n: stats.failed.toLocaleString() }) }}</div>
        </div>
      </el-col>
      <el-col :span="4">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><DataLine /></el-icon>
            {{ $t('dashboard.totalCalls') }}
          </div>
          <div class="stat-value">{{ stats.total.toLocaleString() }}</div>
          <div class="stat-caption">{{ $t('dashboard.totalCallsCaption') }}</div>
        </div>
      </el-col>
      <el-col :span="4">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><Coin /></el-icon>
            {{ $t('dashboard.todayTokens') }}
          </div>
          <div class="stat-value">{{ stats.todayTokens.toLocaleString() }}</div>
          <div class="stat-caption">{{ $t('dashboard.todayTokensCaption') }}</div>
        </div>
      </el-col>
      <el-col :span="4">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><TrendCharts /></el-icon>
            {{ $t('dashboard.totalTokens') }}
          </div>
          <div class="stat-value">{{ stats.totalTokens.toLocaleString() }}</div>
          <div class="stat-caption">{{ $t('dashboard.totalTokensCaption') }}</div>
        </div>
      </el-col>
      <el-col :span="4">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><Box /></el-icon>
            {{ $t('dashboard.availableModels') }}
          </div>
          <div class="stat-value">{{ stats.models }}</div>
          <div class="stat-caption">{{ $t('dashboard.providersCaption', { n: stats.providers }) }}</div>
        </div>
      </el-col>
      <el-col :span="4">
        <div class="stat-card-v2">
          <div class="stat-head">
            <el-icon class="stat-head-icon" :size="18"><FolderOpened /></el-icon>
            {{ $t('dashboard.routerGroups') }}
          </div>
          <div class="stat-value">{{ stats.groups }}</div>
          <div class="stat-caption">{{ $t('dashboard.groupsCaption') }}</div>
        </div>
      </el-col>
    </el-row>

    <!-- 使用日历: 月视图, 无数据日灰显, 颜色深浅 = 当日调用量级 -->
    <section class="usage-card">
      <div class="usage-head">
        <h3>{{ $t('dashboard.usageCalendar') }}</h3>
        <span v-if="monthDate" class="usage-meta">
          {{ $t('dashboard.calMonthSummary', {
            calls: monthSum.calls.toLocaleString(),
            tokens: monthSum.tokens.toLocaleString(),
          }) }}
        </span>
        <span v-else class="usage-meta"></span>
      </div>
      <el-config-provider :locale="zhCn">
        <el-calendar v-model="calDate" @change="onCalChange">
          <template #date-cell="{ data }">
            <div class="cal-cell" :class="calCellClass(data.day)">
              <div class="cal-day">{{ data.day.slice(8).replace(/^0/, '') }}</div>
              <div v-if="dayCalls(data.day) > 0" class="cal-num" :title="calTooltip(data.day)">
                {{ dayCalls(data.day) }}
              </div>
            </div>
          </template>
        </el-calendar>
      </el-config-provider>
    </section>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Clock, DataLine, Coin, TrendCharts, Box, FolderOpened } from '@element-plus/icons-vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import api from '../api'

const { t } = useI18n()

const stats = ref({ total: 0, today: 0, totalTokens: 0, todayTokens: 0, models: 0, providers: 0, groups: 0, failed: 0 })

// ── 使用日历 ──
const calDate = ref(new Date())           // el-calendar 当前视图月份
const monthData = ref({})                 // { "YYYY-MM-DD": {calls, tokens} }, 当前显示月
const monthDate = ref('')                 // 当前显示月 "YYYY-MM"
let lastMonth = ''                        // 防抖: 同一月不重复拉取

function dayCalls(day) {
  return (monthData.value[day] || {}).calls || 0
}
function calTooltip(day) {
  const d = monthData.value[day]
  if (!d) return ''
  return t('dashboard.calTooltip', { calls: d.calls.toLocaleString(), tokens: d.tokens.toLocaleString() })
}
// 无数据灰 / 有数据按调用数 3 档加深
function calCellClass(day) {
  const n = dayCalls(day)
  if (n === 0) return 'cal-cell-empty'
  if (n < 5) return 'cal-cell-l1'
  if (n < 20) return 'cal-cell-l2'
  return 'cal-cell-l3'
}
const monthSum = computed(() => {
  let calls = 0, tokens = 0
  for (const d of Object.values(monthData.value)) { calls += d.calls; tokens += d.tokens }
  return { calls, tokens }
})

async function loadMonth(date) {
  const ym = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
  if (ym === lastMonth && Object.keys(monthData.value).length) return
  lastMonth = ym
  try {
    const resp = await api.get(`/api/usage/daily?month=${ym}`)
    const list = resp.data.data || []
    const map = {}
    for (const d of list) map[d.date] = { calls: d.calls, tokens: d.tokens }
    monthData.value = map
    monthDate.value = ym
  } catch (e) { /* 月份数据失败保持空日历 */ }
}

function onCalChange(d) { loadMonth(d) }

onMounted(async () => {
  await loadMonth(new Date())
  // usage / providers / routers 三个接口各自失败不阻塞其它统计
  await Promise.allSettled([
    (async () => {
      const resp = await api.get('/api/usage/summary')
      const d = resp.data
      stats.value.total = d.total || 0
      stats.value.today = d.today || 0
      stats.value.totalTokens = d.total_tokens || 0
      stats.value.todayTokens = d.today_tokens || 0
      stats.value.failed = d.failed || 0
    })(),
    (async () => {
      // 可用模型 = 全部启用渠道的模型并集(去重); 渠道数 = 启用渠道数
      const resp = await api.get('/api/providers')
      const list = resp.data || []
      const enabledList = list.filter(p => p.enabled)
      const modelSet = new Set()
      for (const p of enabledList) for (const m of (p.models || [])) modelSet.add(m)
      stats.value.models = modelSet.size
      stats.value.providers = enabledList.length
    })(),
    (async () => {
      const resp = await api.get('/api/routers')
      stats.value.groups = Object.keys((resp.data || {}).data || {}).length
    })(),
  ])
})
</script>

<style scoped>
.stat-card-v2 {
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: var(--gw-radius-md);
  padding: 18px;
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
  font-size: 22px;
  font-weight: 600;
  color: var(--gw-foreground);
  word-break: break-all;
}
.stat-caption {
  margin-top: 8px;
  font-size: 12px;
  color: var(--gw-muted-foreground);
}

/* 使用日历卡 */
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

/* 日历格子: 空灰, 有数据按调用数 3 档深浅 */
.usage-card :deep(.el-calendar) {
  --el-calendar-cell-width: 100%;
}
.cal-cell {
  min-height: 52px;
  padding: 4px 6px;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}
.cal-day {
  font-size: 12px;
  color: var(--gw-muted-foreground);
}
.cal-num {
  align-self: flex-end;
  font-size: 13px;
  font-weight: 600;
  color: var(--gw-foreground);
}
.cal-cell-empty { background: transparent; }
.cal-cell-l1 { background: var(--gw-primary-50); }
.cal-cell-l2 { background: var(--gw-primary-100); }
.cal-cell-l3 { background: var(--gw-primary); }
.cal-cell-l3 .cal-day, .cal-cell-l3 .cal-num { color: #fff; }
</style>