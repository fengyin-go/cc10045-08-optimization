<template>
  <section class="report-panel" data-testid="snapshot-panel">
    <header class="panel-head">
      <h3>留档对比</h3>
      <p class="panel-desc">
        两份留档按指标编号对齐，逐项给出差值；同一次报告生成的两个副本会注明来源相同，差值应为 0。
      </p>
    </header>

    <form class="panel-form" @submit.prevent="load">
      <label>
        <span>报告编号</span>
        <input v-model="reportId" data-testid="snapshot-report-id" placeholder="如 1" />
      </label>
      <button class="btn" data-testid="snapshot-load" type="submit">列出留档</button>
    </form>

    <table v-if="snapshots.length" class="data-table">
      <thead>
        <tr>
          <th>留档</th><th>名称</th><th>所属轮次</th><th>数据截面</th><th>截止</th><th>来源项</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="snapshot in snapshots" :key="snapshot.id" :data-testid="`snapshot-${snapshot.id}`">
          <td>
            <label class="pick">
              <input v-model="picked" type="checkbox" :value="snapshot.id" />
              #{{ snapshot.id }}
            </label>
          </td>
          <td>{{ snapshot.snapshot_name }}</td>
          <td>{{ snapshot.round_id ?? '—' }}</td>
          <td>{{ snapshot.section_id ?? '—' }}</td>
          <td>{{ snapshot.snapshot_content?.cutoff_at ?? '—' }}</td>
          <td class="sources">
            <span v-for="src in snapshot.source_summary" :key="src.metric_name">
              {{ src.metric_name }}：{{ src.value ?? '—' }}（{{ src.source }}）
            </span>
          </td>
        </tr>
      </tbody>
    </table>

    <div class="panel-form">
      <button class="btn primary" type="button" :disabled="picked.length !== 2" @click="compare">
        比较选中的两次留档
      </button>
    </div>

    <template v-if="comparison">
      <p class="panel-msg" data-testid="compare-source-note">
        {{ comparison.summary.source_note }}
        （旧轮 {{ comparison.summary.round_a ?? '—' }} → 新轮 {{ comparison.summary.round_b ?? '—' }}）
      </p>
      <table class="data-table">
        <thead>
          <tr><th>指标编号</th><th>指标</th><th>旧值</th><th>新值</th><th>差值</th><th>差值百分比</th></tr>
        </thead>
        <tbody>
          <tr v-for="diff in comparison.diffs" :key="diff.metric_id" :data-testid="`diff-${diff.metric_id}`">
            <td>{{ diff.metric_id }}</td>
            <td>{{ diff.metric_name }}</td>
            <td>{{ diff.value_a ?? '—' }}</td>
            <td>{{ diff.value_b ?? '—' }}</td>
            <td>{{ diff.diff ?? '—' }}</td>
            <td>{{ diff.diff_pct ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </template>
    <p v-if="message" class="panel-msg">{{ message }}</p>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  compareReportSnapshots,
  listReportSnapshots,
  type ReportSnapshot,
  type SnapshotComparison,
} from '@/api/backend-service'

const emit = defineEmits<{ error: [message: string] }>()

const reportId = ref('1')
const snapshots = ref<ReportSnapshot[]>([])
const picked = ref<number[]>([])
const comparison = ref<SnapshotComparison | null>(null)
const message = ref('')

async function load() {
  message.value = ''
  comparison.value = null
  picked.value = []
  try {
    snapshots.value = await listReportSnapshots(Number(reportId.value))
    if (!snapshots.value.length) message.value = '该报告还没有留档'
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '留档读取失败')
  }
}

async function compare() {
  message.value = ''
  comparison.value = null
  // 列表按新到旧排列，选中的两个按 id 升序作为旧轮 → 新轮
  const [a, b] = [...picked.value].sort((x, y) => x - y)
  try {
    comparison.value = await compareReportSnapshots(a, b)
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '对比失败')
  }
}
</script>

<style scoped>
.report-panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.panel-head h3 { margin: 0 0 4px; font-size: 15px; }
.panel-desc { margin: 0 0 10px; color: var(--muted); font-size: 12px; }
.panel-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 12px; }
.panel-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.panel-form input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; width: 120px; }
.pick { flex-direction: row; align-items: center; gap: 6px; }
.sources { display: flex; flex-direction: column; gap: 2px; font-size: 11px; color: var(--muted); }
.panel-msg { font-size: 12px; color: #1f6feb; margin: 8px 0; }
</style>
