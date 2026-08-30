<template>
  <div class="dash">
    <!-- ── 4 张主卡: Token + 字符双口径 ── -->
    <div class="hero-row">
      <div class="hero-card">
        <div class="hero-head">
          <span class="hero-label">{{ $t('dashboard.todayTokens') }}</span>
          <span class="hero-ico" v-html="icoCoinW"></span>
        </div>
        <div class="hero-value">{{ fmt(stats.todayTokens) }}</div>
        <div class="hero-foot">
          <span v-if="tokDelta !== null" class="badge" :class="tokDelta >= 0 ? 'badge-up' : 'badge-down'">
            {{ tokDelta >= 0 ? '↑' : '↓' }} {{ Math.abs(tokDelta) }}%
          </span>
          <span class="hero-caption">{{ $t('dashboard.vsYesterday') }}</span>
        </div>
      </div>
      <div class="hero-card">
        <div class="hero-head">
          <span class="hero-label">{{ $t('dashboard.totalTokens') }}</span>
          <span class="hero-ico" v-html="icoBarsW"></span>
        </div>
        <div class="hero-value">{{ fmtBig(stats.totalTokens) }}</div>
        <div class="hero-caption">{{ $t('dashboard.totalTokensCaption', { n: fmt(stats.total) }) }}</div>
      </div>
      <div class="hero-card">
        <div class="hero-head">
          <span class="hero-label">{{ $t('dashboard.todayChars') }}</span>
          <span class="hero-ico" v-html="icoDocW"></span>
        </div>
        <div class="hero-value">{{ fmt(stats.todayChars) }}</div>
        <div class="hero-foot">
          <span v-if="charDelta !== null" class="badge" :class="charDelta >= 0 ? 'badge-up' : 'badge-down'">
            {{ charDelta >= 0 ? '↑' : '↓' }} {{ Math.abs(charDelta) }}%
          </span>
          <span class="hero-caption">{{ $t('dashboard.charsCaptionIO') }}</span>
        </div>
      </div>
      <div class="hero-card">
        <div class="hero-head">
          <span class="hero-label">{{ $t('dashboard.totalChars') }}</span>
          <span class="hero-ico" v-html="icoWaveW"></span>
        </div>
        <div class="hero-value">{{ fmtBig(stats.totalChars) }}</div>
        <div class="hero-caption">{{ $t('dashboard.charsCaptionDual') }}</div>
      </div>
    </div>

    <!-- ── 次要指标条 ── -->
    <div class="chip-row">
      <div class="chip" v-for="c in chips" :key="c.key">
        <span class="chip-dot" :style="{ background: c.color }"></span>
        <span class="chip-label">{{ c.label }}</span>
        <span class="chip-val">{{ c.value }}</span>
      </div>
    </div>

    <!-- ── 用量趋势: Token / 字符双系列 ── -->
    <div class="card">
      <div class="card-head">
        <div>
          <h3>{{ $t('dashboard.trendTitle') }}</h3>
          <p class="sub">{{ range === '12m' ? $t('dashboard.trendSubMonthly') : $t('dashboard.trendSubDaily') }}</p>
        </div>
        <div class="legend">
          <span class="legend-item"><span class="legend-dot" style="background:#91CAFF"></span>{{ $t('dashboard.legendChars') }}</span>
          <span class="legend-item"><span class="legend-dot" style="background:var(--gw-primary)"></span>{{ $t('dashboard.legendTokens') }}</span>
        </div>
        <div class="range-tabs">
          <button v-for="r in ranges" :key="r.key" class="range-tab" :class="{ active: range === r.key }"
                  @click="setRange(r.key)">{{ r.label }}</button>
        </div>
      </div>
      <svg v-if="chart" class="trend-svg" :viewBox="`0 0 ${chart.W} ${chart.H}`" preserveAspectRatio="xMidYMid meet">
        <rect v-for="(g, i) in chart.grid" :key="'g' + i" :x="chart.padL" :y="g.y" :width="chart.plotW"
              :height="g.major ? 1 : 1" :fill="g.major ? '#E5E5EA' : '#F2F2F5'" />
        <text v-for="(l, i) in chart.yLabels" :key="'yl' + i" :x="chart.padL - 8" :y="l.y + 3.5"
              text-anchor="end" class="axis-text">{{ l.text }}</text>
        <text v-for="(l, i) in chart.yrLabels" :key="'yr' + i" :x="chart.W - chart.padR + 8" :y="l.y + 3.5"
              text-anchor="start" class="axis-text">{{ l.text }}</text>
        <rect v-for="(b, i) in chart.bars" :key="'c' + i" :x="b.x1" :y="b.charsY" :width="chart.barW"
              :height="b.charsH" rx="3" fill="#91CAFF" />
        <rect v-for="(b, i) in chart.bars" :key="'t' + i" :x="b.x2" :y="b.tokY" :width="chart.barW"
              :height="b.tokH" rx="3" :fill="b.isLast ? '#0E5FD8' : '#1677FF'" />
        <text v-for="(l, i) in chart.xLabels" :key="'xl' + i" :x="l.x" :y="chart.H - 6"
              text-anchor="middle" class="axis-text" :class="{ 'axis-today': l.today }">{{ l.text }}</text>
      </svg>
      <div v-else class="empty">{{ $t('dashboard.noTokenData') }}</div>
    </div>

    <!-- ── 渠道 / 模型可靠性排行: 真实调用口径, 排除探测 ── -->
    <div class="rel-row">
      <div class="card rel-card">
        <div class="card-head">
          <div>
            <h3>{{ $t('dashboard.reliabilityTitle') }}</h3>
            <p class="sub">{{ $t('dashboard.reliabilitySubtitle') }}</p>
          </div>
          <span v-if="bestProvider" class="best-pill">
            {{ $t('dashboard.mostReliable', { name: bestProvider.name, rate: bestProvider.success_rate.toFixed(1) }) }}
          </span>
        </div>
        <div v-if="reliability.providers.length" class="rel-list">
          <div class="rel-item" v-for="(p, i) in reliability.providers" :key="p.name">
            <span class="rank" :class="{ top: i === 0 }">{{ i + 1 }}</span>
            <span class="rel-name" :title="p.name">{{ p.name }}</span>
            <span class="rel-calls">{{ fmt(p.calls) }} {{ $t('dashboard.callsUnit') }}</span>
            <span class="rel-track">
              <span class="rel-fill" :style="{ width: p.success_rate + '%', background: rateColor(p.success_rate) }"></span>
            </span>
            <span class="rel-rate" :style="{ color: rateText(p.success_rate) }">{{ p.success_rate.toFixed(1) }}%</span>
            <span class="rel-lat">{{ fmtLatency(p.avg_latency_ms) }}</span>
          </div>
        </div>
        <div v-else class="empty small">{{ $t('dashboard.reliabilityEmpty') }}</div>
        <p class="footnote">
          {{ $t('dashboard.reliabilityFootnote', { n: fmt(reliability.excluded_probes) }) }}
        </p>
      </div>

      <div class="card rel-card">
        <div class="card-head">
          <div>
            <h3>{{ $t('dashboard.modelReliabilityTitle') }}</h3>
            <p class="sub">{{ $t('dashboard.modelReliabilitySubtitle', { n: reliability.min_calls }) }}</p>
          </div>
          <span v-if="bestModel" class="best-pill">
            {{ $t('dashboard.mostReliable', { name: bestModel.name, rate: bestModel.success_rate.toFixed(1) }) }}
          </span>
        </div>
        <div v-if="reliability.models.length" class="rel-list">
          <div class="rel-item" v-for="(m, i) in reliability.models" :key="m.name">
            <span class="rank" :class="{ top: i === 0 }">{{ i + 1 }}</span>
            <span class="rel-name" :title="m.name">{{ m.name }}</span>
            <span class="rel-calls">{{ fmt(m.calls) }} {{ $t('dashboard.callsUnit') }}</span>
            <span class="rel-track">
              <span class="rel-fill" :style="{ width: m.success_rate + '%', background: rateColor(m.success_rate) }"></span>
            </span>
            <span class="rel-rate" :style="{ color: rateText(m.success_rate) }">{{ m.success_rate.toFixed(1) }}%</span>
            <span class="rel-lat">{{ fmtLatency(m.avg_latency_ms) }}</span>
          </div>
        </div>
        <div v-else class="empty small">{{ $t('dashboard.reliabilityEmpty') }}</div>
        <p class="footnote">
          {{ $t('dashboard.modelFootnote', { n: reliability.min_calls }) }}
        </p>
      </div>
    </div>

    <!-- ── 最近用量 ── -->
    <div class="card table-card">
      <div class="card-head">
        <h3>{{ $t('dashboard.recentUsage') }}</h3>
        <span class="usage-meta">{{ $t('dashboard.usageSummary', { count: fmt(stats.total), today: fmt(stats.today) }) }}</span>
      </div>
      <table class="usage-table">
        <thead>
          <tr>
            <th>{{ $t('dashboard.time') }}</th>
            <th>{{ $t('dashboard.provider') }}</th>
            <th>{{ $t('dashboard.model') }}</th>
            <th class="num">Token</th>
            <th>{{ $t('dashboard.status') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in recent" :key="row.id">
            <td>{{ row.time }}</td>
            <td>{{ row.provider }}</td>
            <td class="model">{{ row.model }}</td>
            <td class="num tok">{{ fmt(row.tokens) }}</td>
            <td>
              <span class="pill" :class="row.success ? 'pill-ok' : 'pill-fail'">
                {{ row.success ? $t('dashboard.success') : $t('dashboard.failed') }}
              </span>
            </td>
          </tr>
          <tr v-if="!recent.length">
            <td colspan="5" class="empty small">{{ $t('dashboard.noRecords') }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import api from '../api'

const { t } = useI18n()

// ── 主卡图标(inline SVG, 蓝色 = 重点口径 / 灰色 = 累计口径) ──
const icoCoin = `<svg width="20" height="20" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="7" stroke="#1677FF" stroke-width="1.8"/><path d="M6.5 10h7M10 6.5c-1.8 2-1.8 5 0 7" stroke="#1677FF" stroke-width="1.5" stroke-linecap="round"/></svg>`
const icoBars = `<svg width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M4 16V9M8.7 16V4M13.4 16v-6M18 16V7" stroke="#6E6E73" stroke-width="2" stroke-linecap="round"/></svg>`
const icoDoc = `<svg width="20" height="20" viewBox="0 0 20 20" fill="none"><rect x="3" y="4" width="14" height="12" rx="2" stroke="#1677FF" stroke-width="1.8"/><path d="M6 8h5M6 11h8" stroke="#1677FF" stroke-width="1.5" stroke-linecap="round"/></svg>`
const icoWave = `<svg width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M3 15c3-1 4-8 7-8s4 7 7 8" stroke="#6E6E73" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>`
const icoWrap = (svg, bg) => `<span style="display:inline-flex;width:36px;height:36px;align-items:center;justify-content:center;background:${bg};border-radius:10px;">${svg}</span>`
const icoCoinW = icoWrap(icoCoin, 'var(--gw-primary-50)')
const icoBarsW = icoWrap(icoBars, 'var(--gw-muted)')
const icoDocW = icoWrap(icoDoc, 'var(--gw-primary-50)')
const icoWaveW = icoWrap(icoWave, 'var(--gw-muted)')

const stats = ref({
  total: 0, today: 0, totalTokens: 0, todayTokens: 0,
  todayChars: 0, totalChars: 0, failed: 0, models: 0, providers: 0, groups: 0,
})

// ── 可靠性排行(真实调用口径, 排除 speed_test 探测) ──
const reliability = ref({ providers: [], models: [], excluded_probes: 0, min_calls: 10 })
const bestProvider = computed(() => reliability.value.providers[0] || null)
const bestModel = computed(() => reliability.value.models[0] || null)

function rateColor(r) {
  if (r >= 95) return '#22C55E'
  if (r >= 90) return '#F59E0B'
  return '#EF4444'
}
function rateText(r) {
  if (r >= 95) return '#16A34A'
  if (r >= 90) return '#B45309'
  return '#B91C1C'
}
function fmtLatency(ms) {
  if (ms == null) return '—'
  return (ms / 1000).toFixed(1) + 's'
}

// ── 趋势图 ──
const range = ref('14d')
const trendData = ref([])      // 统一为 { label, calls, tokens, chars, isLast }
const ranges = computed(() => [
  { key: '14d', label: t('dashboard.range14d') },
  { key: '30d', label: t('dashboard.range30d') },
  { key: '12m', label: t('dashboard.range12m') },
])

async function loadTrend() {
  try {
    if (range.value === '12m') {
      const resp = await api.get('/api/usage/monthly?months=12')
      const list = resp.data.data || []
      trendData.value = list.map((d, i) => ({
        label: d.month.slice(5).replace(/^0/, '') + t('dashboard.monthUnit'),
        calls: d.calls, tokens: d.tokens, chars: d.chars, isLast: i === list.length - 1,
      }))
    } else {
      const days = range.value === '30d' ? 30 : 14
      const resp = await api.get(`/api/usage/daily?days=${days}`)
      const list = resp.data.data || []
      trendData.value = list.map((d, i) => ({
        label: d.date.slice(5),
        calls: d.calls, tokens: d.tokens, chars: d.chars, isLast: i === list.length - 1,
      }))
    }
  } catch (e) { /* 趋势加载失败保持空图 */ }
}
function setRange(key) {
  if (range.value === key) return
  range.value = key
  loadTrend()
}

// 环比昨日: 用 14d 日数据的最后两天
const tokDelta = computed(() => pctDelta('tokens'))
const charDelta = computed(() => pctDelta('chars'))
function pctDelta(field) {
  const d = trendData.value
  if (range.value !== '14d' || d.length < 2) return null
  const today = d[d.length - 1][field]
  const yesterday = d[d.length - 2][field]
  if (!yesterday) return null
  return Math.round((today - yesterday) / yesterday * 1000) / 10
}

// 双轴 SVG 柱状图几何计算
const chart = computed(() => {
  const data = trendData.value
  if (!data.length) return null
  const W = 1000, H = 230, padL = 48, padR = 56, padT = 12, padB = 26
  const plotW = W - padL - padR
  const plotH = H - padT - padB
  const niceMax = (v) => {
    if (v <= 0) return 1
    const pow = Math.pow(10, Math.floor(Math.log10(v)))
    const n = v / pow
    const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
    return step * pow
  }
  const tokMax = niceMax(Math.max(...data.map(d => d.tokens), 1))
  const charMax = niceMax(Math.max(...data.map(d => d.chars), 1))
  const group = plotW / data.length
  const barW = Math.max(4, Math.min(22, group * 0.26))
  const bars = data.map((d, i) => {
    const cx = padL + group * i + group / 2
    const th = Math.max(d.tokens > 0 ? 2 : 0, Math.round(d.tokens / tokMax * plotH))
    const ch = Math.max(d.chars > 0 ? 2 : 0, Math.round(d.chars / charMax * plotH))
    return {
      x1: +(cx - barW - 1).toFixed(1), x2: +(cx + 1).toFixed(1),
      tokY: padT + plotH - th, tokH: th,
      charsY: padT + plotH - ch, charsH: ch,
      isLast: d.isLast,
    }
  })
  const grid = [], yLabels = [], yrLabels = []
  for (let i = 0; i <= 4; i++) {
    const y = padT + plotH - Math.round(plotH * i / 4)
    grid.push({ y, major: i === 0 })
    yLabels.push({ y, text: fmtAxis(tokMax * i / 4) })
    yrLabels.push({ y, text: fmtAxis(charMax * i / 4) })
  }
  const step = Math.ceil(data.length / 8)
  const xLabels = []
  data.forEach((d, i) => {
    if (i % step === 0 || d.isLast) {
      xLabels.push({
        x: +(padL + group * i + group / 2).toFixed(1),
        text: d.isLast && range.value !== '12m' ? t('dashboard.today') : d.label,
        today: d.isLast,
      })
    }
  })
  return { W, H, padL, padR, plotW, barW, grid, yLabels, yrLabels, bars, xLabels }
})

function fmtAxis(v) {
  if (v >= 1000000) return trimNum(v / 1000000) + 'M'
  if (v >= 1000) return trimNum(v / 1000) + 'k'
  return String(Math.round(v))
}
function trimNum(n) {
  const s = n.toFixed(1)
  return s.endsWith('.0') ? s.slice(0, -2) : s
}

// ── 次要指标条 ──
const chips = computed(() => [
  { key: 'today', label: t('dashboard.chipCallsToday'), value: fmt(stats.value.today), color: 'var(--gw-primary)' },
  { key: 'total', label: t('dashboard.chipCallsTotal'), value: fmt(stats.value.total), color: '#8B5CF6' },
  { key: 'failed', label: t('dashboard.chipFailed'), value: fmt(stats.value.failed), color: '#EF4444' },
  { key: 'models', label: t('dashboard.chipModels'), value: fmt(stats.value.models), color: '#22C55E' },
  { key: 'groups', label: t('dashboard.chipGroups'), value: fmt(stats.value.groups), color: '#F59E0B' },
])

// ── 最近用量 ──
const recent = ref([])

function fmt(n) {
  return (n || 0).toLocaleString()
}

// 仅用于「总 Token / 总字符」主卡: 超过 1 亿以「亿」为单位, 其余千分位原样
function fmtBig(n) {
  n = n || 0
  if (n >= 100000000) {
    const s = (n / 100000000).toFixed(1)
    return s.replace(/\.0$/, '') + '亿'
  }
  return n.toLocaleString()
}

onMounted(async () => {
  await loadTrend()
  await Promise.allSettled([
    (async () => {
      const resp = await api.get('/api/usage/summary')
      const d = resp.data
      stats.value.total = d.total || 0
      stats.value.today = d.today || 0
      stats.value.totalTokens = d.total_tokens || 0
      stats.value.todayTokens = d.today_tokens || 0
      stats.value.todayChars = d.today_chars || 0
      stats.value.totalChars = d.total_chars || 0
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
    (async () => {
      // 可靠性排行: 后端排除 speed_test 探测, 样本 <10 次不进榜
      const resp = await api.get('/api/usage/reliability?min_calls=10&limit=5')
      reliability.value = resp.data || reliability.value
    })(),
    (async () => {
      const resp = await api.get('/api/usage?limit=8')
      recent.value = (resp.data.data || [])
    })(),
  ])
})
</script>

<style scoped>
.dash { display: flex; flex-direction: column; gap: 16px; }

/* ── 主卡 ── */
.hero-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.hero-card {
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: 12px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.hero-head { display: flex; align-items: center; justify-content: space-between; }
.hero-label { font-size: 13px; color: var(--gw-muted-foreground); }
.hero-ico { display: inline-flex; width: 36px; height: 36px; align-items: center; justify-content: center; }
.hero-ico :deep(svg) { width: 20px; height: 20px; }
.hero-value {
  font-size: 30px;
  font-weight: 700;
  letter-spacing: -0.5px;
  color: var(--gw-card-foreground);
  word-break: break-all;
  line-height: 1.1;
}
.hero-foot { display: flex; align-items: center; gap: 8px; }
.hero-caption { font-size: 12px; color: var(--gw-muted-foreground); }
.badge {
  display: inline-flex; align-items: center;
  height: 22px; padding: 0 8px;
  border-radius: 11px;
  font-size: 11px; font-weight: 600;
}
.badge-up { background: var(--gw-state-success-bg); color: var(--gw-state-success-fg); }
.badge-down { background: var(--gw-state-error-bg); color: var(--gw-state-error-fg); }

/* ── 次要指标条 ── */
.chip-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
.chip {
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: 10px;
  height: 44px;
  padding: 0 14px;
  display: flex; align-items: center; gap: 8px;
}
.chip-dot { width: 8px; height: 8px; border-radius: 4px; flex: none; }
.chip-label { font-size: 12px; color: var(--gw-muted-foreground); }
.chip-val { margin-left: auto; font-size: 14px; font-weight: 600; color: var(--gw-card-foreground); }

/* ── 通用卡片 ── */
.card {
  background: var(--gw-card);
  border: 1px solid var(--gw-border);
  border-radius: 12px;
  padding: 24px;
}
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.card-head h3 { margin: 0; font-size: 16px; font-weight: 600; color: var(--gw-card-foreground); }
.card-head .sub { margin: 4px 0 0; font-size: 12px; color: var(--gw-muted-foreground); }
.usage-meta { font-size: 12px; color: var(--gw-muted-foreground); }

/* ── 趋势图 ── */
.legend { display: flex; align-items: center; gap: 16px; }
.legend-item { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--gw-muted-foreground); }
.legend-dot { width: 10px; height: 10px; border-radius: 3px; }
.range-tabs {
  display: flex; gap: 2px;
  background: var(--gw-muted);
  border-radius: 8px; padding: 3px;
}
.range-tab {
  border: 0; background: transparent;
  height: 24px; padding: 0 10px;
  border-radius: 6px;
  font-size: 12px; color: var(--gw-muted-foreground);
  cursor: pointer;
}
.range-tab.active {
  background: var(--gw-card);
  color: var(--gw-card-foreground);
  font-weight: 500;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
}
.trend-svg { width: 100%; height: auto; margin-top: 16px; }
.axis-text { font-size: 10px; fill: #86868B; font-family: inherit; }
.axis-today { font-weight: 600; fill: #0E5FD8; }

/* ── 可靠性排行 ── */
.rel-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.rel-card { display: flex; flex-direction: column; gap: 12px; padding-bottom: 16px; }
.best-pill {
  flex: none;
  display: inline-flex; align-items: center;
  height: 24px; padding: 0 10px;
  border-radius: 12px;
  background: var(--gw-primary-50);
  color: var(--gw-primary);
  font-size: 11px; font-weight: 600;
  max-width: 260px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rel-list { display: flex; flex-direction: column; }
.rel-item {
  display: flex; align-items: center; gap: 12px;
  height: 44px;
}
.rank { width: 20px; flex: none; font-size: 13px; font-weight: 500; color: #86868B; }
.rank.top { font-weight: 700; color: var(--gw-primary); }
.rel-name {
  width: 130px; flex: none;
  font-size: 13px; font-weight: 500; color: var(--gw-card-foreground);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rel-calls { width: 72px; flex: none; text-align: right; font-size: 12px; color: var(--gw-muted-foreground); }
.rel-track {
  flex: 1; height: 8px; border-radius: 4px;
  background: var(--gw-muted);
  overflow: hidden;
}
.rel-fill { display: block; height: 100%; border-radius: 4px; }
.rel-rate { width: 52px; flex: none; text-align: right; font-size: 13px; font-weight: 600; }
.rel-lat { width: 48px; flex: none; text-align: right; font-size: 12px; color: var(--gw-muted-foreground); }
.footnote {
  margin: 0; padding-top: 8px;
  font-size: 11px; color: #86868B;
  border-top: 1px dashed var(--gw-border);
}

/* ── 最近用量表 ── */
.table-card { padding: 0; overflow: hidden; }
.table-card .card-head { padding: 20px 24px 0; }
.usage-table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; }
.usage-table th {
  text-align: left;
  font-size: 12px; font-weight: 500;
  color: var(--gw-muted-foreground);
  background: #FAFAFA;
  padding: 12px 24px;
  border-bottom: 1px solid var(--gw-border);
}
.usage-table td {
  padding: 14px 24px;
  border-bottom: 1px solid var(--gw-border);
  color: var(--gw-card-foreground);
}
.usage-table tr:last-child td { border-bottom: 0; }
.usage-table .num { text-align: right; }
.usage-table .tok { color: var(--gw-primary); font-weight: 500; }
.usage-table .model { font-weight: 500; }
.pill {
  display: inline-flex; align-items: center;
  height: 22px; padding: 0 8px;
  border-radius: 11px;
  font-size: 11px; font-weight: 500;
}
.pill-ok { background: var(--gw-state-success-bg); color: var(--gw-state-success-fg); }
.pill-fail { background: var(--gw-state-error-bg); color: var(--gw-state-error-fg); }

.empty { padding: 32px 0; text-align: center; font-size: 13px; color: var(--gw-muted-foreground); }
.empty.small { padding: 16px 0; }

/* 窄屏: 主卡 2 列, 排行单列 */
@media (max-width: 1100px) {
  .hero-row { grid-template-columns: repeat(2, 1fr); }
  .chip-row { grid-template-columns: repeat(2, 1fr); }
  .rel-row { grid-template-columns: 1fr; }
}
</style>
