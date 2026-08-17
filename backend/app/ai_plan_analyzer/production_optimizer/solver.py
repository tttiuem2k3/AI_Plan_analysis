from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
import logging
import math
import os

from ortools.sat.python import cp_model

from .constraints import ConstraintBundle
from .exceptions import SolveError
from .models import Operation, ScheduledOperation, SolverRawResult

logger = logging.getLogger(__name__)


def _is_labor_resource_code(resource_id: str) -> bool:
    return str(resource_id or "").strip().upper() == "M000"


def _to_min(dt: datetime, origin: datetime) -> int:
    return int((dt - origin).total_seconds() / 60)


def _is_working_day(day: date, *, allow_sunday: bool, non_working_dates: Set[str]) -> bool:
    if day.isoformat() in non_working_dates:
        return False
    if not allow_sunday and day.weekday() == 6:
        return False
    return True


def _next_working_datetime(dt: datetime, *, allow_sunday: bool, non_working_dates: Set[str]) -> datetime:
    cursor = dt
    while not _is_working_day(cursor.date(), allow_sunday=allow_sunday, non_working_dates=non_working_dates):
        cursor = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time())
    return cursor


def _build_daily_qty(
    *,
    start_dt: datetime,
    end_dt: datetime,
    total_qty: int,
    production_capacity: int,
    overtime_capacity: int,
    allow_sunday: bool,
    non_working_dates: Set[str],
) -> List[Dict[str, Any]]:
    cap_per_day = max(0, int(production_capacity or 0)) + max(0, int(overtime_capacity or 0))
    if total_qty <= 0 or cap_per_day <= 0:
        return []

    remaining = int(total_qty)
    out: List[Dict[str, Any]] = []
    cursor = start_dt.date()
    end_day = max(start_dt.date(), end_dt.date())

    while cursor <= end_day and remaining > 0:
        if _is_working_day(cursor, allow_sunday=allow_sunday, non_working_dates=non_working_dates):
            qty = min(cap_per_day, remaining)
            out.append({"ProductionDate": cursor.isoformat(), "Quantity": qty})
            remaining -= qty
        cursor += timedelta(days=1)
    return out


def _duration_minutes_from_daily_capacity(op: Operation) -> int:
    daily_capacity = max(0, int(op.production_capacity_calc or 0)) + max(0, int(op.overtime_capacity_calc or 0))
    if daily_capacity <= 0 or op.total_qty <= 0:
        return max(1, int(op.required_minutes or 0))
    elapsed_days = max(1, math.ceil(op.total_qty / daily_capacity))
    return elapsed_days * 24 * 60


def _add_fixed_interval_no_overlap(model: cp_model.CpModel, start_var, end_var, busy_start: int, busy_end: int, only_if):
    left = model.NewBoolVar("left_busy")
    right = model.NewBoolVar("right_busy")
    model.Add(end_var <= busy_start).OnlyEnforceIf(left)
    model.Add(start_var >= busy_end).OnlyEnforceIf(right)
    model.AddBoolOr([left, right]).OnlyEnforceIf(only_if)


