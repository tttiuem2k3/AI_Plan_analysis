from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List

from .enums import Severity
from .models import (
    ConflictItem,
    ConflictReport,
    Hotspots,
    KpiSnapshot,
    Operation,
    ScheduledOperation,
    SolverRawResult,
)


def _is_labor_resource_code(resource_id: str) -> bool:
    return str(resource_id or "").strip().upper() == "M000"


def detect_conflicts(operations: List[Operation], related_constraints) -> ConflictReport:
    items: List[ConflictItem] = []

    # Baseline machine overlaps in input window.
    by_resource: Dict[str, List[Operation]] = defaultdict(list)
    for op in operations:
        for r in op.resource_candidates:
            by_resource[r.resource_id].append(op)

    for rid, ops in by_resource.items():
        ops = sorted(ops, key=lambda x: x.start_dt)
        for i in range(1, len(ops)):
            prev = ops[i - 1]
            cur = ops[i]
            if cur.start_dt < prev.end_dt:
                items.append(
                    ConflictItem(
                        code="MACHINE_OVERLAP",
                        severity=Severity.ERROR,
                        operation_id=cur.operation_id,
                        message="Xung đột nguồn lực máy trong lịch baseline.",
                        context={"machine": rid, "prev": prev.operation_id, "cur": cur.operation_id},
                    )
                )

    by_phase: Dict[str, List[Operation]] = defaultdict(list)
    for op in operations:
        if op.phase_group:
            by_phase[op.phase_group].append(op)
    for phase, ops in by_phase.items():
        ops = sorted(ops, key=lambda x: x.start_dt)
        for i in range(1, len(ops)):
            prev = ops[i - 1]
            cur = ops[i]
            if cur.start_dt < prev.end_dt:
                items.append(
                    ConflictItem(
                        code="PHASE_GROUP_OVERLAP",
                        severity=Severity.ERROR,
                        operation_id=cur.operation_id,
                        message="Xung đột phase group trong lịch baseline.",
                        context={"phase_group": phase, "prev": prev.operation_id, "cur": cur.operation_id},
                    )
                )

    for op in operations:
        for r in op.resource_candidates:
            for bi in related_constraints.resource_busy_intervals.get(r.resource_id, []):
                op_start = op.start_dt
                op_end = op.end_dt
                bi_start = related_constraints.resource_busy_intervals[r.resource_id][0].start_min
                _ = bi_start
                # only warn if baseline touches related plan window shape
                # actual hard constraints handled in CP-SAT solve.
                items.append(
                    ConflictItem(
                        code="RELATED_PLAN_RESOURCE_BUSY",
                        severity=Severity.WARNING,
                        operation_id=op.operation_id,
                        message="Nguồn lực có thể cấn với kế hoạch liên quan.",
                        context={"machine": r.resource_id, "related_plan": bi.plan_no},
                    )
                )
                break

    return ConflictReport(items=items)


def _late_unplanned_for_op(op: Operation, sched: ScheduledOperation) -> tuple[int, int]:
    input_late = max(0, int(op.input_late_qty or 0))
    input_unplanned = max(0, int(op.input_unplanned_qty or 0))

    if sched.end_dt <= op.due_dt:
        return input_late, input_unplanned
    tardy_min = max(0, int((sched.end_dt - op.due_dt).total_seconds() / 60))
    if op.required_minutes <= 0:
        return input_late, input_unplanned
    ratio = min(1.0, tardy_min / max(1, op.required_minutes))
    recomputed_late_qty = int(round(op.total_qty * ratio))

    theoretical_before_due = min(op.max_possible_qty_before_due, op.total_qty)
    recomputed_unplanned_qty = max(0, op.total_qty - theoretical_before_due)

    # The client payload can already contain business-computed shortage values.
    # Treat those values as authoritative lower bounds so KPI text and LLM prompts
    # do not accidentally erase late/unplanned quantities during solver analysis.
    late_qty = max(input_late, recomputed_late_qty)
    unplanned_qty = max(input_unplanned, recomputed_unplanned_qty)
    return max(0, late_qty), max(0, unplanned_qty)


