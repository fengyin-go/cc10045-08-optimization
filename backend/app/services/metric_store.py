"""指标存储领域服务：管理指标定义、执行快照计算并与基准对比。

真实数据流：接收指标定义 → 调用 pipeline.compute_metric_snapshot 计算当前值
→ 写入 metric_values 表 → 对比上一期基准值和历史序列 → 检测 z-score 异常
→ 下游告警策略消费者。
"""
from __future__ import annotations

import json
from typing import Any

from app import report_data
from app.database import db
from app.pipeline import compute_metric_snapshot
from app.services.base import BaseModuleService


class MetricStoreService(BaseModuleService):
    MODULE = "metric_store"
    ENTITY = "指标快照"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["指标名称", "快照值", "快照时间", "口径版本", "对比基准"]
    REQUIRED_FIELDS = ["指标名称", "快照值", "快照时间"]
    STATUS_ORDER = ["已保存", "对比成功", "有异常"]
    ACTION_RULES = {"保存快照": "已保存", "标记异常": "有异常"}
    NEGATIVE_ACTIONS = []

    _SNAPSHOT_COLUMNS = {
        "指标名称": "metric_name",
        "快照值": "snapshot_value",
        "快照时间": "snapshot_time",
        "口径版本": "calibration_version",
        "对比基准": "baseline_value",
    }

    def _snapshot_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """把 metric_values 行映射成指标快照页面的字段。"""
        return {
            "id": row["id"],
            "status": row.get("comparison_status") or "已保存",
            "pending": False,
            "abnormal": row.get("comparison_status") == "有异常",
            "指标名称": row.get("metric_name", ""),
            "快照值": row.get("snapshot_value"),
            "快照时间": row.get("snapshot_time", ""),
            "口径版本": row.get("calibration_version", 1),
            "对比基准": row.get("baseline_value"),
            "metric_id": row.get("metric_id"),
            "round_id": row.get("round_id"),
            "round_key": row.get("round_key"),
        }

    def _snapshot_rows(
        self, where: str = "1=1", params: list[Any] | None = None,
        page: int = 1, size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        params = params or []
        total = db.count("metric_values", where, params)
        rows = db.fetchall(
            f"SELECT mv.*, m.metric_name FROM metric_values mv "
            f"LEFT JOIN metrics m ON m.id = mv.metric_id "
            f"WHERE {where} ORDER BY mv.id DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        )
        return [self._snapshot_view(row) for row in rows], total

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """指标快照列表取自 metric_values，而不是指标定义表。"""
        where = ["1=1"]
        params: list[Any] = []
        for field, expected in (filters or {}).items():
            column = self._SNAPSHOT_COLUMNS.get(field)
            if column:
                where.append(f"mv.{column} LIKE ?")
                params.append(f"%{expected}%")
        if keyword:
            where.append("m.metric_name LIKE ?")
            params.append(f"%{keyword}%")
        if status:
            where.append("mv.comparison_status = ?")
            params.append(status)
        return self._snapshot_rows(" AND ".join(where), params, page, size)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = db.fetchone(
            "SELECT mv.*, m.metric_name FROM metric_values mv "
            "LEFT JOIN metrics m ON m.id = mv.metric_id WHERE mv.id=?",
            [entry_id],
        )
        return self._snapshot_view(row) if row else None

    def list_metrics(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        """列出所有指标定义。"""
        total = db.count("metrics")
        rows = db.fetchall(
            "SELECT * FROM metrics ORDER BY id DESC LIMIT ? OFFSET ?",
            [size, (page - 1) * size],
        )
        return rows, total

    def compute_snapshot(self, metric_id: int, round_id: int | None = None) -> dict[str, Any]:
        """对指定指标执行一次快照计算并存储。

        未指定轮次时自动并入「最近一轮仍在计算、且还没写入该指标」的轮次，
        使报告内多项指标能落在同一轮里。
        """
        metric = db.fetchone("SELECT * FROM metrics WHERE id=?", [metric_id])
        if metric is None:
            return {"ok": False, "message": f"指标 {metric_id} 不存在"}
        result = compute_metric_snapshot(dict(metric), round_id=round_id)
        if result is None:
            return {"ok": False, "message": f"指标 {metric['metric_name']} 计算失败"}
        return {"ok": True, "result": result}

    def save_value(
        self,
        metric_id: int,
        value: float,
        *,
        round_id: int | None = None,
        baseline_value: float | None = None,
        comparison_status: str = "已保存",
    ) -> dict[str, Any]:
        """把一项明确的指标值写入当前轮次（模拟新一轮覆盖率结果落地）。"""
        metric = db.fetchone("SELECT * FROM metrics WHERE id=?", [metric_id])
        if metric is None:
            return {"ok": False, "message": f"指标 {metric_id} 不存在"}
        target_round = round_id
        if target_round is None:
            target_round = int(report_data.resolve_round_for_metric(metric_id)["id"])
        try:
            result = report_data.write_metric_value(
                target_round, metric_id, value,
                baseline_value=baseline_value,
                comparison_status=comparison_status,
                data_source="metric_store",
            )
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "result": result}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记一条指标快照：写入 metric_values，并归属到当前指标轮次。"""
        metric_name = str(values.get("指标名称", "")).strip()
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        metric = db.fetchone("SELECT * FROM metrics WHERE metric_name=?", [metric_name])
        if metric is None:
            # metrics 表同时承载指标定义，登记新指标时补齐类型与来源列
            metric_id = db.insert("metrics", {
                "metric_name": metric_name,
                "metric_type": values.get("指标类型") or "gauge",
                "source_module": values.get("来源模块") or "metric_store",
                "calibration_version": int(values.get("口径版本") or 1),
                "unit": values.get("单位") or "",
            })
            metric = db.fetchone("SELECT * FROM metrics WHERE id=?", [metric_id])

        try:
            value = float(values["快照值"])
        except (TypeError, ValueError):
            return None, ["快照值"]

        baseline = values.get("对比基准")
        try:
            baseline_value = float(baseline) if str(baseline or "").strip() else None
        except (TypeError, ValueError):
            baseline_value = None

        saved = self.save_value(
            int(metric["id"]), value,
            baseline_value=baseline_value,
            comparison_status=self.STATUS_ORDER[0],
        )
        if not saved.get("ok"):
            return None, []
        entry = self.get_entry(int(saved["result"]["metric_value_id"]))
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """快照动作作用于 metric_values 行。"""
        snapshot = db.fetchone("SELECT * FROM metric_values WHERE id=?", [entry_id])
        if snapshot is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        db.update("metric_values", {"comparison_status": target}, "id=?", [entry_id])
        return self.get_entry(entry_id), f"{self.ENTITY}已{action}"

    def compute_all(self, round_id: int | None = None) -> dict[str, Any]:
        """对所有指标执行快照计算，默认落进同一轮。"""
        metrics = db.fetchall("SELECT * FROM metrics")
        target_round = round_id
        if target_round is None and metrics:
            round_row = report_data.create_round(
                None, [m["id"] for m in metrics], note="全量指标计算")
            target_round = int(round_row["id"])
        results = []
        for m in metrics:
            r = self.compute_snapshot(m["id"], round_id=target_round)
            results.append({
                "metric_name": m["metric_name"],
                "ok": r["ok"],
                "value": r["result"]["snapshot_value"] if r["ok"] else None,
                "status": r["result"]["comparison_status"] if r["ok"] else "failed",
                "round_id": r["result"].get("round_id") if r["ok"] else target_round,
            })
        return {"processed": len(metrics), "round_id": target_round, "results": results}

    def metric_trend(self, metric_id: int, limit: int = 30) -> dict[str, Any]:
        """获取指标的历史趋势数据。"""
        metric = db.fetchone("SELECT * FROM metrics WHERE id=?", [metric_id])
        if metric is None:
            return {"trend": "no_data", "points": []}

        rows = db.fetchall(
            "SELECT * FROM metric_values WHERE metric_id=? ORDER BY snapshot_time DESC LIMIT ?",
            [metric_id, limit],
        )
        values = [r["snapshot_value"] for r in rows]
        if not values:
            return {"trend": "no_data", "points": [], "metric_name": metric["metric_name"]}

        import statistics
        mean_v = statistics.mean(values)

        trend = "stable"
        if len(values) >= 2:
            if values[0] > mean_v * 1.1:
                trend = "upward"
            elif values[0] < mean_v * 0.9:
                trend = "downward"

        return {
            "metric_name": metric["metric_name"],
            "metric_type": metric["metric_type"],
            "unit": metric["unit"],
            "trend": trend,
            "latest": values[0],
            "mean": round(mean_v, 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "points": [{"time": r["snapshot_time"], "value": r["snapshot_value"],
                       "status": r["comparison_status"]} for r in rows],
        }

    def compare_calibrations(self, metric_id: int, v1: int, v2: int) -> dict[str, Any]:
        """对比同一指标在两个口径版本下的差异。"""
        snapshots_v1 = db.fetchall(
            "SELECT * FROM metric_values WHERE metric_id=? AND calibration_version=? ORDER BY snapshot_time DESC LIMIT 10",
            [metric_id, v1],
        )
        snapshots_v2 = db.fetchall(
            "SELECT * FROM metric_values WHERE metric_id=? AND calibration_version=? ORDER BY snapshot_time DESC LIMIT 10",
            [metric_id, v2],
        )

        import statistics
        mean_v1 = statistics.mean([s["snapshot_value"] for s in snapshots_v1]) if snapshots_v1 else 0
        mean_v2 = statistics.mean([s["snapshot_value"] for s in snapshots_v2]) if snapshots_v2 else 0
        diff_pct = round((mean_v1 - mean_v2) / max(abs(mean_v2), 0.0001) * 100, 2)

        return {
            "metric_id": metric_id,
            "version_a": v1,
            "version_b": v2,
            "mean_a": round(mean_v1, 4),
            "mean_b": round(mean_v2, 4),
            "diff_pct": diff_pct,
            "significant": abs(diff_pct) > 5,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_metrics"] = db.count("metrics")
        snapshot["total_snapshots"] = db.count("metric_values")
        abnormal = db.count("metric_values", "comparison_status='有异常'")
        snapshot["abnormal_snapshots"] = abnormal
        return snapshot