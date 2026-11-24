<template>
  <section class="report-panel" data-testid="report-section-panel">
    <header class="panel-head">
      <h3>报告数据截面</h3>
      <p class="panel-desc">
        多指标一次合并读取；同一份报告的指标必须来自同一轮，新一轮没写齐时整体回退到上一完整轮。
      </p>
    </header>

    <form class="panel-form" @submit.prevent="load">
      <label>
        <span>报告编号</span>
        <input v-model="reportId" data-testid="section-report-id" placeholder="如 1" />
      </label>
      <button class="btn" data-testid="section-load" type="submit">刷新数据截面</button>
      <button class="btn" type="button" :disabled="!section" @click="generate">重新生成报告</button>
      <button class="btn primary" type="button" :disabled="!section" @click="saveSnapshot">
        保存日报快照
      </button>
    </form>

    <template v-if="section">
      <div class="panel-meta">
        <span data-testid="section-round">轮次：{{ section.round_id ?? '—' }}</span>
        <span data-testid="section-mode">取数方式：{{ modeLabel(section.data_mode) }}</span>
        <span data-testid="section-cutoff">截止：{{ section.cutoff_at ?? '—' }}</span>
        <span data-testid="section-waited">
          {{ section.waited_for_round ? '已等到完整新一轮' : '未等到新一轮，沿用上一完整轮' }}
        </span>
        <span v-if="section.pending_round_id" class="warn" data-testid="section-pending">
          待完成轮次 {{ section.pending_round_id }}（{{ section.pending_round_status }}）
        </span>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>指标编号</th><th>指标</th><th>数值</th><th>所属轮次</th><th>来源行</th><th>数据时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in section.sections" :key="item.metric_id" :data-testid="`section-metric-${item.metric_id}`">
            <td>{{ item.metric_id }}</td>
            <td>{{ item.metric_name }}</td>
            <td>{{ item.current_value ?? '缺失' }}</td>
            <td>{{ item.round_id ?? '—' }}</td>
            <td>metric_values#{{ item.metric_value_id ?? '—' }}</td>
            <td>{{ item.source_time ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <p v-if="message" class="panel-msg" data-testid="section-message">{{ message }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import {
  captureSection,
  generateReport,
  saveReportSnapshot,
  type ReportSectionContent,
} from '@/api/backend-service'

const props = defineProps<{ rows: { id: number | string }[] }>()
const emit = defineEmits<{ changed: []; error: [message: string] }>()

const reportId = ref('')
const section = ref<ReportSectionContent | null>(null)
const message = ref('')

function modeLabel(mode: string) {
  return {
    merged_round: '同轮合并读取',
    fallback_previous_round: '回退上一完整轮',
    explicit_round: '指定轮次',
    incomplete_round: '轮次未写齐',
    legacy_merged: '无轮次（合并取最新）',
  }[mode] ?? mode
}

function defaultReportId() {
  const ids = props.rows.map((row) => Number(row.id)).filter((id) => Number.isFinite(id))
  return ids.length ? String(Math.max(...ids)) : '1'
}

async function load() {
  message.value = ''
  if (!reportId.value.trim()) return
  try {
    section.value = await captureSection(Number(reportId.value))
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '截面读取失败')
  }
}

async function generate() {
  message.value = ''
  try {
    const result = await generateReport(Number(reportId.value))
    message.value = `已生成，轮次 ${result.round_id ?? '—'}`
    await load()
    emit('changed')
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '报告生成失败')
  }
}

async function saveSnapshot() {
  message.value = ''
  try {
    const snapshot = await saveReportSnapshot(
      Number(reportId.value),
      `日报快照-${new Date().toISOString().slice(0, 19)}`,
    )
    message.value = snapshot.waited_for_round
      ? `已留档：轮次 ${snapshot.round_id}，已等到完整新一轮`
      : `已留档：沿用轮次 ${snapshot.round_id}，未等到新一轮 ${snapshot.snapshot_content.pending_round_id ?? ''}`
    await load()
    emit('changed')
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '留档失败')
  }
}

onMounted(() => {
  reportId.value = defaultReportId()
  void load()
})

watch(
  () => props.rows.length,
  () => {
    if (!reportId.value) reportId.value = defaultReportId()
  },
)
</script>

<style scoped>
.report-panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.panel-head h3 { margin: 0 0 4px; font-size: 15px; }
.panel-desc { margin: 0 0 10px; color: var(--muted); font-size: 12px; }
.panel-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 12px; }
.panel-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.panel-form input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; width: 120px; }
.panel-meta { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); margin-bottom: 10px; }
.panel-meta .warn { color: #b45309; }
.panel-msg { font-size: 12px; color: #1f6feb; margin: 8px 0 0; }
</style>
