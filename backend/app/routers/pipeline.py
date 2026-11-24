"""数据分析管道路由：事件采集、聚合执行、指标快照、分群计算等核心操作入口。"""
from __future__ import annotations

from fastapi import APIRouter

from app import report_data
from app.database import db
from app.pipeline import (
    check_alert, compute_aggregation, compute_cohort_comparison,
    compute_forecast, compute_funnel, compute_metric_snapshot,
    compute_retention, compute_segment, generate_report,
    ingest_events, precompute_cube, seed_real_data,
)
from app.services import service_for

router = APIRouter(prefix="/api/pipeline", tags=["分析管线"])


@router.post("/seed")
def api_seed_real_data() -> dict:
    """初始化全部种子数据：事件、指标、聚合窗口、分群、漏斗等。"""
    counts = seed_real_data()
    return {"ok": True, "counts": counts}


@router.post("/events/ingest")
def api_ingest_events() -> dict:
    """对所有已启用的事件定义批量摄入模拟事件。"""
    svc = service_for("event_tracker")
    result = svc.ingest_all_enabled()
    return result


@router.post("/aggregation/run-all")
def api_run_all_aggregations() -> dict:
    """对所有已启用聚合窗口执行计算。"""
    svc = service_for("realtime_agg")
    result = svc.run_all_active()
    return result


@router.post("/aggregation/{window_id}/run")
def api_run_aggregation(window_id: int) -> dict:
    """对指定聚合窗口执行单次计算。"""
    svc = service_for("realtime_agg")
    return svc.run_aggregation(window_id)


@router.post("/metrics/compute-all")
def api_compute_all_metrics(round_id: int | None = None) -> dict:
    """对所有指标执行快照计算（默认同轮）。"""
    svc = service_for("metric_store")
    return svc.compute_all(round_id=round_id)


@router.post("/metrics/{metric_id}/snapshot")
def api_compute_snapshot(metric_id: int, round_id: int | None = None) -> dict:
    """对指定指标执行快照计算，可指定轮次。"""
    svc = service_for("metric_store")
    return svc.compute_snapshot(metric_id, round_id=round_id)


# ============================================================================
# 指标轮次 / 报告数据截面
# ============================================================================

@router.post("/report-rounds")
def api_create_round(payload: dict) -> dict:
    """开启一轮指标计算，声明本轮应写齐的指标集合。"""
    metric_ids = payload.get("metric_ids") or []
    if not metric_ids and payload.get("report_id") is not None:
        report = db.fetchone("SELECT * FROM reports WHERE id=?", [int(payload["report_id"])])
        metric_ids = report_data.report_metric_ids(report) if report else []
    round_row = report_data.create_round(
        payload.get("report_id"), metric_ids,
        note=payload.get("note", ""), round_key=payload.get("round_key"),
    )
    return {"ok": True, "round": round_row}


@router.get("/report-rounds")
def api_list_rounds(report_id: int | None = None) -> dict:
    """列出轮次及其完成情况。"""
    if report_id is not None:
        rows = report_data.rounds_for_report(report_id)
    else:
        rows = db.fetchall("SELECT * FROM report_rounds ORDER BY id DESC")
    items = []
    for row in rows:
        values = report_data.round_metric_values(int(row["id"]))
        required = report_data._as_id_list(row.get("required_metric_ids"))
        items.append({
            **row,
            "metric_ids": required,
            "written_metric_ids": sorted(values.keys()),
            "missing_metric_ids": [mid for mid in required if mid not in values],
            "is_complete": report_data.round_is_complete(row, values),
        })
    return {"items": items, "total": len(items)}


