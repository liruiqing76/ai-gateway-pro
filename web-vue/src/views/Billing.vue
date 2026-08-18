<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">计费中心</h2>

    <el-row :gutter="16" style="margin-bottom:20px">
      <el-col :span="12">
        <el-card shadow="hover">
          <div class="stat-icon">📦</div>
          <div class="stat-value">{{ tierLabel }}</div>
          <div class="stat-label">当前套餐</div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="hover">
          <div class="stat-icon">🎯</div>
          <div class="stat-value">{{ remaining }}</div>
          <div class="stat-label">今日剩余额度</div>
        </el-card>
      </el-col>
    </el-row>

    <h3 style="font-size:14px;font-weight:600;margin-bottom:12px">定价计划</h3>
    <el-row :gutter="16" style="margin-bottom:20px">
      <el-col :span="8" v-for="p in plans" :key="p.plan">
        <el-card shadow="hover" style="position:relative">
          <el-tag v-if="p.plan === currentTier" type="primary" size="small" style="position:absolute;top:12px;right:12px">当前</el-tag>
          <div class="stat-icon">{{ p.plan === 'free' ? '🆓' : p.plan === 'pro' ? '⭐' : '🏢' }}</div>
          <div style="font-size:20px;font-weight:700;margin:8px 0">{{ p.price_cents === 0 ? '免费' : '¥' + (p.price_cents / 100).toFixed(0) + '/月' }}</div>
          <div style="font-size:12px;color:#94a3b8;margin-bottom:12px">{{ p.features.join(' · ') }}</div>
          <el-button v-if="p.plan !== currentTier && p.plan !== 'free'" type="primary" size="small" @click="placeOrder(p.plan)">升级</el-button>
        </el-card>
      </el-col>
    </el-row>

    <h3 style="font-size:14px;font-weight:600;margin-bottom:12px">订单记录</h3>
    <el-card shadow="hover">
      <el-table :data="orders" empty-text="暂无订单" v-loading="loading">
        <el-table-column prop="order_no" label="订单号" width="200" />
        <el-table-column prop="plan_name" label="计划" width="120" />
        <el-table-column label="金额" width="100">
          <template #default="{ row }">¥{{ (row.amount_cents / 100).toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.payment_status === 'paid' ? 'success' : 'warning'" size="small">
              {{ row.payment_status === 'paid' ? '已支付' : '待支付' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ row.created_at ? new Date(row.created_at).toLocaleString() : '-' }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const loading = ref(false)
const currentTier = ref('free')
const remaining = ref(0)
const tierLabel = ref('🆓 免费版')
const plans = ref([])
const orders = ref([])

async function load() {
  try {
    const [meResp, plansResp, ordersResp] = await Promise.all([
      api.get('/api/billing/me'),
      api.get('/api/billing/plans'),
      api.get('/api/billing/orders'),
    ])
    const me = meResp.data
    currentTier.value = me.tier
    remaining.value = Math.max(0, (me.quota_daily || 0) - (me.quota_used_today || 0))
    tierLabel.value = me.tier === 'free' ? '🆓 免费版' : me.tier === 'pro' ? '⭐ Pro' : '🏢 企业版'
    plans.value = plansResp.data
    orders.value = ordersResp.data
  } catch (e) { /* */ }
}

async function placeOrder(plan) {
  try {
    await ElMessageBox.confirm('确认升级到 ' + plan + '？', '确认', { type: 'info' })
    const resp = await api.post('/api/billing/orders', { plan_name: plan })
    ElMessage.success('订单已创建: ' + resp.data.order_no)
    load()
  } catch (e) { /* cancelled */ }
}

onMounted(load)
</script>