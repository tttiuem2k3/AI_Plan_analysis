from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from math import ceil
from typing import Any, Dict, List, Optional, Tuple
import logging

from .enums import ResourceMode
from .models import Operation, ParsedInput, ScheduledOperation

logger = logging.getLogger(__name__)


def detect_unplanned_branch(parsed: ParsedInput) -> bool:
    """Detect the special branch where EndDateManufactering is not finalized.

    Business meaning:
    - Missing plan-level manufacturing end date indicates planning values may not be computed yet.
    - This is NOT a fatal error; optimizer should complete missing values.
    """

    for plan in parsed.main_plans:
        if plan.end_manufacturing is None:
            return True
    return False


def exclude_machine_based_ops_from_labor_recommendation(operations: List[Operation]) -> None:
    """Clear labor recommendation for machine-based operations.

    Rule:
    - If an operation is machine-based, do not recommend extra workers in this branch.
    """

    for op in operations:
        if op.resource_mode == ResourceMode.MACHINE or op.resource_machine > 0:
            op.recommended_resource_worker = None


def estimate_required_workers_for_labor_op(op: Operation) -> Tuple[int, int]:
    """Estimate minimum and maximum worker recommendation for a labor operation.

    Returns:
    - (recommended_resource_worker, max_resource_worker)

    Notes:
    - Uses 8h regular + 4h overtime/day and TimeLimit as minutes/unit.
    - Chooses minimum workers that can meet TotalQuantity by DueDate if possible.
    """

    if op.resource_mode != ResourceMode.LABOR:
        return 0, max(0, int(op.max_resource_worker or 0))

    max_worker = max(0, int(op.max_resource_worker or op.resource_worker or 0))
    if max_worker <= 0:
        return 0, 0

    days_before_due = max(1, (op.due_dt.date() - op.start_dt.date()).days + 1)
    denom = max(0.1, float(op.time_limit_min_per_unit))

    # Per worker/day capacity (regular + overtime)
    qty_per_worker_per_day = (12.0 * 60.0) / denom
    if qty_per_worker_per_day <= 0:
        return min(max_worker, max(1, int(op.resource_worker or 1))), max_worker

    required_workers = ceil(float(op.total_qty) / float(days_before_due) / float(qty_per_worker_per_day))
    required_workers = max(1, int(required_workers))
    recommended = min(max_worker, required_workers)
    return recommended, max_worker


def _compute_daily_caps(op: Operation, worker_for_labor: int) -> Tuple[int, int]:
    tl = max(0.1, float(op.time_limit_min_per_unit))
    if op.resource_mode == ResourceMode.MACHINE:
        setups = [max(0, int(c.setup_minutes)) for c in (op.resource_candidates or [])]
        if not setups:
            setups = [0]
        regular_minutes = sum(max(0, 8 * 60 - s) for s in setups)
        overtime_minutes = sum(4 * 60 for _ in setups)
        return int(regular_minutes / tl), int(overtime_minutes / tl)

    regular_minutes = max(0, int(worker_for_labor) * 8 * 60)
    overtime_minutes = max(0, int(worker_for_labor) * 4 * 60)
    return int(regular_minutes / tl), int(overtime_minutes / tl)


def build_daily_qty_schedule(
    *,
    start_dt: datetime,
    end_dt: datetime,
    total_qty: int,
    production_capacity: int,
    overtime_capacity: int,
) -> List[Dict[str, Any]]:
    """Allocate DailyQty from start to end with daily cap constraints.

    Output format is JSON-serializable and matches API expectation:
    - ProductionDate
    - Quantity
    """

    out: List[Dict[str, Any]] = []
    if total_qty <= 0:
        return out

    max_per_day = max(0, int(production_capacity) + int(overtime_capacity))
    if max_per_day <= 0:
        return out

    cur = start_dt.date()
    end_day = end_dt.date()
    remaining = int(total_qty)

    while cur <= end_day and remaining > 0:
        qty = min(max_per_day, remaining)
        out.append({"ProductionDate": cur.isoformat(), "Quantity": int(qty)})
        remaining -= qty
        cur = cur + timedelta(days=1)

    return out


