<template>
  <section class="page" data-module="analysis">
    <header class="page-head">
      <div>
        <h2>分析方案管理</h2>
        <p class="page-desc">定义数据分析方案，包括分析主题、方法选择、显著性检验和输出形式。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记分析方案</button>
        <button class="btn" type="button" @click="exportRows">导出分析方案清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无分析方案数据，可先登记分析方案</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条分析方案记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="detail" class="overlay" @click.self="detail = null">
      <aside class="panel">
        <h3>分析方案详情 · {{ detail.entry['方案编号'] }}</h3>
        <dl class="detail-grid">
          <template v-for="field in detailFields" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ detail.entry[field] ?? '—' }}</dd>
          </template>
          <dt>当前状态</dt>
          <dd>{{ detail.entry.status }}</dd>
        </dl>
        <h4>当前共享结论（与实验列表同一份版本）</h4>
        <dl v-if="detail.conclusion" class="detail-grid">
          <template v-for="field in conclusionFields" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ detail.conclusion[field] ?? '—' }}</dd>
          </template>
        </dl>
        <p v-else class="empty-state">尚无显著性结论</p>
        <h4>结论版本历史（旧版本不残留，只保留可追溯状态）</h4>
        <table class="data-table" v-if="detail.conclusions.length">
          <thead>
            <tr>
              <th v-for="column in conclusionColumns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in detail.conclusions" :key="String(item.id)">
              <td v-for="column in conclusionColumns" :key="column">{{ item[column] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-state">尚无结论版本</p>
        <h4>绑定本方案的实验</h4>
        <table class="data-table" v-if="detail.experiments.length">
          <thead>
            <tr>
              <th v-for="column in experimentColumns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in detail.experiments" :key="String(item.id)">
              <td v-for="column in experimentColumns" :key="column">{{ item[column] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-state">暂无实验绑定</p>
        <button class="btn" type="button" @click="detail = null">关闭</button>
      </aside>
    </div>

    <div v-if="createVisible" class="overlay" @click.self="createVisible = false">
      <form class="panel" @submit.prevent="submitCreate">
        <h3>登记分析方案</h3>
        <label v-for="field in createFields" :key="field.name" class="form-item">
          <span>{{ field.name }}{{ field.required ? '（必填）' : '' }}</span>
          <input v-model="createForm[field.name]" :placeholder="field.placeholder" />
        </label>
        <div class="panel-actions">
          <button class="btn primary" type="submit">提交登记</button>
          <button class="btn ghost" type="button" @click="createVisible = false">取消</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type Row = Record<string, string | number | null>
type Detail = {
  entry: Row
  conclusion: Row | null
  conclusions: Row[]
  experiments: Row[]
}

const ENDPOINT = '/api/analysis'
const columns = ["方案编号", "分析主题", "数据集范围", "分析方法", "对比基准", "显著性阈值", "输出图表类型", "调度周期", "核心指标", "结论版本", "显著性结论"]
const actions = ["提交方案评审", "启用方案", "生成显著性结论", "归档方案"]
const statuses = ["草稿", "方案评审中", "已启用", "已归档"]
const stats = [{"label": "分析方案总数", "value": 0}, {"label": "已启用方案", "value": 0}, {"label": "方案评审中", "value": 0}]
const detailFields = columns
const conclusionFields = ["结论编号", "结论版本", "核心指标", "指标版本", "显著性结论", "显著性阈值", "结论样本量", "配置哈希", "状态", "生成时间"]
const conclusionColumns = ["结论版本", "指标版本", "显著性结论", "显著性阈值", "结论样本量", "状态"]
const experimentColumns = ["方案编号", "实验名称", "结论版本", "当前样本量", "最小样本量"]
const createFields = [
  { name: "方案编号", required: true, placeholder: "如 ANAL-0004" },
  { name: "分析主题", required: true, placeholder: "分析主题" },
  { name: "数据集范围", required: true, placeholder: "数据集范围" },
  { name: "分析方法", required: false, placeholder: "如 双比例 z 检验" },
  { name: "对比基准", required: false, placeholder: "如 对照组转化率" },
  { name: "显著性阈值", required: false, placeholder: "冻结为 0.05" },
  { name: "输出图表类型", required: false, placeholder: "如 漏斗+趋势" },
  { name: "调度周期", required: false, placeholder: "如 每日" },
  { name: "核心指标", required: false, placeholder: "如 METR-CONVERSION" },
]

const session = useSessionStore()
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const detail = ref<Detail | null>(null)
const createVisible = ref(false)
const createForm = ref<Record<string, string>>({})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createForm.value = {}
  createVisible.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...createForm.value, operator: session.operator } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '分析方案登记失败')
    }
    createVisible.value = false
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分析方案登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  const values: Record<string, string> = { action, operator: session.operator }
  if (action === '生成显著性结论') {
    const verdict = window.prompt('显著性结论（显著 / 不显著）', '显著')
    if (verdict === null) {
      return
    }
    values['显著性结论'] = verdict.trim()
    const samples = window.prompt('结论样本量（留空则取绑定实验的当前样本量）', '')
    if (samples && samples.trim()) {
      values['结论样本量'] = samples.trim()
    }
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message || '分析方案动作未生效'
      return
    }
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分析方案操作失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('分析方案详情读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分析方案详情读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分析方案列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分析方案列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.35);
  display: flex;
  justify-content: flex-end;
  z-index: 20;
}
.panel {
  width: 560px;
  max-width: 90vw;
  height: 100%;
  overflow: auto;
  background: #fff;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.detail-grid {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 6px 10px;
  margin: 0;
  font-size: 13px;
}
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
.form-item span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.form-item input { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.panel-actions { display: flex; gap: 8px; }
.notice-text { color: #067647; }
</style>
