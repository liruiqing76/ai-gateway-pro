<template>
  <div class="free-model-search">
    <h2 style="font-size:18px;font-weight:600;margin-bottom:16px">免费模型搜索</h2>

    <!-- 统计卡片 -->
    <el-row :gutter="16" style="margin-bottom:16px">
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-num">{{ stats.total_posts || 0 }}</div>
          <div class="stat-label">帖子总数</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-num">{{ stats.total_reports || 0 }}</div>
          <div class="stat-label">报告数</div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">各来源帖子数</div>
          <div class="source-stats" v-if="stats.by_source">
            <el-tag v-for="(count, source) in stats.by_source" :key="source" class="source-tag">
              {{ source }}: {{ count }}
            </el-tag>
            <span v-if="!Object.keys(stats.by_source).length" style="color:#999">暂无数据</span>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 操作栏 -->
    <div style="margin-bottom:16px;display:flex;gap:12px;align-items:center">
      <el-button type="primary" :loading="searching" @click="triggerSearch">
        {{ searching ? '搜索中...' : '立即搜索' }}
      </el-button>
      <el-button @click="loadStats">刷新统计</el-button>
      <el-tag v-if="runtime.running" type="warning">后台任务运行中</el-tag>
      <span v-if="runtime.last_search_at" class="last-run">
        上次运行: {{ runtime.last_search_at }}
      </span>
    </div>

    <!-- 报告列表 -->
    <el-card shadow="hover">
      <template #header><span class="card-title">历史报告</span></template>
      <el-table :data="reports" style="width:100%" v-loading="loadingReports" empty-text="暂无报告">
        <el-table-column prop="run_date" label="日期" width="120" />
        <el-table-column prop="post_count" label="帖子数" width="90" />
        <el-table-column label="可行性" width="100">
          <template #default="{row}">
            <span v-if="row.feasibility_score !== null">{{ row.feasibility_score.toFixed(2) }}</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="真实性" width="100">
          <template #default="{row}">
            <span v-if="row.truthfulness_score !== null">{{ row.truthfulness_score.toFixed(2) }}</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{row}">
            <el-tag :type="row.status === 'completed' ? 'success' : 'info'" size="small">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{row}">
            <el-button size="small" @click="viewReport(row.run_date)">查看</el-button>
            <el-button size="small" type="primary" link @click="downloadReport(row.run_date)">下载</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 报告详情对话框 -->
    <el-dialog v-model="detailVisible" title="报告详情" width="80%" top="5vh">
      <div v-if="reportDetail">
        <div style="margin-bottom:12px">
          <el-tag type="info">日期: {{ reportDetail.report.run_date }}</el-tag>
          <el-tag v-if="reportDetail.report.feasibility_score !== null" type="success" style="margin-left:8px">
            可行性: {{ reportDetail.report.feasibility_score.toFixed(2) }}
          </el-tag>
          <el-tag v-if="reportDetail.report.truthfulness_score !== null" type="warning" style="margin-left:8px">
            真实性: {{ reportDetail.report.truthfulness_score.toFixed(2) }}
          </el-tag>
        </div>

        <el-tabs v-model="activeTab">
          <el-tab-pane label="帖子列表" name="posts">
            <el-table :data="reportDetail.posts" style="width:100%;max-height:60vh;overflow:auto">
              <el-table-column prop="source" label="来源" width="90" />
              <el-table-column prop="title" label="标题" min-width="240">
                <template #default="{row}">
                  <a
                    v-if="row.url"
                    class="post-link"
                    :href="normalizeUrl(row.url)"
                    target="_blank"
                    rel="noopener noreferrer"
                    :title="row.title"
                  >{{ row.title }}</a>
                  <span v-else>{{ row.title }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="author" label="作者" width="120" show-overflow-tooltip />
              <el-table-column label="可行性/真实性" width="130">
                <template #default="{row}">
                  <span v-if="getEval(row.id)">
                    {{ getEval(row.id).feasibility_score }}/{{ getEval(row.id).truthfulness_score }}
                  </span>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="100">
                <template #default="{row}">
                  <el-button size="small" type="primary" link @click="openUrl(row.url)">打开</el-button>
                  <el-button size="small" type="danger" link @click="ignorePost(row.hash_key)" title="标记为无关内容">忽略</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="评估详情" name="evals">
            <el-table :data="reportDetail.evaluations" style="width:100%;max-height:60vh;overflow:auto">
              <el-table-column prop="post_id" label="帖子ID" width="90" />
              <el-table-column label="可行性" width="90">
                <template #default="{row}">{{ row.feasibility_score }}/5</template>
              </el-table-column>
              <el-table-column label="真实性" width="90">
                <template #default="{row}">{{ row.truthfulness_score }}/5</template>
              </el-table-column>
              <el-table-column prop="reason" label="理由" min-width="300" show-overflow-tooltip />
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="Markdown" name="md">
            <div
              class="md-preview md-rendered"
              v-if="reportMarkdown"
              v-html="reportMarkdownHtml"
            ></div>
            <div v-else style="color:#999">加载中...</div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

/* ---------- Markdown 轻量渲染（仅支持本报告用到的语法） ---------- */

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * 规范化 URL：
 * - 还原 escapeHtml 产生的实体
 * - 去掉 Markdown 尖括号包裹
 * - 裸空格转 %20（搜狗/百度结果常见，不处理会被当成链接结束）
 * - 只放行 http/https，挡掉 javascript: 等危险协议
 */
function normalizeUrl(raw) {
  let url = String(raw || '').trim()
  if (!url) return ''
  url = url.replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/&quot;/g, '"')
  url = url.replace(/^(?:&lt;|<)/, '').replace(/(?:&gt;|>)$/, '')
  url = url.replace(/\s+/g, '%20')
  if (!/^https?:\/\//i.test(url)) return ''
  return url
}

function linkHtml(text, rawUrl) {
  const url = normalizeUrl(rawUrl)
  const label = text || url
  if (!url) return label
  const warn = /weixin\.sogou\.com/i.test(url)
    ? ' title="搜狗微信中转链接，token 可能已过期，若跳转失败请反查原文"'
    : ''
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="md-link"${warn}>${label}</a>`
}

function inline(text) {
  const stash = []
  const push = (html) => {
    stash.push(html)
    return `\u0000${stash.length - 1}\u0000`
  }
  let s = String(text || '')
  // [标题](<url>) / [标题](url) —— 支持 URL 内一层括号，且允许空格（靠右侧 ) 收尾）
  s = s.replace(/\[([^\]]*)\]\((?:&lt;)?((?:[^()]|\([^)]*\))*)(?:&gt;)?\)/g, (m, t, u) => push(linkHtml(t || u, u)))
  // 裸 URL（转义后 < > 已变成实体，不会误伤已生成的 a 标签）
  s = s.replace(/(https?:\/\/[^\s\u0000]+)/g, (m, u) => push(linkHtml(u, u)))
  // 粗体
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => stash[Number(i)])
}

function renderMarkdown(md) {
  if (!md) return ''
  const lines = escapeHtml(md).split('\n')
  const out = []
  let inList = false
  const closeList = () => {
    if (inList) {
      out.push('</ul>')
      inList = false
    }
  }
  for (const line of lines) {
    const heading = line.match(/^(#{1,6})\s+(.*)$/)
    if (heading) {
      closeList()
      const lv = heading[1].length
      out.push(`<h${lv}>${inline(heading[2])}</h${lv}>`)
      continue
    }
    if (/^\s*[-*]\s+/.test(line)) {
      if (!inList) {
        out.push('<ul>')
        inList = true
      }
      out.push(`<li>${inline(line.replace(/^\s*[-*]\s+/, ''))}</li>`)
      continue
    }
    closeList()
    if (line.trim()) out.push(`<p>${inline(line)}</p>`)
  }
  closeList()
  return out.join('')
}

const stats = ref({})
const reports = ref([])
const runtime = ref({})
const searching = ref(false)
const loadingReports = ref(false)
const detailVisible = ref(false)
const reportDetail = ref(null)
const reportMarkdown = ref('')
const activeTab = ref('posts')
const reportMarkdownHtml = computed(() => renderMarkdown(reportMarkdown.value))

onMounted(() => {
  loadStats()
  loadReports()
})

async function loadStats() {
  try {
    const resp = await api.get('/api/free-model-search/stats')
    if (resp.data.ok) {
      stats.value = resp.data
      runtime.value = resp.data.runtime || {}
    }
  } catch (e) {
    console.error('加载统计失败', e)
  }
}

async function loadReports() {
  loadingReports.value = true
  try {
    const resp = await api.get('/api/free-model-search/reports?limit=50')
    if (resp.data.ok) {
      reports.value = resp.data.data || []
    }
  } catch (e) {
    console.error('加载报告列表失败', e)
  } finally {
    loadingReports.value = false
  }
}

async function triggerSearch() {
  searching.value = true
  try {
    const resp = await api.post('/api/free-model-search/search')
    if (resp.data.ok) {
      ElMessage.success(`搜索完成: 发现${resp.data.total_found}条, 新增${resp.data.added}条, 评估${resp.data.evaluated}条`)
      loadStats()
      loadReports()
    } else {
      ElMessage.error('搜索失败: ' + (resp.data.error || '未知错误'))
    }
  } catch (e) {
    ElMessage.error('搜索请求失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    searching.value = false
  }
}

async function viewReport(date) {
  detailVisible.value = true
  reportDetail.value = null
  reportMarkdown.value = ''
  try {
    const resp = await api.get(`/api/free-model-search/report/${date}`)
    if (resp.data.ok) {
      reportDetail.value = resp.data
      // 加载 markdown
      const mdResp = await api.get(`/api/free-model-search/report/${date}/file`, { responseType: 'text' })
      reportMarkdown.value = mdResp.data
    }
  } catch (e) {
    ElMessage.error('加载报告详情失败: ' + (e.response?.data?.detail || e.message))
  }
}

function getEval(postId) {
  if (!reportDetail.value || !reportDetail.value.evaluations) return null
  return reportDetail.value.evaluations.find(e => e.post_id === postId) || null
}

function openUrl(url) {
  const safe = normalizeUrl(url)
  if (safe) {
    window.open(safe, '_blank', 'noopener,noreferrer')
  } else {
    ElMessage.warning('链接无效或缺少 http(s) 协议，无法打开')
  }
}

async function ignorePost(hash) {
  if (!hash || hash.length !== 32) {
    ElMessage.error('无效的帖子标识');
    return;
  }
  try {
    await api.post(`/api/free-model-search/post/${hash}/ignore`);
    ElMessage.success('已标记为无关内容');
    // 刷新当前报告以移除已忽略的帖子
    if (reportDetail.value) {
      await viewReport(reportDetail.value.report.run_date);
    }
  } catch (e) {
    ElMessage.error('操作失败: ' + (e.response?.data?.message || e.message));
  }
}

function downloadReport(date) {
  window.open(`/api/free-model-search/report/${date}/file`, '_blank')
}
</script>

<style scoped>
.stat-card {
  text-align: center;
}
.stat-num {
  font-size: 28px;
  font-weight: 700;
  color: var(--gw-primary, #409eff);
}
.stat-label {
  font-size: 13px;
  color: var(--gw-text-secondary, #64748b);
  margin-top: 4px;
}
.source-stats {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.source-tag {
  margin: 0;
}
.last-run {
  font-size: 12px;
  color: var(--gw-text-secondary, #64748b);
}
.md-preview {
  background: var(--gw-muted, #f5f7fa);
  padding: 16px;
  border-radius: 8px;
  font-family: var(--gw-font-mono, monospace);
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  max-height: 60vh;
  overflow: auto;
}

/* 渲染后的 Markdown：覆盖 pre 的等宽/保留空白行为 */
.md-rendered {
  font-family: inherit;
  white-space: normal;
  line-height: 1.75;
}
.md-rendered :deep(h1) { font-size: 20px; font-weight: 600; margin: 4px 0 12px; }
.md-rendered :deep(h2) {
  font-size: 16px; font-weight: 600; margin: 18px 0 8px;
  padding-left: 8px; border-left: 3px solid var(--gw-primary, #409eff);
}
.md-rendered :deep(h3) { font-size: 14px; font-weight: 600; margin: 12px 0 4px; }
.md-rendered :deep(ul) { margin: 6px 0 12px; padding-left: 20px; }
.md-rendered :deep(li) { margin: 4px 0; }
.md-rendered :deep(p) { margin: 6px 0; }
.md-rendered :deep(.md-link),
.post-link {
  color: var(--gw-primary, #409eff);
  text-decoration: none;
  cursor: pointer;
}
.md-rendered :deep(.md-link:hover),
.post-link:hover {
  text-decoration: underline;
}
</style>
