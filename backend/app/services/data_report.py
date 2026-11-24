"""数据报告领域服务：管理周期性分析报告的模板配置和自动生成。

真实数据流：定义报告模板（包含指标集/周期/输出格式）→ 合并读取同一轮指标值
→ 固化为报告数据截面（report_sections，可回查来源）→ 写入 report_outputs → 快照管理。

一次性读取全部指标，不再逐个指标串行查询；同一份报告的指标必须来自同一轮次。
"""
from __future__ import annotations

import json
from typing import Any

from app import report_data
from app.database import db
from app.pipeline import generate_report
from app.services.base import BaseModuleService


class DataReportService(BaseModuleService):
    MODULE = "data_report"
    ENTITY = "报告生成器"
    OPERATION_TERM = "生成"
    LIST_FIELDS = ["报告名称", "包含指标集", "报告周期", "输出格式", "自动发送"]
    REQUIRED_FIELDS = ["报告名称", "包含指标集", "报告周期"]
    STATUS_ORDER = ["草稿", "已发布", "已停用"]
    ACTION_RULES = {"发布报告": "已发布", "停用报告": "已停用"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """面板动作：生成报告 = 合并读取并固化截面；其余按状态流转。"""
        if action in ("生成报告", "生成", "生成数据报告"):
            result = self.generate(entry_id)
            if not result.get("ok"):
                return self.get_entry(entry_id), result.get("message", "生成失败")
            return self.get_entry(entry_id), (
                f"报告已生成（轮次 {result.get('round_id')}，"
                f"{result['summary']['total_metrics']} 项指标，"
                f"来源{'一致' if result.get('is_consistent') else '不一致'}）"
            )
        return super().run_action(entry_id, action)

    def generate(
        self,
        report_id: int,
        *,
        round_id: int | None = None,
        wait_for_round: bool = False,
    ) -> dict[str, Any]:
        """生成一次数据报告：一次合并读取，固化一份数据截面。"""
        report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id])
        if report is None:
            return {"ok": False, "message": f"报告 {report_id} 不存在"}

        result = generate_report(dict(report), round_id=round_id, wait_for_round=wait_for_round)
        if not result.get("ok"):
            return result
        return {
            "ok": True,
            "output_id": result["id"],
            "report_name": result["report_name"],
            "summary": result["summary"],
            "section_id": result["section_id"],
            "section_key": result["section_key"],
            "round_id": result["round_id"],
            "round_key": result["round_key"],
            "data_mode": result["data_mode"],
            "cutoff_at": result["cutoff_at"],
            "is_complete": result["is_complete"],
            "is_consistent": result["is_consistent"],
            "waited_for_round": result["waited_for_round"],
            "pending_round_id": result["pending_round_id"],
            "pending_round_status": result["pending_round_status"],
            "source_summary": result["source_summary"],
            "sections": result["sections"],
        }

    def capture(self, report_id: int, *, wait_for_round: bool = False) -> dict[str, Any]:
        """合并读取一次报告数据截面（不生成 output），返回截面与来源项。"""
        report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id])
        if report is None:
            return {"ok": False, "message": f"报告 {report_id} 不存在"}
        return report_data.capture_report_section(
            dict(report), wait_for_round=wait_for_round, note="报告数据预览")


    def recent_outputs(self, report_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """获取报告的历史生成结果，附带本次实际使用的数据截面与来源项。"""
        rows = db.fetchall(
            "SELECT * FROM report_outputs WHERE report_id=? ORDER BY id DESC LIMIT ?",
            [report_id, limit],
        )
        for r in rows:
            r["output_content"] = report_data._loads(r.get("output_content"), {})
            section = (
                report_data.section_detail(int(r["section_id"]))
                if r.get("section_id") is not None else None
            )
            r["section"] = section
            r["source_summary"] = section["source_summary"] if section else r["output_content"].get("source_summary", [])
        return rows

    def compare_outputs(self, output_id_a: int, output_id_b: int) -> dict[str, Any]:
        """对比两份报告输出的差异（按指标编号对齐，不受栏目顺序影响）。"""
        a = db.fetchone("SELECT * FROM report_outputs WHERE id=?", [output_id_a])
        b = db.fetchone("SELECT * FROM report_outputs WHERE id=?", [output_id_b])
        if not a or not b:
            return {"ok": False, "message": "报告输出不存在"}

        content_a = report_data._loads(a["output_content"], {})
        content_b = report_data._loads(b["output_content"], {})

        map_a = report_data.section_metric_map(content_a)
        map_b = report_data.section_metric_map(content_b)
        diffs = []
        for key in list(map_a.keys()) + [k for k in map_b.keys() if k not in map_a]:
            sa = map_a.get(key, {})
            sb = map_b.get(key, {})
            val_a = report_data._as_float(sa.get("current_value"))
            val_b = report_data._as_float(sb.get("current_value"))
            if val_a is None or val_b is None:
                continue
            diff = round(val_b - val_a, 4)
            pct = round(diff / abs(val_a) * 100, 2) if val_a != 0 else None
            diffs.append({
                "metric_id": sb.get("metric_id", sa.get("metric_id")),
                "metric": sb.get("metric_name") or sa.get("metric_name"),
                "value_a": val_a,
                "value_b": val_b,
                "diff": diff,
                "diff_pct": pct,
                "has_diff": abs(diff) > 1e-9,
            })

        same_source = bool(
            (a.get("generation_key") and a.get("generation_key") == b.get("generation_key"))
            or (a.get("section_id") and a.get("section_id") == b.get("section_id"))
        )
        return {
            "ok": True,
            "output_a_time": a.get("generated_at"),
            "output_b_time": b.get("generated_at"),
            "same_source": same_source,
            "significant_diffs": len([d for d in diffs if d["diff_pct"] is not None and abs(d["diff_pct"]) > 5]),
            "diffs": diffs,
        }

    def captured_section(self, report_id: int) -> dict[str, Any] | None:
        """查看报告最近一次确认的数据截面。"""
        return report_data.latest_section(report_id)

    def sections(self, report_id: int) -> list[dict[str, Any]]:
        """报告全部历史截面，供回查。"""
        return report_data.list_sections(report_id)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_reports"] = db.count("reports")
        snapshot["total_outputs"] = db.count("report_outputs")
        snapshot["total_sections"] = db.count("report_sections")
        snapshot["open_rounds"] = db.count("report_rounds", "status=?", [report_data.ROUND_OPEN])
        return snapshot