def compute_plan_end_date_manufacturing(detail_updates: List[Dict[str, Any]]) -> Optional[str]:
    """Infer plan-level EndDateManufactering from updated detail EndDate values."""

    latest: Optional[datetime] = None
    for row in detail_updates:
        e = row.get("EndDate")
        if e in (None, ""):
            continue
        try:
            dt = datetime.fromisoformat(str(e)[:19].replace("T", " "))
        except Exception:
            try:
                dt = datetime.fromisoformat(str(e)[:10])
            except Exception:
                continue
        latest = dt if latest is None or dt > latest else latest

    return latest.isoformat(sep="T", timespec="seconds") if latest else None


def compute_earliest_possible_due_date_if_missed(detail_updates: List[Dict[str, Any]]) -> Optional[str]:
    """Compute earliest feasible due date if current due dates are missed."""

    latest_required: Optional[datetime] = None
    has_miss = False

    for row in detail_updates:
        late_qty = int(row.get("LateQty") or 0)
        unplanned_qty = int(row.get("UnplannedQty") or 0)
        if late_qty > 0 or unplanned_qty > 0:
            has_miss = True

        end_raw = row.get("EndDate")
        if end_raw in (None, ""):
            continue
        try:
            end_dt = datetime.fromisoformat(str(end_raw)[:19].replace("T", " "))
        except Exception:
            try:
                end_dt = datetime.fromisoformat(str(end_raw)[:10])
            except Exception:
                continue
        latest_required = end_dt if latest_required is None or end_dt > latest_required else latest_required

    if not has_miss:
        return None
    return latest_required.date().isoformat() if latest_required else None


def _late_unplanned_by_due(total_qty: int, due_dt: datetime, daily_qty: List[Dict[str, Any]], max_per_day: int) -> Tuple[int, int]:
    if total_qty <= 0:
        return 0, 0

    qty_before_due = 0
    qty_total_scheduled = 0
    for r in daily_qty:
        try:
            q = int(r.get("Quantity") or 0)
            d = date.fromisoformat(str(r.get("ProductionDate") or "")[:10])
        except Exception:
            continue
        qty_total_scheduled += q
        if d <= due_dt.date():
            qty_before_due += q

    qty_before_due = max(0, min(total_qty, qty_before_due))
    late_qty = max(0, total_qty - qty_before_due)

    if max_per_day <= 0:
        unplanned_qty = total_qty
    else:
        unplanned_qty = max(0, total_qty - qty_total_scheduled)

    return int(late_qty), int(unplanned_qty)


