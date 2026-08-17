from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Iterable, List

from .models import ConflictReport, Hotspots, KpiSnapshot, ProposalOption


def _drop_empty(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {k: _drop_empty(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {}, ())}
    if isinstance(obj, list):
        out = [_drop_empty(x) for x in obj]
        return [x for x in out if x not in (None, "", [], {}, ())]
    return obj


def _dedupe_keep_order(items: Iterable[Any]) -> List[Any]:
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def build_compact_llm_payload(
    conflict_report: ConflictReport,
    kpi: KpiSnapshot,
    hotspots: Hotspots,
    problem_rows: List[Dict[str, Any]],
    options: List[ProposalOption],
    *,
    top_conflicts: int = 5,
    top_rows: int = 8,
    top_options: int = 3,
) -> Dict[str, Any]:
    conflicts_sorted = sorted(
        [x.to_dict() for x in conflict_report.items],
        key=lambda c: (0 if c.get("severity") == "ERROR" else 1, c.get("code", "")),
    )

    warnings = [c for c in conflicts_sorted if c.get("severity") in ("WARNING", "INFO")][:top_conflicts]
    errors_sample = [c for c in conflicts_sorted if c.get("severity") == "ERROR"][:top_conflicts]

    problem_rows_sorted = sorted(problem_rows, key=lambda r: (r.get("late_qty", 0) + r.get("unplanned_qty", 0)), reverse=True)
    problem_rows_small = []
    for row in problem_rows_sorted[:top_rows]:
        problem_rows_small.append(
            {
                "plan_no": row.get("plan_no"),
                "operation_id": row.get("operation_id"),
                "so_don_hang": row.get("so_don_hang"),
                "khach_hang": row.get("khach_hang"),
                "phase_id": row.get("phase_id"),
                "phase_name": row.get("phase_name"),
                "phase_group": row.get("phase_group"),
                "finished_product_code": row.get("finished_product_code"),
                "finished_product_name": row.get("finished_product_name"),
                "semi_finished_product_code": row.get("semi_finished_product_code"),
                "semi_finished_product_name": row.get("semi_finished_product_name"),
                "resource": row.get("resource"),
                "late_qty": row.get("late_qty", 0),
                "unplanned_qty": row.get("unplanned_qty", 0),
                "dependency": row.get("dependency"),
                "workforce": row.get("workforce"),
                "dept": row.get("dept"),
            }
        )

    options_small = []
    for opt in sorted(options, key=lambda x: (x.rank, -x.score))[:top_options]:
        moves = []
        for mv in opt.moves:
            moves.append(
                {
                    "type": mv.type.value,
                    "reason": mv.reason,
                    "operation_id": mv.operation_id,
                    "machine": mv.machine,
                    "from_machine": mv.from_machine,
                    "to_machine": mv.to_machine,
                    "op_ids_after": mv.op_ids_after,
                    "expected_impact": {
                        "late_ops_delta": mv.expected_impact.get("late_ops_delta", 0),
                        "tardiness_minutes_delta": mv.expected_impact.get("tardiness_minutes_delta", 0),
                        "setup_minutes_delta": mv.expected_impact.get("setup_minutes_delta", 0),
                    },
                }
            )
        options_small.append(
            {
                "id": opt.id,
                "rank": opt.rank,
                "score": round(float(opt.score), 4),
                "title": opt.title,
                "reason_summary": opt.reason_summary,
                "moves": moves,
                "expected_impact": opt.expected_impact,
            }
        )

    payload = {
        "errors": {
            "baseline_validation_errors_count": len([x for x in conflict_report.items if x.severity.value == "ERROR"]),
            "baseline_validation_errors_sample": errors_sample,
            "warnings": warnings[:5],
        },
        "ortools_options": {
            "options": options_small,
        },
        "kpi_snapshot": {
            "feasible": kpi.feasible,
            "total_operations": kpi.total_operations,
            "late_ops_count": kpi.late_ops_count,
            "total_late_qty": kpi.total_late_qty,
            "total_unplanned_qty": kpi.total_unplanned_qty,
            "total_tardiness_minutes": kpi.total_tardiness_minutes,
            "overtime_used_minutes": kpi.overtime_used_minutes,
            "resource_utilization": kpi.resource_utilization,
        },
        "hotspots": {
            "bottleneck_machines": _dedupe_keep_order(hotspots.bottleneck_machines)[:5],
            "bottleneck_phase_groups": _dedupe_keep_order(hotspots.bottleneck_phase_groups)[:5],
            "bottleneck_ops": _dedupe_keep_order(hotspots.bottleneck_ops)[:5],
            "blocked_operations": _dedupe_keep_order(hotspots.blocked_operations)[:5],
        },
        "business": {
            "problem_rows": problem_rows_small,
        },
    }
    return _drop_empty(payload)