def analyze_solution(operations: List[Operation], solved: SolverRawResult) -> tuple[KpiSnapshot, Hotspots, List[Dict], Dict]:
    op_map = {op.operation_id: op for op in operations}
    late_ops = 0
    total_late = 0
    total_unplanned = 0
    total_tardy = 0
    resource_minutes: Dict[str, int] = defaultdict(int)

    problem_rows: List[Dict] = []
    blocked = []

    scheduled_ids = set()
    for so in solved.scheduled_operations:
        op = op_map.get(so.operation_id)
        if op is None:
            continue
        scheduled_ids.add(so.operation_id)
        duration = max(0, so.end_min - so.start_min)
        resource_minutes[so.assigned_resource] += duration
        late_qty, unplanned_qty = _late_unplanned_for_op(op, so)
        if late_qty > 0 or unplanned_qty > 0 or so.tardiness_minutes > 0:
            late_ops += 1
        total_late += late_qty
        total_unplanned += unplanned_qty
        total_tardy += so.tardiness_minutes

        is_blocked = bool(op.predecessor_id)
        if is_blocked:
            blocked.append(op.operation_id)

        if late_qty > 0 or unplanned_qty > 0 or so.tardiness_minutes > 0 or is_blocked:
            problem_rows.append(
                {
                    "plan_no": op.plan_no,
                    "operation_id": op.operation_id,
                    "so_don_hang": op.order_no,
                    "khach_hang": op.customer,
                    "phase_id": op.phase_id,
                    "phase_name": op.phase_name,
                    "phase_group": op.phase_group,
                    "finished_product_code": op.finished_product_code,
                    "finished_product_name": op.finished_product_name,
                    "semi_finished_product_code": op.semi_finished_product_code,
                    "semi_finished_product_name": op.semi_finished_product_name,
                    "resource": {"code": so.assigned_resource, "name": op.resource_names},
                    "due_date": op.due_dt.isoformat(),
                    "expected_end_date": so.end_dt.isoformat(),
                    "total_qty": op.total_qty,
                    "capacity_per_day": op.production_capacity_calc,
                    "overtime_capacity_per_day": op.overtime_capacity_calc,
                    "late_qty": late_qty,
                    "unplanned_qty": unplanned_qty,
                    "dependency": {
                        "is_blocked_by_predecessor": is_blocked,
                        "blocked_by": op.predecessor_id,
                    },
                    "workforce": {
                        "regular_staff_required": max(1, op.resource_worker),
                        "ot_staff_recommended": max(0, round(op.resource_worker * 0.2)),
                        "ot_staff_max": max(1, op.resource_worker),
                        "outsource_staff_recommended": max(0, round(op.resource_worker * 0.1)),
                        "outsource_ot_staff_recommended": max(0, round(op.resource_worker * 0.05)),
                    },
                    "dept": op.phase_group,
                }
            )

    # If an operation cannot be scheduled, treat as fully unplanned and severe late risk.
    for op in operations:
        if op.operation_id in scheduled_ids:
            continue
        late_ops += 1
        total_unplanned += max(0, op.total_qty)
        total_late += max(0, op.total_qty)
        total_tardy += max(0, op.required_minutes)
        if op.predecessor_id:
            blocked.append(op.operation_id)
        problem_rows.append(
            {
                "plan_no": op.plan_no,
                "operation_id": op.operation_id,
                "so_don_hang": op.order_no,
                "khach_hang": op.customer,
                "phase_id": op.phase_id,
                "phase_name": op.phase_name,
                "phase_group": op.phase_group,
                "finished_product_code": op.finished_product_code,
                "finished_product_name": op.finished_product_name,
                "semi_finished_product_code": op.semi_finished_product_code,
                "semi_finished_product_name": op.semi_finished_product_name,
                "resource": {
                    "code": (op.resource_candidates[0].resource_id if op.resource_candidates else "LABOR"),
                    "name": op.resource_names,
                },
                "due_date": op.due_dt.isoformat(),
                "expected_end_date": None,
                "total_qty": op.total_qty,
                "capacity_per_day": op.production_capacity_calc,
                "overtime_capacity_per_day": op.overtime_capacity_calc,
                "late_qty": max(0, op.total_qty),
                "unplanned_qty": max(0, op.total_qty),
                "dependency": {
                    "is_blocked_by_predecessor": bool(op.predecessor_id),
                    "blocked_by": op.predecessor_id,
                },
                "workforce": {
                    "regular_staff_required": max(1, op.resource_worker),
                    "ot_staff_recommended": max(0, round(op.resource_worker * 0.2)),
                    "ot_staff_max": max(1, op.resource_worker),
                    "outsource_staff_recommended": max(0, round(op.resource_worker * 0.1)),
                    "outsource_ot_staff_recommended": max(0, round(op.resource_worker * 0.05)),
                },
                "dept": op.phase_group,
            }
        )

    total_ops = len(operations)
    horizon_minutes = max(1, int((max(x.end_dt for x in solved.scheduled_operations) - solved.horizon_start).total_seconds() / 60)) if solved.scheduled_operations else 1
    utilization = {k: round(min(1.0, v / horizon_minutes), 4) for k, v in resource_minutes.items()}

    machine_counts = Counter(
        x.assigned_resource
        for x in solved.scheduled_operations
        if not _is_labor_resource_code(x.assigned_resource) and str(x.assigned_resource or "").upper() != "LABOR"
    )
    phase_counts = Counter(op_map[x.operation_id].phase_group for x in solved.scheduled_operations if x.operation_id in op_map)

    hotspots = Hotspots(
        bottleneck_machines=[m for m, _ in machine_counts.most_common(5)],
        bottleneck_phase_groups=[p for p, _ in phase_counts.most_common(5)],
        bottleneck_ops=[x["operation_id"] for x in sorted(problem_rows, key=lambda r: (r["late_qty"] + r["unplanned_qty"]), reverse=True)[:5]],
        blocked_operations=list(dict.fromkeys(blocked))[:5],
    )

    kpi = KpiSnapshot(
        feasible=solved.feasible,
        total_operations=total_ops,
        late_ops_count=late_ops,
        total_late_qty=total_late,
        total_unplanned_qty=total_unplanned,
        total_tardiness_minutes=total_tardy,
        overtime_used_minutes=0,
        resource_utilization=utilization,
    )

    problem_rows_sorted = sorted(problem_rows, key=lambda r: (r["late_qty"], r["unplanned_qty"]), reverse=True)
    aux = {
        "resource_minutes": dict(resource_minutes),
    }
    return kpi, hotspots, problem_rows_sorted, aux