@router.post("/report-rounds/{round_id}/values")
def api_write_round_value(round_id: int, payload: dict) -> dict:
    """把一项指标值写入指定轮次（模拟收入先落、退款后到）。"""
    try:
        result = report_data.write_metric_value(
            round_id,
            int(payload["metric_id"]),
            float(payload["value"]),
            baseline_value=payload.get("baseline_value"),
            comparison_status=payload.get("comparison_status", "已保存"),
            snapshot_time=payload.get("snapshot_time"),
            data_source=payload.get("data_source", "report_round"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "result": result}


@router.get("/report-sections")
def api_list_sections(report_id: int) -> dict:
    """报告的全部数据截面（新的在前）。"""
    return {"items": report_data.list_sections(report_id)}


@router.get("/report-sections/{section_id}")
def api_section_detail(section_id: int) -> dict:
    """单个截面的完整内容与来源项。"""
    section = report_data.section_detail(section_id)
    if section is None:
        return {"ok": False, "message": "截面不存在"}
    return {"ok": True, "section": section}


@router.post("/report/{report_id}/generate")
def api_generate_report(
    report_id: int,
    round_id: int | None = None,
    wait_for_round: bool = False,
) -> dict:
    """对指定报告模板执行生成（合并读取 + 固化数据截面）。"""
    svc = service_for("data_report")
    return svc.generate(report_id, round_id=round_id, wait_for_round=wait_for_round)


@router.get("/report/{report_id}/outputs")
def api_report_outputs(report_id: int, limit: int = 10) -> dict:
    """报告历史输出，各自附带实际使用的数据截面。"""
    items = service_for("data_report").recent_outputs(report_id, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/report/{report_id}/sections")
def api_report_sections(report_id: int) -> dict:
    """报告历次数据截面。"""
    return {"items": report_data.list_sections(report_id)}


@router.get("/report/{report_id}/section")
def api_capture_report_section(report_id: int, wait_for_round: bool = False) -> dict:
    """按当前数据合并读取一份报告截面（预览，不生成输出）。"""
    return service_for("data_report").capture(report_id, wait_for_round=wait_for_round)


@router.post("/report/{report_id}/snapshots")
def api_create_report_snapshot(report_id: int, payload: dict | None = None) -> dict:
    """把报告当前确认的数据截面整体留档（含来源项）。"""
    payload = payload or {}
    return service_for("report_snapshot").create_for_report(
        report_id,
        payload.get("snapshot_name") or f"报告{report_id}留档",
        section_id=payload.get("section_id"),
        wait_for_round=bool(payload.get("wait_for_round", False)),
    )


@router.get("/report/{report_id}/snapshots")
def api_report_snapshots(report_id: int) -> dict:
    """报告的全部留档，含数据截面与来源项。"""
    items = report_data.list_snapshots(report_id)
    return {"items": items, "total": len(items)}


@router.get("/report-snapshots/{snapshot_id}")
def api_snapshot_detail(snapshot_id: int) -> dict:
    """单个留档的完整内容。"""
    snapshot = report_data.snapshot_detail(snapshot_id)
    if snapshot is None:
        return {"ok": False, "message": "快照不存在"}
    return {"ok": True, "snapshot": snapshot}


@router.get("/report-snapshots/{snapshot_id}/comparisons")
def api_snapshot_comparisons(snapshot_id: int) -> dict:
    """与该留档相关的历次对比结果（不可变）。"""
    items = report_data.list_comparisons(snapshot_id)
    return {"items": items, "total": len(items)}


@router.get("/report-snapshots/{snapshot_id_a}/compare/{snapshot_id_b}")
def api_compare_snapshots(snapshot_id_a: int, snapshot_id_b: int) -> dict:
    """按指标编号比较两个留档，逐项给出差值；同源时注明来源相同。"""
    return report_data.compare_snapshots(snapshot_id_a, snapshot_id_b)


@router.post("/segments/{segment_id}/compute")
def api_compute_segment(segment_id: int) -> dict:
    """对指定分群执行计算。"""
    svc = service_for("segment_engine")
    return svc.run_computation(segment_id)


@router.post("/funnels/{funnel_id}/compute")
def api_compute_funnel(funnel_id: int) -> dict:
    """对指定漏斗执行分析计算。"""
    svc = service_for("funnel_analyzer")
    return svc.run_computation(funnel_id)


@router.post("/retention/{task_id}/compute")
def api_compute_retention(task_id: int) -> dict:
    """对指定留存任务执行计算。"""
    svc = service_for("retention_analysis")
    return svc.run_computation(task_id)


@router.post("/forecast/{task_id}/compute")
def api_compute_forecast(task_id: int) -> dict:
    """对指定预测任务执行计算。"""
    svc = service_for("forecast_model")
    return svc.run_forecast(task_id)


@router.post("/comparison/{comparison_id}/compute")
def api_compute_comparison(comparison_id: int) -> dict:
    """对指定分群对比执行计算。"""
    svc = service_for("cohort_compare")
    return svc.run_comparison(comparison_id)


@router.post("/cube/{cube_id}/precompute")
def api_precompute_cube(cube_id: int) -> dict:
    """对指定Cube执行预计算。"""
    svc = service_for("cube_builder")
    return svc.run_precomputation(cube_id)


@router.post("/alerts/check-all")
def api_check_all_alerts() -> dict:
    """检查所有已生效告警规则。"""
    svc = service_for("alert_policy")
    return svc.check_all()


@router.post("/seed/demo-report")
def api_seed_demo_report() -> dict:
    """准备可演示的经营日报：收入/退款 + 一轮完整数据 + 基准留档。"""
    result = report_data.ensure_demo_daily_report()
    return {"ok": True, "demo": result}


@router.get("/analytics/full-pipeline")
def api_full_pipeline() -> dict:
    """执行完整分析链路：事件采集→聚合→指标→分群→漏斗→留存→预测→报告。"""
    steps = {}

    # 1. 事件采集
    svc_events = service_for("event_tracker")
    steps["events"] = svc_events.ingest_all_enabled()

    # 2. 聚合
    svc_agg = service_for("realtime_agg")
    steps["aggregation"] = svc_agg.run_all_active()

    # 3. 指标快照
    svc_metrics = service_for("metric_store")
    steps["metrics"] = svc_metrics.compute_all()

    # 4. 分群
    segments = db.fetchall("SELECT id FROM segments LIMIT 6")
    seg_results = []
    for s in segments:
        r = service_for("segment_engine").run_computation(s["id"])
        seg_results.append(r)
    steps["segments"] = {"processed": len(segments), "results": seg_results}

    # 5. 漏斗
    funnels = db.fetchall("SELECT id FROM funnels LIMIT 3")
    funnel_results = []
    for f in funnels:
        r = service_for("funnel_analyzer").run_computation(f["id"])
        funnel_results.append(r)
    steps["funnels"] = {"processed": len(funnels), "results": funnel_results}

    # 6. 留存
    ret_tasks = db.fetchall("SELECT id FROM retention_tasks LIMIT 3")
    ret_results = []
    for t in ret_tasks:
        r = service_for("retention_analysis").run_computation(t["id"])
        ret_results.append(r)
    steps["retention"] = {"processed": len(ret_tasks), "results": ret_results}

    # 7. 预测
    forecast_tasks = db.fetchall("SELECT id FROM forecast_tasks LIMIT 2")
    fc_results = []
    for t in forecast_tasks:
        r = service_for("forecast_model").run_forecast(t["id"])
        fc_results.append(r)
    steps["forecast"] = {"processed": len(forecast_tasks), "results": fc_results}

    # 8. 报告
    reports = db.fetchall("SELECT id FROM reports LIMIT 2")
    report_results = []
    for rep in reports:
        r = service_for("data_report").generate(rep["id"])
        report_results.append(r)
    steps["reports"] = {"processed": len(reports), "results": report_results}

    return {"ok": True, "pipeline": steps}