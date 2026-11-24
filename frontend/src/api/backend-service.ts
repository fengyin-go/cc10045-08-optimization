import { request, fetchJson } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type SectionItem = {
  metric_id: number
  metric_name: string
  unit: string
  current_value: number | null
  round_id: number | null
  metric_value_id: number | null
  source_time: string | null
}

export type ReportSectionContent = {
  report_id: number
  report_name: string
  data_mode: string
  round_id: number | null
  is_complete: boolean
  is_consistent: boolean
  waited_for_round: boolean
  pending_round_id: number | null
  pending_round_status: string | null
  cutoff_at: string | null
  sections: SectionItem[]
  source_summary: { metric_name: string; value: number | null; source: string }[]
}

export type ReportSnapshot = {
  ok?: boolean
  id: number
  snapshot_id?: number
  snapshot_name: string
  section_id: number | null
  round_id: number | null
  round_key?: string | null
  generation_key: string | null
  created_at?: string
  data_mode?: string
  is_complete?: boolean
  is_consistent?: boolean
  waited_for_round?: boolean
  pending_round_id?: number | null
  pending_round_status?: string | null
  cutoff_at?: string | null
  snapshot_content: ReportSectionContent
  source_summary: ReportSectionContent['source_summary']
}

export type SnapshotDiff = {
  metric_id: number
  metric_name: string
  value_a: number | null
  value_b: number | null
  diff: number | null
  diff_pct: number | null
  has_diff: boolean
}

export type SnapshotComparison = {
  ok: boolean
  same_source: boolean
  has_difference: boolean
  summary: {
    round_a: number | null
    round_b: number | null
    same_source: boolean
    source_note: string
    diffs_found: number
    significant_diffs: number
    details: SnapshotDiff[]
  }
  diffs: SnapshotDiff[]
}

export async function captureSection(reportId: number): Promise<ReportSectionContent> {
  const payload = await fetchJson<{ ok: boolean; content: ReportSectionContent }>(
    `/api/pipeline/report/${reportId}/section`,
  )
  if (!payload.ok) throw new Error('报告数据截面读取失败')
  return payload.content
}

export async function generateReport(reportId: number): Promise<Record<string, unknown>> {
  const response = await request(`/api/pipeline/report/${reportId}/generate`, { method: 'POST' })
  const payload = await response.json()
  if (!payload.ok) throw new Error(payload.message ?? '报告生成失败')
  return payload
}

export async function saveReportSnapshot(
  reportId: number,
  snapshotName: string,
  sectionId?: number,
): Promise<ReportSnapshot> {
  const response = await request(`/api/pipeline/report/${reportId}/snapshots`, {
    method: 'POST',
    body: JSON.stringify({ snapshot_name: snapshotName, section_id: sectionId }),
  })
  const payload = await response.json()
  if (!payload.ok) throw new Error(payload.message ?? '留档失败')
  return { ...payload, id: payload.snapshot_id }
}

export async function listReportSnapshots(reportId: number): Promise<ReportSnapshot[]> {
  const payload = await fetchJson<{ items: ReportSnapshot[] }>(
    `/api/pipeline/report/${reportId}/snapshots`,
  )
  return payload.items
}

export async function compareReportSnapshots(
  snapshotA: number,
  snapshotB: number,
): Promise<SnapshotComparison> {
  return fetchJson<SnapshotComparison>(
    `/api/pipeline/report-snapshots/${snapshotA}/compare/${snapshotB}`,
  )
}