def solve_operations(
    operations: List[Operation],
    related_constraints: ConstraintBundle,
    time_limit_s: float = 8.0,
    enforce_dept_exclusive: bool = True,
    allow_sunday: bool = True,
    non_working_dates: Optional[List[str]] = None,
) -> SolverRawResult:
    if not operations:
        raise SolveError("No operations to solve")

    non_working = set(non_working_dates or [])
    horizon_start = min(op.start_dt for op in operations)
    horizon_end = max(
        max(op.end_dt, op.due_dt, op.material_available_dt or op.start_dt) + timedelta(minutes=max(0, int(op.required_minutes or 0)))
        for op in operations
    ) + timedelta(days=3)
    horizon = max(120, _to_min(horizon_end, horizon_start))

    model = cp_model.CpModel()

    start_vars: Dict[str, cp_model.IntVar] = {}
    end_vars: Dict[str, cp_model.IntVar] = {}
    interval_main: Dict[str, cp_model.IntervalVar] = {}
    for op in operations:
        duration = _duration_minutes_from_daily_capacity(op)
        material_start = op.material_available_dt if op.material_available_dt and op.material_available_dt > op.start_dt else op.start_dt
        material_start = _next_working_datetime(material_start, allow_sunday=allow_sunday, non_working_dates=non_working)
        earliest = max(0, _to_min(material_start, horizon_start))
        latest_end = min(horizon, max(duration, _to_min(max(op.due_dt, op.end_dt) + timedelta(days=2), horizon_start)))

        start = model.NewIntVar(earliest, max(earliest, latest_end - duration), f"s_{op.operation_id}")
        end = model.NewIntVar(earliest + duration, max(earliest + duration, latest_end), f"e_{op.operation_id}")
        model.Add(end == start + duration)

        start_vars[op.operation_id] = start
        end_vars[op.operation_id] = end
        interval_main[op.operation_id] = model.NewIntervalVar(start, duration, end, f"int_{op.operation_id}")

    by_resource_intervals: Dict[str, List[cp_model.IntervalVar]] = defaultdict(list)
    for op in operations:
        candidates = op.resource_candidates or []
        if not candidates:
            continue
        duration = _duration_minutes_from_daily_capacity(op)
        for c in candidates:
            if _is_labor_resource_code(c.resource_id):
                continue
            by_resource_intervals[c.resource_id].append(interval_main[op.operation_id])

    for rid, intervals in by_resource_intervals.items():
        model.AddNoOverlap(intervals)

    labor_intervals_by_dept: Dict[str, List[cp_model.IntervalVar]] = defaultdict(list)
    labor_demands_by_dept: Dict[str, List[int]] = defaultdict(list)
    labor_capacity_by_dept: Dict[str, int] = defaultdict(int)
    for op in operations:
        if op.resource_candidates:
            continue
        dept = op.phase_group or "LABOR"
        demand = max(1, int(op.resource_worker or 0))
        capacity = max(demand, int(op.max_resource_worker or 0))
        labor_intervals_by_dept[dept].append(interval_main[op.operation_id])
        labor_demands_by_dept[dept].append(demand)
        labor_capacity_by_dept[dept] = max(labor_capacity_by_dept[dept], capacity)

    for dept, intervals in labor_intervals_by_dept.items():
        demands = labor_demands_by_dept[dept]
        capacity = max(1, labor_capacity_by_dept[dept])
        model.AddCumulative(intervals, demands, capacity)

    if enforce_dept_exclusive:
        by_phase_group: Dict[str, List[cp_model.IntervalVar]] = defaultdict(list)
        for op in operations:
            if op.phase_group:
                by_phase_group[op.phase_group].append(interval_main[op.operation_id])

        for _, intervals in by_phase_group.items():
            model.AddNoOverlap(intervals)

    op_map = {op.operation_id: op for op in operations}
    for op in operations:
        if op.predecessor_id and op.predecessor_id in end_vars:
            model.Add(start_vars[op.operation_id] >= end_vars[op.predecessor_id])

    for op in operations:
        candidates = op.resource_candidates or []
        if candidates:
            for c in candidates:
                if _is_labor_resource_code(c.resource_id):
                    continue
                for bi in related_constraints.resource_busy_intervals.get(c.resource_id, []):
                    left = model.NewBoolVar(f"res_left_{op.operation_id}_{c.resource_id}_{bi.start_min}")
                    right = model.NewBoolVar(f"res_right_{op.operation_id}_{c.resource_id}_{bi.start_min}")
                    model.Add(end_vars[op.operation_id] <= bi.start_min).OnlyEnforceIf(left)
                    model.Add(start_vars[op.operation_id] >= bi.end_min).OnlyEnforceIf(right)
                    model.AddBoolOr([left, right])

        for bi in related_constraints.phase_group_busy_intervals.get(op.phase_group, []):
            left = model.NewBoolVar(f"pg_left_{op.operation_id}_{bi.start_min}")
            right = model.NewBoolVar(f"pg_right_{op.operation_id}_{bi.start_min}")
            model.Add(end_vars[op.operation_id] <= bi.start_min).OnlyEnforceIf(left)
            model.Add(start_vars[op.operation_id] >= bi.end_min).OnlyEnforceIf(right)
            model.AddBoolOr([left, right])

    tardy_vars: List[cp_model.IntVar] = []
    overtime_vars: List[cp_model.IntVar] = []
    deviation_vars: List[cp_model.IntVar] = []
    for op in operations:
        due_min = max(0, _to_min(op.due_dt, horizon_start))
        late = model.NewIntVar(0, horizon, f"late_{op.operation_id}")
        model.Add(late >= end_vars[op.operation_id] - due_min)
        tardy_vars.append(late)

        planned_end = max(0, _to_min(op.end_dt, horizon_start))
        dev = model.NewIntVar(0, horizon, f"dev_{op.operation_id}")
        model.Add(dev >= end_vars[op.operation_id] - planned_end)
        model.Add(dev >= planned_end - end_vars[op.operation_id])
        deviation_vars.append(dev)

        day_regular = 8 * 60
        ot = model.NewIntVar(0, horizon, f"ot_{op.operation_id}")
        model.Add(ot >= end_vars[op.operation_id] - (planned_end + day_regular))
        overtime_vars.append(ot)

    model.Minimize(
        1000 * sum(tardy_vars)
        + 500 * sum(overtime_vars)
        + 10 * sum(deviation_vars)
        + sum(end_vars.values())
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(1.0, float(time_limit_s))
    try:
        workers = int(os.getenv("ORTOOLS_NUM_SEARCH_WORKERS", "1") or "1")
    except Exception:
        workers = 1
    solver.parameters.num_search_workers = max(1, min(8, workers))

    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    if not feasible:
        return SolverRawResult(
            status=status_name,
            feasible=False,
            objective_value=0.0,
            horizon_start=horizon_start,
            scheduled_operations=[],
            unscheduled_operation_ids=[op.operation_id for op in operations],
            meta={"reason": "No feasible solution from CP-SAT"},
        )

    scheduled: List[ScheduledOperation] = []
    for op in operations:
        s = int(solver.Value(start_vars[op.operation_id]))
        e = int(solver.Value(end_vars[op.operation_id]))
        assigned = "LABOR"
        if op.resource_candidates:
            machine_codes = [c.resource_id for c in op.resource_candidates if not _is_labor_resource_code(c.resource_id)]
            assigned = ",".join(machine_codes) if machine_codes else "LABOR"

        tardiness = max(0, e - max(0, _to_min(op.due_dt, horizon_start)))
        start_dt = horizon_start + timedelta(minutes=s)
        end_dt = horizon_start + timedelta(minutes=e)
        daily_qty = _build_daily_qty(
            start_dt=start_dt,
            end_dt=end_dt,
            total_qty=op.total_qty,
            production_capacity=op.production_capacity_calc,
            overtime_capacity=op.overtime_capacity_calc,
            allow_sunday=allow_sunday,
            non_working_dates=non_working,
        )
        scheduled.append(
            ScheduledOperation(
                operation_id=op.operation_id,
                plan_no=op.plan_no,
                phase_group=op.phase_group,
                assigned_resource=assigned,
                start_min=s,
                end_min=e,
                start_dt=start_dt,
                end_dt=end_dt,
                due_dt=op.due_dt,
                tardiness_minutes=tardiness,
                daily_qty=daily_qty,
            )
        )

    logger.info("CP-SAT status=%s objective=%s", status_name, solver.ObjectiveValue())
    return SolverRawResult(
        status=status_name,
        feasible=True,
        objective_value=float(solver.ObjectiveValue()),
        horizon_start=horizon_start,
        scheduled_operations=scheduled,
        unscheduled_operation_ids=[],
        meta={
            "wall_time_s": float(solver.WallTime()),
            "calendar": {
                "allow_sunday": allow_sunday,
                "non_working_dates": sorted(non_working),
            },
            "labor_cumulative_depts": sorted(labor_intervals_by_dept.keys()),
        },
    )
