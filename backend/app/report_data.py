"""报告数据截面与指标轮次：保证一次报告读取的数据来自同一轮指标计算。

核心约束：
  1. 报告的多指标汇总走合并读取（一条 SQL 取齐全部指标的最新值），不再逐指标查询。
  2. 每份报告数据截面（report_sections + report_section_items）持久保存实际读到的
     指标值、来源行（metric_values.id）、所属轮次和截止时间，供月底复核回查。
  3. 同一份报告里的指标必须来自同一轮；若新一轮只写入了部分指标，读取时整体回退
     到上一完整轮，绝不允许「收入 120 + 退款 10」这种撕裂组合。
  4. 截面一经确认不再改写；后续重算只产生新轮次、新截面、新快照。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from app.database import db


ROUND_OPEN = "计算中"
ROUND_DONE = "已完成"
SECTION_CONFIRMED = "已确认"


# ============================================================================
# 通用工具
# ============================================================================

def _loads(raw: Any, default: Any = None) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _as_id_list(raw: Any) -> list[int]:
    values = _loads(raw, [])
    if not isinstance(values, list):
        return []
    result: list[int] = []
    for value in values:
        try:
            metric_id = int(value)
        except (TypeError, ValueError):
            continue
        if metric_id not in result:
            result.append(metric_id)
    return result


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def report_metric_ids(report: dict[str, Any]) -> list[int]:
    """读取报告模板包含的指标编号，保持模板里的顺序。"""
    return _as_id_list(report.get("包含指标集", report.get("metric_ids", "[]")))


def _metrics_by_id(metric_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = list(dict.fromkeys(int(mid) for mid in metric_ids))
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = db.fetchall(f"SELECT * FROM metrics WHERE id IN ({placeholders})", ids)
    return {row["id"]: row for row in rows}


# ============================================================================
# 轮次：同一轮内的指标值才能拼成一份报告
# ============================================================================

def _round_key() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"RND-{stamp}-{uuid.uuid4().hex[:6]}"


def get_round(round_id: int | None) -> dict[str, Any] | None:
    if round_id is None:
        return None
    return db.fetchone("SELECT * FROM report_rounds WHERE id=?", [round_id])


def create_round(
    report_id: int | None,
    metric_ids: Iterable[int],
    *,
    note: str = "",
    round_key: str | None = None,
) -> dict[str, Any]:
    """开启一轮指标计算，记录本轮应当覆盖的指标集合。"""
    required = list(dict.fromkeys(int(mid) for mid in metric_ids))
    key = round_key or _round_key()
    round_id = db.insert("report_rounds", {
        "round_key": key,
        "report_id": report_id,
        "required_metric_ids": _dumps(required),
        "status": ROUND_OPEN,
        "started_at": now(),
        "note": note,
    })
    return db.fetchone("SELECT * FROM report_rounds WHERE id=?", [round_id]) or {}


def round_metric_values(round_id: int) -> dict[int, dict[str, Any]]:
    """一轮内每个指标的最新值，单条 SQL 取齐。"""
    rows = db.fetchall(
        "SELECT * FROM metric_values WHERE round_id=? ORDER BY id ASC", [round_id],
    )
    values: dict[int, dict[str, Any]] = {}
    for row in rows:
        values[int(row["metric_id"])] = row
    return values


def round_is_complete(round_row: dict[str, Any], values: dict[int, dict[str, Any]] | None = None) -> bool:
    required = _as_id_list(round_row.get("required_metric_ids"))
    if not required:
        return False
    present = values if values is not None else round_metric_values(int(round_row["id"]))
    return all(metric_id in present for metric_id in required)


def refresh_round_status(round_id: int) -> dict[str, Any] | None:
    round_row = get_round(round_id)
    if round_row is None:
        return None
    values = round_metric_values(round_id)
    complete = round_is_complete(round_row, values)
    cutoff = max((row["snapshot_time"] for row in values.values()), default=None)
    changes: dict[str, Any] = {}
    if complete and round_row["status"] != ROUND_DONE:
        changes["status"] = ROUND_DONE
        changes["completed_at"] = now()
    if cutoff and cutoff != round_row.get("cutoff_at"):
        changes["cutoff_at"] = cutoff
    if changes:
        db.update("report_rounds", changes, "id=?", [round_id])
        return get_round(round_id)
    return round_row


def write_metric_value(
    round_id: int,
    metric_id: int,
    value: float,
    *,
    baseline_value: float | None = None,
    comparison_status: str = "已保存",
    snapshot_time: str | None = None,
    calibration_version: int | None = None,
    data_source: str = "report_round",
) -> dict[str, Any]:
    """把一项指标值写入指定轮次；同一轮同一指标重复写入按更新处理。"""
    metric = db.fetchone("SELECT * FROM metrics WHERE id=?", [metric_id])
    if metric is None:
        raise ValueError(f"指标 {metric_id} 不存在")
    round_row = get_round(round_id)
    if round_row is None:
        raise ValueError(f"轮次 {round_id} 不存在")

    existing = db.fetchone(
        "SELECT * FROM metric_values WHERE round_id=? AND metric_id=? ORDER BY id ASC LIMIT 1",
        [round_id, metric_id],
    )
    payload = {
        "metric_id": metric_id,
        "snapshot_value": round(float(value), 4),
        "snapshot_time": snapshot_time or now(),
        "calibration_version": (
            calibration_version
            if calibration_version is not None
            else metric.get("calibration_version", 1)
        ),
        "baseline_value": baseline_value,
        "comparison_status": comparison_status,
        "round_id": round_id,
        "round_key": round_row.get("round_key"),
        "data_source": data_source,
    }
    if existing:
        db.update("metric_values", {
            "snapshot_value": payload["snapshot_value"],
            "snapshot_time": payload["snapshot_time"],
            "baseline_value": payload["baseline_value"],
            "comparison_status": payload["comparison_status"],
            "data_source": payload["data_source"],
        }, "id=?", [existing["id"]])
        value_id = existing["id"]
    else:
        value_id = db.insert("metric_values", payload)

    refreshed = refresh_round_status(round_id)
    return {
        "metric_value_id": value_id,
        "round_id": round_id,
        "round_status": refreshed["status"] if refreshed else ROUND_OPEN,
        "round_complete": bool(refreshed and refreshed["status"] == ROUND_DONE),
    }


def resolve_round_for_metric(metric_id: int) -> dict[str, Any]:
    """为一次指标计算找到应归属的轮次。

    优先并入最近一轮仍在计算、且尚未写入该指标的轮次；否则按包含该指标的
    报告指标体系新开一轮。这样「收入先写、退款后到」会落进同一轮。
    """
    reports = db.fetchall("SELECT * FROM reports")
    holder_reports = [r for r in reports if metric_id in report_metric_ids(r)]

    expected: list[int] = []
    for report in holder_reports:
        for mid in report_metric_ids(report):
            if mid not in expected:
                expected.append(mid)
    report_ids = [int(r["id"]) for r in holder_reports]

    for candidate in db.fetchall(
        "SELECT * FROM report_rounds WHERE status=? ORDER BY id DESC", [ROUND_OPEN],
    ):
        required = _as_id_list(candidate.get("required_metric_ids"))
        if metric_id not in required:
            continue
        scoped_report = candidate.get("report_id")
        if scoped_report is not None and report_ids and int(scoped_report) not in report_ids:
            continue
        values = round_metric_values(int(candidate["id"]))
        if metric_id in values:
            continue
        return candidate

    return create_round(
        report_ids[0] if len(report_ids) == 1 else None,
        expected or [metric_id],
        note="指标计算自动开轮",
    )


def rounds_for_report(report_id: int, metric_ids: Iterable[int] | None = None) -> list[dict[str, Any]]:
    """报告相关的轮次，按开启顺序倒序。"""
    wanted = set(int(mid) for mid in metric_ids) if metric_ids else None
    rows = db.fetchall("SELECT * FROM report_rounds ORDER BY id DESC")
    result = []
    for row in rows:
        scoped = row.get("report_id")
        if scoped is not None and int(scoped) != int(report_id):
            continue
        required = set(_as_id_list(row.get("required_metric_ids")))
        if wanted is not None and scoped is None and not (required & wanted):
            continue
        result.append(row)
    return result


# ============================================================================
# 报告数据截面：合并读取 + 持久留痕
# ============================================================================

def _latest_values(metric_ids: list[int]) -> dict[int, dict[str, Any]]:
    """一条合并查询取齐多个指标各自的最新值。"""
    if not metric_ids:
        return {}
    placeholders = ", ".join("?" for _ in metric_ids)
    rows = db.fetchall(
        f"SELECT * FROM metric_values WHERE metric_id IN ({placeholders}) ORDER BY id DESC",
        metric_ids,
    )
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:
        latest.setdefault(int(row["metric_id"]), row)
    return latest


def _pending_round(report_id: int, metric_ids: list[int], after_id: int | None) -> dict[str, Any] | None:
    wanted = set(metric_ids)
    for row in rounds_for_report(report_id, metric_ids):
        if row["status"] != ROUND_OPEN:
            continue
        if after_id is not None and int(row["id"]) <= after_id:
            continue
        required = set(_as_id_list(row.get("required_metric_ids")))
        if required & wanted:
            return row
    return None


def _round_candidates(report_id: int, metric_ids: list[int]) -> list[tuple[dict[str, Any], dict[int, dict[str, Any]]]]:
    candidates = []
    for row in rounds_for_report(report_id, metric_ids):
        values = round_metric_values(int(row["id"]))
        if all(mid in values for mid in metric_ids):
            candidates.append((row, values))
    return candidates


def _section_key(report_id: int, round_id: int | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"SEC-{report_id}-{round_id if round_id is not None else 'L'}-{stamp}-{uuid.uuid4().hex[:6]}"


def _source_item(metric: dict[str, Any], value: dict[str, Any] | None, position: int) -> dict[str, Any]:
    return {
        "position": position,
        "metric_id": metric["id"],
        "metric_name": metric["metric_name"],
        "metric_type": metric.get("metric_type", ""),
        "unit": metric.get("unit", ""),
        "value": _as_float(value["snapshot_value"]) if value else None,
        "baseline": _as_float(value["baseline_value"]) if value else None,
        "comparison_status": value["comparison_status"] if value else "unknown",
        "metric_value_id": value["id"] if value else None,
        "round_id": value.get("round_id") if value else None,
        "round_key": value.get("round_key") if value else None,
        "source_time": value["snapshot_time"] if value else None,
        "source": (
            f"metric_values#{value['id']}@report_rounds#{value.get('round_id')}"
            if value and value.get("round_id") is not None
            else (f"metric_values#{value['id']}@legacy" if value else "missing")
        ),
    }


def capture_report_section(
    report: dict[str, Any],
    *,
    round_id: int | None = None,
    wait_for_round: bool = False,
    note: str = "",
    data_mode_hint: str = "generate",
) -> dict[str, Any]:
    """合并读取一份报告数据截面并持久保存。

    若指定 round_id 则只读该轮；否则自动挑选「最近一个覆盖全部报告指标的完整轮」。
    新一轮只写入部分指标时整体回退到上一完整轮，并在截面里记录未等到的那一轮。
    """
    report_id = int(report["id"])
    metric_ids = report_metric_ids(report)
    if not metric_ids:
        metric_ids = [int(row["id"]) for row in db.fetchall("SELECT id FROM metrics ORDER BY id LIMIT 20")]

    metrics = _metrics_by_id(metric_ids)
    metric_ids = [mid for mid in metric_ids if mid in metrics]

    requested_round = get_round(round_id) if round_id is not None else None
    if round_id is not None and requested_round is None:
        return {"ok": False, "message": f"轮次 {round_id} 不存在"}

    chosen_round: dict[str, Any] | None = None
    values: dict[int, dict[str, Any]] = {}
    data_mode = "legacy_merged"

    if requested_round is not None:
        chosen_round = requested_round
        values = round_metric_values(int(chosen_round["id"]))
        data_mode = "explicit_round"
    else:
        candidates = _round_candidates(report_id, metric_ids)
        if candidates:
            chosen_round, values = candidates[0]
            data_mode = "merged_round"

    pending = _pending_round(
        report_id, metric_ids, int(chosen_round["id"]) if chosen_round else None,
    )

    if requested_round is None and chosen_round is None and wait_for_round:
        return {
            "ok": False,
            "ready": False,
            "message": "尚无覆盖全部指标的同轮数据",
            "pending_round_id": pending["id"] if pending else None,
            "pending_round_status": pending["status"] if pending else None,
            "metric_ids": metric_ids,
        }
    if requested_round is not None and wait_for_round and not all(mid in values for mid in metric_ids):
        missing = [mid for mid in metric_ids if mid not in values]
        return {
            "ok": False,
            "ready": False,
            "message": "指定轮次尚未写齐全部指标",
            "pending_round_id": chosen_round["id"] if chosen_round else None,
            "missing_metric_ids": missing,
        }

    if chosen_round is None:
        values = _latest_values(metric_ids)

    items: list[dict[str, Any]] = []
    for position, metric_id in enumerate(metric_ids):
        items.append(_source_item(metrics[metric_id], values.get(metric_id), position))

    complete = all(item["value"] is not None for item in items)
    round_ids = {item["round_id"] for item in items if item["round_id"] is not None}
    consistent = complete and len(round_ids) == 1 and all(
        item["round_id"] is not None for item in items
    )
    # waited_for_round：本次确实取自一轮完整数据，且没有更新的半轮在等。
    waited_for_round = bool(consistent and pending is None)
    if chosen_round is not None and not complete:
        data_mode = "incomplete_round"
    elif pending is not None and consistent:
        data_mode = "fallback_previous_round"

    cutoff_at: str | None = None
    if chosen_round is not None:
        cutoff_at = chosen_round.get("cutoff_at")
    if not cutoff_at:
        times = [item["source_time"] for item in items if item["source_time"]]
        cutoff_at = max(times) if times else None

    source_summary = [
        {
            key: item[key]
            for key in (
                "metric_id", "metric_name", "value", "metric_value_id",
                "round_id", "round_key", "source_time", "source",
            )
        }
        for item in items
    ]
    sections = [
        {
            "metric_id": item["metric_id"],
            "metric_name": item["metric_name"],
            "metric_type": item["metric_type"],
            "unit": item["unit"],
            "current_value": item["value"],
            "baseline": item["baseline"],
            "status": item["comparison_status"],
            "metric_value_id": item["metric_value_id"],
            "round_id": item["round_id"],
            "round_key": item["round_key"],
            "source_time": item["source_time"],
        }
        for item in items
    ]

    section_key = _section_key(report_id, int(chosen_round["id"]) if chosen_round else None)
    generated_at = now()
    content = {
        "report_id": report_id,
        "report_name": report.get("报告名称", report.get("report_name", "")),
        "generated_at": generated_at,
        "data_mode": data_mode,
        "round_id": int(chosen_round["id"]) if chosen_round else None,
        "round_key": chosen_round.get("round_key") if chosen_round else None,
        "section_key": section_key,
        "is_complete": complete,
        "is_consistent": consistent,
        "waited_for_round": waited_for_round,
        "pending_round_id": pending["id"] if pending else None,
        "pending_round_status": pending["status"] if pending else None,
        "pending_metric_ids": (
            [mid for mid in _as_id_list(pending.get("required_metric_ids")) if mid not in values]
            if pending else []
        ),
        "cutoff_at": cutoff_at,
        "sections": sections,
        "source_summary": source_summary,
        "summary": {
            "total_metrics": len(sections),
            "abnormal_count": sum(1 for s in sections if s.get("status") == "有异常"),
            "normal_count": sum(1 for s in sections if s.get("status") == "已保存"),
            "missing_count": sum(1 for s in sections if s.get("current_value") is None),
            "source_consistent": consistent,
        },
    }

    section_id = db.insert("report_sections", {
        "section_key": section_key,
        "report_id": report_id,
        "round_id": int(chosen_round["id"]) if chosen_round else None,
        "round_key": chosen_round.get("round_key") if chosen_round else None,
        "cutoff_metric_value_id": max(
            (item["metric_value_id"] for item in items if item["metric_value_id"]), default=None),
        "cutoff_at": cutoff_at,
        "status": SECTION_CONFIRMED,
        "is_complete": 1 if complete else 0,
        "is_consistent": 1 if consistent else 0,
        "waited_for_round": 1 if waited_for_round else 0,
        "pending_round_id": pending["id"] if pending else None,
        "pending_round_status": pending["status"] if pending else None,
        "data_mode": data_mode,
        "source_summary": _dumps(source_summary),
        "section_content": _dumps(content),
        "note": note or data_mode_hint,
    })
    for item in items:
        db.insert("report_section_items", {
            "section_id": section_id,
            "metric_id": item["metric_id"],
            "metric_name": item["metric_name"],
            "metric_type": item["metric_type"],
            "unit": item["unit"],
            "position": item["position"],
            "value": item["value"],
            "baseline_value": item["baseline"],
            "comparison_status": item["comparison_status"],
            "metric_value_id": item["metric_value_id"],
            "source_time": item["source_time"],
            "round_id": item["round_id"],
            "round_key": item["round_key"],
        })

    return {
        "ok": True,
        "ready": True,
        "section_id": section_id,
        "section_key": section_key,
        "report_id": report_id,
        "round_id": content["round_id"],
        "round_key": content["round_key"],
        "data_mode": data_mode,
        "is_complete": complete,
        "is_consistent": consistent,
        "waited_for_round": waited_for_round,
        "pending_round_id": content["pending_round_id"],
        "pending_round_status": content["pending_round_status"],
        "cutoff_at": cutoff_at,
        "content": content,
        "source_summary": source_summary,
    }


# ============================================================================
# 截面查询 / 快照 / 对比
# ============================================================================

def section_detail(section_id: int) -> dict[str, Any] | None:
    section = db.fetchone("SELECT * FROM report_sections WHERE id=?", [section_id])
    if section is None:
        return None
    section["section_content"] = _loads(section.get("section_content"), {})
    section["source_summary"] = _loads(section.get("source_summary"), [])
    section["items"] = db.fetchall(
        "SELECT * FROM report_section_items WHERE section_id=? ORDER BY position ASC, id ASC",
        [section_id],
    )
    return section


def latest_section(report_id: int) -> dict[str, Any] | None:
    row = db.fetchone(
        "SELECT * FROM report_sections WHERE report_id=? AND status=? ORDER BY id DESC LIMIT 1",
        [report_id, SECTION_CONFIRMED],
    )
    return section_detail(int(row["id"])) if row else None


def list_sections(report_id: int) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT * FROM report_sections WHERE report_id=? ORDER BY id DESC", [report_id],
    )
    for row in rows:
        row["section_content"] = _loads(row.get("section_content"), {})
        row["source_summary"] = _loads(row.get("source_summary"), [])
    return rows


def section_metric_map(content: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    """按指标编号建立栏目索引，顺序变化不影响对比。"""
    result: dict[Any, dict[str, Any]] = {}
    for index, section in enumerate(content.get("sections", []) or []):
        key = section.get("metric_id")
        if key is None:
            key = section.get("metric_name") or f"__position_{index}"
        result[str(key)] = section
    return result


def create_snapshot(
    report_id: int,
    snapshot_name: str,
    *,
    section_id: int | None = None,
    wait_for_round: bool = False,
) -> dict[str, Any]:
    """把报告当前确认的数据截面整体留档为快照。

    默认每次保存都重新合并读取一份截面（记录本次实际收到的数据与截止点）；
    只有显式传入 section_id 时才复制某份既有截面，用于同一次生成的两个副本。
    快照复制截面内容与来源项；之后无论指标如何重算，这份留档都不再变化。
    """
    if section_id is not None:
        section = section_detail(section_id)
        if section is None:
            return {"ok": False, "message": f"报告数据截面 {section_id} 不存在"}
    else:
        report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id])
        if report is None:
            return {"ok": False, "message": f"报告 {report_id} 不存在"}
        captured = capture_report_section(
            dict(report), wait_for_round=wait_for_round, note="留档时生成截面")
        if not captured.get("ok"):
            return captured
        section = section_detail(int(captured["section_id"]))
        if section is None:
            return {"ok": False, "message": "截面保存失败"}

    content = section["section_content"]
    source_summary = section["source_summary"]
    snapshot_id = db.insert("snapshots", {
        "snapshot_name": snapshot_name,
        "report_id": report_id,
        "snapshot_content": _dumps(content),
        "section_id": section["id"],
        "round_id": section.get("round_id"),
        "generation_key": section.get("section_key"),
        "source_summary": _dumps(source_summary),
        "status": "已保存",
    })
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "snapshot_name": snapshot_name,
        "report_id": report_id,
        "section_id": section["id"],
        "section_key": section.get("section_key"),
        "round_id": section.get("round_id"),
        "round_key": section.get("round_key"),
        "data_mode": section.get("data_mode"),
        "is_complete": bool(section.get("is_complete")),
        "is_consistent": bool(section.get("is_consistent")),
        "waited_for_round": bool(section.get("waited_for_round")),
        "pending_round_id": section.get("pending_round_id"),
        "pending_round_status": section.get("pending_round_status"),
        "cutoff_at": section.get("cutoff_at"),
        "source_summary": source_summary,
        "content": content,
    }


def snapshot_detail(snapshot_id: int) -> dict[str, Any] | None:
    row = db.fetchone("SELECT * FROM snapshots WHERE id=?", [snapshot_id])
    if row is None:
        return None
    row["snapshot_content"] = _loads(row.get("snapshot_content"), {})
    row["source_summary"] = _loads(row.get("source_summary"), [])
    return row


def list_snapshots(report_id: int) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT * FROM snapshots WHERE report_id=? ORDER BY id DESC", [report_id],
    )
    for row in rows:
        row["snapshot_content"] = _loads(row.get("snapshot_content"), {})
        row["source_summary"] = _loads(row.get("source_summary"), [])
    return rows


def _same_source(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("section_id") and a.get("section_id") == b.get("section_id"):
        return True
    if a.get("generation_key") and a.get("generation_key") == b.get("generation_key"):
        return True
    return bool(a.get("round_id") and a.get("round_id") == b.get("round_id"))


def compare_snapshots(snapshot_a_id: int, snapshot_b_id: int) -> dict[str, Any]:
    """按指标编号对齐两份留档，逐指标给出差值。

    a 视为旧轮、b 视为新轮；diff = b - a。栏目顺序不同不影响对应关系。
    """
    a = snapshot_detail(snapshot_a_id)
    b = snapshot_detail(snapshot_b_id)
    if a is None or b is None:
        return {"ok": False, "message": "快照不存在"}

    content_a = a["snapshot_content"] or {}
    content_b = b["snapshot_content"] or {}
    map_a = section_metric_map(content_a)
    map_b = section_metric_map(content_b)

    details: list[dict[str, Any]] = []
    for key in list(map_a.keys()) + [k for k in map_b.keys() if k not in map_a]:
        section_a = map_a.get(key, {})
        section_b = map_b.get(key, {})
        label = section_b.get("metric_name") or section_a.get("metric_name") or key
        metric_id = section_b.get("metric_id", section_a.get("metric_id"))
        try:
            metric_id = int(metric_id) if metric_id is not None else key
        except (TypeError, ValueError):
            pass
        value_a = _as_float(section_a.get("current_value"))
        value_b = _as_float(section_b.get("current_value"))
        diff = None
        diff_pct = None
        if value_a is not None and value_b is not None:
            diff = round(value_b - value_a, 4)
            if value_a != 0:
                diff_pct = round((value_b - value_a) / abs(value_a) * 100, 2)
        round_a = section_a.get("round_id")
        round_b = section_b.get("round_id")
        details.append({
            "metric_id": metric_id,
            "metric_name": label,
            "unit": section_b.get("unit") or section_a.get("unit"),
            "value_a": value_a,
            "value_b": value_b,
            "old_value": value_a,
            "new_value": value_b,
            "diff": diff,
            "diff_value": diff,
            "delta": diff,
            "abs_diff": abs(diff) if diff is not None else None,
            "diff_pct": diff_pct,
            "has_diff": bool(diff is not None and abs(diff) > 1e-9),
            "round_id_a": round_a,
            "round_id_b": round_b,
            "source_a": section_a.get("source_time"),
            "source_b": section_b.get("source_time"),
        })

    same_source = _same_source(a, b)
    changed = [detail for detail in details if detail["has_diff"]]
    summary = {
        "snapshot_a": snapshot_a_id,
        "snapshot_b": snapshot_b_id,
        "snapshot_a_name": a.get("snapshot_name"),
        "snapshot_b_name": b.get("snapshot_name"),
        "snapshot_a_time": a.get("created_at"),
        "snapshot_b_time": b.get("created_at"),
        "round_a": a.get("round_id"),
        "round_b": b.get("round_id"),
        "section_a": a.get("section_id"),
        "section_b": b.get("section_id"),
        "same_source": same_source,
        "source_note": (
            "两份快照来自同一次报告生成，来源相同，差值应为 0"
            if same_source else "两份快照来自不同轮次/不同生成批次"
        ),
        "total_metrics": len(details),
        "diffs_found": len(changed),
        "significant_diffs": len([d for d in details if d["diff_pct"] is not None and abs(d["diff_pct"]) > 5]),
        "details": details,
    }

    has_difference = bool(changed) and not same_source
    comparison_id = db.insert("snapshot_comparisons", {
        "snapshot_a_id": snapshot_a_id,
        "snapshot_b_id": snapshot_b_id,
        "same_source": 1 if same_source else 0,
        "has_difference": 1 if has_difference else 0,
        "result_content": _dumps(summary),
    })

    # 仅补写尚未记录过的差异摘要，已留档快照的汇总内容永远不变。
    for snapshot in (a, b):
        if not snapshot.get("comparison_diff"):
            db.update("snapshots", {
                "comparison_diff": _dumps(summary),
                "status": "有差异" if has_difference else "已对比",
            }, "id=?", [int(snapshot["id"])])

    return {
        "ok": True,
        "comparison_id": comparison_id,
        "same_source": same_source,
        "has_difference": has_difference,
        "summary": summary,
        "diffs": details,
    }


def ensure_demo_daily_report() -> dict[str, Any] | None:
    """准备一份可演示的「经营日报」：收入/退款两项指标 + 一轮完整数据 + 已留档基准。

    已有同名报告时直接复用，不重复造数。
    """
    existing = db.fetchone("SELECT * FROM reports WHERE report_name=?", ["经营日报"])
    if existing and db.count("report_sections", "report_id=?", [int(existing["id"])]):
        return {"report_id": int(existing["id"]), "created": False}

    metric_specs = [("收入", "gauge", "元"), ("退款", "gauge", "元")]
    metric_ids: list[int] = []
    for name, mtype, unit in metric_specs:
        row = db.fetchone("SELECT * FROM metrics WHERE metric_name=?", [name])
        if row is None:
            metric_ids.append(db.insert("metrics", {
                "metric_name": name,
                "metric_type": mtype,
                "source_module": "realtime_agg",
                "unit": unit,
            }))
        else:
            metric_ids.append(int(row["id"]))

    if existing:
        report_id = int(existing["id"])
        db.update("reports", {"metric_ids": _dumps(metric_ids)}, "id=?", [report_id])
    else:
        report_id = db.insert("reports", {
            "report_name": "经营日报",
            "metric_ids": _dumps(metric_ids),
            "report_period": "daily",
            "output_format": "json",
            "status": "已发布",
        })

    baseline_round = create_round(report_id, metric_ids, note="演示基准轮")
    write_metric_value(int(baseline_round["id"]), metric_ids[0], 100, data_source="demo")
    write_metric_value(int(baseline_round["id"]), metric_ids[1], 10, data_source="demo")
    report = db.fetchone("SELECT * FROM reports WHERE id=?", [report_id])
    captured = capture_report_section(dict(report), note="演示基准截面")
    create_snapshot(report_id, "经营日报基准留档", section_id=int(captured["section_id"]))
    return {
        "report_id": report_id,
        "metric_ids": metric_ids,
        "round_id": int(baseline_round["id"]),
        "section_id": captured["section_id"],
        "created": True,
    }


def list_comparisons(snapshot_id: int | None = None) -> list[dict[str, Any]]:
    if snapshot_id is None:
        rows = db.fetchall("SELECT * FROM snapshot_comparisons ORDER BY id DESC")
    else:
        rows = db.fetchall(
            "SELECT * FROM snapshot_comparisons WHERE snapshot_a_id=? OR snapshot_b_id=? ORDER BY id DESC",
            [snapshot_id, snapshot_id],
        )
    for row in rows:
        row["result_content"] = _loads(row.get("result_content"), {})
    return rows