def recompute_missing_detail_fields(
    *,
    parsed: ParsedInput,
    operations: List[Operation],
    scheduled_operations: List[ScheduledOperation],
) -> Dict[str, Any]:
    """Recompute missing/zero planning fields for EndDateManufactering=null branch.

    For each operation in main plans, compute and populate:
    - EndDate
    - ResourceWorker (labor only, recommendation)
    - ProductionCapacity
    - OvertimeCapacity
    - DailyQty
    - LateQty
    - UnplannedQty
    and infer plan-level EndDateManufactering.
    """

    scheduled_map: Dict[str, ScheduledOperation] = {x.operation_id: x for x in scheduled_operations}
    details: List[Dict[str, Any]] = []

    for op in operations:
        if not bool(op.is_missing_planning_values):
            details.append(
                {
                    "OperationID": op.operation_id,
                    "PlanNo": op.plan_no,
                    "EndDate": op.end_dt.isoformat(sep="T", timespec="seconds"),
                    "ResourceWorker": int(op.resource_worker or 0),
                    "recommended_resource_worker": None,
                    "max_resource_worker": int(op.max_resource_worker or 0) if op.resource_mode == ResourceMode.LABOR else None,
                    "ProductionCapacity": int(op.input_production_capacity or 0),
                    "OvertimeCapacity": int(op.input_overtime_capacity or 0),
                    "regular_capacity": int(op.input_production_capacity or 0),
                    "overtime_capacity": int(op.input_overtime_capacity or 0),
                    "DailyQty": list(op.input_daily_qty or []),
                    "LateQty": int(op.input_late_qty or 0),
                    "UnplannedQty": int(op.input_unplanned_qty or 0),
                    "resource_mode": op.resource_mode.value,
                }
            )
            continue

        sched = scheduled_map.get(op.operation_id)
        start_dt = sched.start_dt if sched else op.start_dt

        if op.resource_mode == ResourceMode.LABOR:
            rec_worker, max_worker = estimate_required_workers_for_labor_op(op)
            if rec_worker > 0:
                op.recommended_resource_worker = rec_worker
                op.resource_worker = rec_worker
            op.max_resource_worker = max_worker
        else:
            op.recommended_resource_worker = None

        worker_for_labor = int(op.resource_worker or 0)
        prod_cap, ot_cap = _compute_daily_caps(op, worker_for_labor)

        max_per_day = max(0, prod_cap + ot_cap)
        if max_per_day <= 0:
            computed_days = 1
        else:
            computed_days = max(1, ceil(float(op.total_qty) / float(max_per_day)))

        estimated_end_dt = start_dt + timedelta(days=computed_days - 1)
        if sched and sched.end_dt > estimated_end_dt:
            estimated_end_dt = sched.end_dt

        daily = build_daily_qty_schedule(
            start_dt=start_dt,
            end_dt=estimated_end_dt,
            total_qty=max(0, int(op.total_qty)),
            production_capacity=prod_cap,
            overtime_capacity=ot_cap,
        )

        late_qty, unplanned_qty = _late_unplanned_by_due(max(0, int(op.total_qty)), op.due_dt, daily, max_per_day)

        op.recomputed_end_dt = estimated_end_dt
        op.recomputed_daily_qty = daily
        op.recomputed_late_qty = int(late_qty)
        op.recomputed_unplanned_qty = int(unplanned_qty)
        op.recomputed_production_capacity = int(prod_cap)
        op.recomputed_overtime_capacity = int(ot_cap)

        details.append(
            {
                "OperationID": op.operation_id,
                "PlanNo": op.plan_no,
                "EndDate": estimated_end_dt.isoformat(sep="T", timespec="seconds"),
                "ResourceWorker": int(op.recommended_resource_worker if op.resource_mode == ResourceMode.LABOR and op.recommended_resource_worker is not None else op.resource_worker),
                "recommended_resource_worker": int(op.recommended_resource_worker or 0) if op.resource_mode == ResourceMode.LABOR else None,
                "max_resource_worker": int(op.max_resource_worker or 0) if op.resource_mode == ResourceMode.LABOR else None,
                "ProductionCapacity": int(prod_cap),
                "OvertimeCapacity": int(ot_cap),
                "regular_capacity": int(prod_cap),
                "overtime_capacity": int(ot_cap),
                "DailyQty": daily,
                "LateQty": int(late_qty),
                "UnplannedQty": int(unplanned_qty),
                "resource_mode": op.resource_mode.value,
            }
        )

    exclude_machine_based_ops_from_labor_recommendation(operations)

    plan_end = compute_plan_end_date_manufacturing(details)
    earliest_due = compute_earliest_possible_due_date_if_missed(details)

    logger.info(
        "Recomputed missing detail fields for unplanned branch: details=%s plan_end=%s earliest_due=%s",
        len(details),
        plan_end,
        earliest_due,
    )

    return {
        "detail_updates": details,
        "plan_end_date_manufacturing": plan_end,
        "earliest_due_date_if_missed": earliest_due,
    }
