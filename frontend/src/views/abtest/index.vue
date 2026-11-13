<template>
  <section class="page" data-module="abtest">
    <header class="page-head">
      <div>
        <h2>实验设计管理</h2>
        <p class="page-desc">设计A/B实验方案，定义对照组与实验组、分流比例、核心评估指标和样本量要求。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记实验方案</button>
        <button class="btn" type="button" @click="exportRows">导出实验设计清单</button>
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
          <td :colspan="columns.length + 2" class="empty-state">暂无实验设计数据，可先登记实验方案</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条实验设计记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="detail" class="overlay" @click.self="detail = null">
      <aside class="panel">
        <h3>实验详情 · {{ detail.entry['方案编号'] }}</h3>
        <dl class="detail-grid">
          <template v-for="field in detailFields" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ detail.entry[field] ?? '—' }}</dd>
          </template>
          <dt>当前状态</dt>
          <dd>{{ detail.entry.status }}</dd>
        </dl>
        <p v-if="detail.gate && !detail.gate.ok" class="gate-gaps">
          结项门禁缺口：{{ detail.gate.gaps.join('；') }}
        </p>
        <p v-else-if="detail.gate" class="gate-ok">结项门禁已通过，可执行结项实验。</p>
        <h4>结项记录（只增不改，可追溯当时阈值）</h4>
        <table class="data-table" v-if="detail.closures.length">
          <thead>
            <tr>
              <th v-for="column in closureColumns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="closure in detail.closures" :key="String(closure.id)">
              <td v-for="column in closureColumns" :key="column">{{ closure[column] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-state">尚无结项记录</p>
        <button class="btn" type="button" @click="detail = null">关闭</button>
      </aside>
    </div>

    <div v-if="createVisible" class="overlay" @click.self="createVisible = false">
      <form class="panel" @submit.prevent="submitCreate">
        <h3>登记实验方案</h3>
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
  gate: { ok: boolean; gaps: string[] }
  closures: Row[]
}

const ENDPOINT = '/api/abtest'
const columns = ["方案编号", "实验名称", "对照组定义", "实验组定义", "分流比例", "核心指标", "最小样本量", "当前样本量", "预计周期", "绑定分析方案", "结论版本"]
const actions = ["提交方案评审", "启动实验", "结项实验", "取消实验"]
const statuses = ["草稿", "方案评审中", "实验中", "已结项", "已取消"]
const stats = [{"label": "实验方案总数", "value": 0}, {"label": "实验中方案", "value": 0}, {"label": "已结项方案", "value": 0}]
const detailFields = columns
const closureColumns = ["结项编号", "结论版本", "指标版本", "显著性结论", "显著性阈值", "配置哈希", "最小样本量", "结项样本量", "操作人", "结项时间"]
const createFields = [
  { name: "方案编号", required: true, placeholder: "如 ABTE-0007" },
  { name: "实验名称", required: true, placeholder: "实验名称" },
  { name: "对照组定义", required: true, placeholder: "对照组流量与版本" },
  { name: "实验组定义", required: false, placeholder: "实验组流量与版本" },
  { name: "分流比例", required: false, placeholder: "如 50/50，各组之和须为 100" },
  { name: "核心指标", required: false, placeholder: "如 METR-CONVERSION" },
  { name: "最小样本量", required: false, placeholder: "如 15000" },
  { name: "当前样本量", required: false, placeholder: "如 12000" },
  { name: "预计周期", required: false, placeholder: "如 2026-11-30" },
  { name: "绑定分析方案", required: false, placeholder: "如 ANAL-CONVERSION" },
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
      throw new Error(payload.message || '实验方案登记失败')
    }
    createVisible.value = false
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验方案登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, operator: session.operator } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      // 门禁未通过等服务端说明要原样展示，不能笼统提示重试
      errorMessage.value = payload.message || '实验设计动作未生效'
      return
    }
    noticeMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验设计操作失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('实验详情读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验详情读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('实验方案列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '实验设计列表读取失败'
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
  width: 520px;
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
.gate-gaps { color: #b42318; font-size: 13px; }
.gate-ok { color: #067647; font-size: 13px; }
.form-item span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.form-item input { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.panel-actions { display: flex; gap: 8px; }
.notice-text { color: #067647; }
</style>
