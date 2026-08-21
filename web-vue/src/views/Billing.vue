<template>
  <div>
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">{{ $t('billing.title') }}</h2>

    <el-row :gutter="16" style="margin-bottom:20px">
      <el-col :span="12">
        <el-card shadow="hover">
          <div class="stat-icon">📦</div>
          <div class="stat-value">{{ tierLabel }}</div>
          <div class="stat-label">{{ $t('billing.currentPlan') }}</div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="hover">
          <div class="stat-icon">🎯</div>
          <div class="stat-value">{{ remaining }}</div>
          <div class="stat-label">{{ $t('billing.dailyRemaining') }}</div>
        </el-card>
      </el-col>
    </el-row>

    <h3 style="font-size:14px;font-weight:600;margin-bottom:12px">{{ $t('billing.plans') }}</h3>
    <el-row :gutter="16" style="margin-bottom:20px">
      <el-col :span="8" v-for="p in plans" :key="p.plan">
        <el-card shadow="hover" style="position:relative">
          <el-tag v-if="p.plan === currentTier" type="primary" size="small" style="position:absolute;top:12px;right:12px">{{ $t('billing.current') }}</el-tag>
          <div class="stat-icon">{{ p.plan === 'free' ? '🆓' : p.plan === 'pro' ? '⭐' : '🏢' }}</div>
          <div style="font-size:20px;font-weight:700;margin:8px 0">{{ p.price_cents === 0 ? $t('billing.free') : $t('billing.perMonth', { price: (p.price_cents / 100).toFixed(0) }) }}</div>
          <div style="font-size:12px;color:var(--gw-text-secondary);margin-bottom:12px">{{ p.features.join(' · ') }}</div>
          <el-button v-if="p.plan !== currentTier && p.plan !== 'free'" type="primary" size="small" @click="placeOrder(p.plan)">{{ $t('billing.upgrade') }}</el-button>
        </el-card>
      </el-col>
    </el-row>

    <h3 style="font-size:14px;font-weight:600;margin-bottom:12px">{{ $t('billing.orders') }}</h3>
    <el-card shadow="hover">
      <el-table :data="orders" :empty-text="$t('billing.noOrders')" v-loading="loading">
        <el-table-column prop="order_no" :label="$t('billing.orderNo')" width="200" />
        <el-table-column prop="plan_name" :label="$t('billing.plan')" width="120" />
        <el-table-column :label="$t('billing.amount')" width="100">
          <template #default="{ row }">¥{{ (row.amount_cents / 100).toFixed(2) }}</template>
        </el-table-column>
        <el-table-column :label="$t('billing.status')" width="100">
          <template #default="{ row }">
            <el-tag :type="row.payment_status === 'paid' ? 'success' : 'warning'" size="small">
              {{ row.payment_status === 'paid' ? $t('billing.paid') : $t('billing.pending') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('billing.time')" width="180">
          <template #default="{ row }">{{ row.created_at ? new Date(row.created_at).toLocaleString() : '-' }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import api from '../api'

const { t } = useI18n()

const loading = ref(false)
const currentTier = ref('free')
const remaining = ref(0)
const tierLabel = ref('')
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
    tierLabel.value = me.tier === 'free' ? t('dashboard.tierFree') : me.tier === 'pro' ? t('dashboard.tierPro') : t('dashboard.tierEnterprise')
    plans.value = plansResp.data
    orders.value = ordersResp.data
  } catch (e) { /* */ }
}

async function placeOrder(plan) {
  try {
    await ElMessageBox.confirm(t('billing.upgradeConfirm', { plan }), t('common.confirm'), { type: 'info' })
    const resp = await api.post('/api/billing/orders', { plan_name: plan })
    ElMessage.success(t('billing.orderCreated', { orderNo: resp.data.order_no }))
    load()
  } catch (e) { /* cancelled */ }
}

onMounted(load)
</script>
