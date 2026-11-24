"""报告快照领域服务：把报告的数据截面整体留档，并支持历史版本对比。

真实数据流：读取报告已确认的数据截面（report_sections）→ 复制截面内容与来源项写入
snapshots（section_id / round_id / generation_key / source_summary 一并存下）
→ 两版本按指标编号对齐比较 → 差异结果另存 snapshot_comparisons。

约定：
  - 留档一旦生成即不可改写，后续指标重算只影响新截面，不影响已留档的汇总与差异。
  - 同一次报告生成产生的两份副本来源相同，比较时注明「来源相同」，差值应为 0。
  - 保存时若新一轮只写了一半指标，留档整体使用上一完整轮，并记录未等到的那一轮
    及本次截止点，避免出现新旧值拼接。
"""
from __future__ import annotations

from typing import Any

from app import report_data
from app.database import db
from app.services.base import BaseModuleService


class ReportSnapshotService(BaseModuleService):
    MODULE = "report_snapshot"
    ENTITY = "快照记录"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["快照名称", "所属报告", "快照时间", "数据摘要", "差异对比"]
    REQUIRED_FIELDS = ["快照名称", "所属报告"]
    STATUS_ORDER = ["已保存", "已对比", "有差异"]
    ACTION_RULES = {"生成快照": "已保存", "对比快照": "已对比"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """面板动作：生成快照 = 重新留档所属报告；对比快照 = 与上一份留档比较。"""
        row = self.get_entry(entry_id)
        if row is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action in ("生成快照", "保存快照", "留档"):
            report_id = row.get("report_id")
            if report_id is None:
                return None, "该快照记录未关联报告，无法留档"
            name = row.get("snapshot_name") or f"报告{report_id}留档"
            result = self.create_for_report(int(report_id), f"{name}-{report_data.now()}")
            if not result.get("ok"):
                return None, result.get("message", "留档失败")
            return self.get_entry(entry_id), (
                f"已按报告 {report_id} 当前数据截面留档，轮次 {result.get('round_id')}，"
                f"截止 {result.get('cutoff_at') or '—'}"
            )
        if action in ("对比快照", "对比"):
            report_id = row.get("report_id")
            previous = db.fetchone(
                "SELECT * FROM snapshots WHERE report_id=? AND id<? ORDER BY id DESC LIMIT 1",
                [report_id, entry_id],
            )
            if previous is None:
                return None, "没有更早的留档可供对比"
            result = self.compare_snapshots(int(previous["id"]), entry_id)
            if not result.get("ok"):
                return None, result.get("message", "对比失败")
            return self.get_entry(entry_id), (
                f"与留档 {previous['id']} 对比完成："
                f"{result['summary']['diffs_found']} 项有差值"
                + ("（来源相同）" if result.get("same_source") else "")
            )
        return super().run_action(entry_id, action)

    def create_for_report(
        self,
        report_id: int,
        snapshot_name: str,
        *,
        section_id: int | None = None,
        wait_for_round: bool = False,
    ) -> dict[str, Any]:
        """为指定报告留档：记录实际收到的报告数据截面及来源项。"""
        return report_data.create_snapshot(
            report_id,
            snapshot_name,
            section_id=section_id,
            wait_for_round=wait_for_round,
        )

    def compare_snapshots(self, snapshot_id_a: int, snapshot_id_b: int) -> dict[str, Any]:
        """按指标编号比较两个留档，统计各自差值。"""
        return report_data.compare_snapshots(snapshot_id_a, snapshot_id_b)

    def report_snapshots(self, report_id: int) -> list[dict[str, Any]]:
        """获取报告的所有留档（按生成先后倒序）。"""
        return report_data.list_snapshots(report_id)

    def snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return report_data.snapshot_detail(snapshot_id)

    def comparisons(self, snapshot_id: int | None = None) -> list[dict[str, Any]]:
        """已留档的历次对比结果（不可变）。"""
        return report_data.list_comparisons(snapshot_id)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_snapshots"] = db.count("snapshots")
        snapshot["total_comparisons"] = db.count("snapshot_comparisons")
        return snapshot
