from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Set
import unicodedata

from .enums import ResourceMode
from .models import Operation, ParsedInput


def _is_labor_resource_code(resource_id: str) -> bool:
    return str(resource_id or "").strip().upper() == "M000"


def _is_labor_operation(raw) -> bool:
    if not raw.resources:
        return True
    return all(_is_labor_resource_code(r.resource_id) for r in raw.resources)


def _text_key(value) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.lower().split())


def _calc_capacity_machine(setup_minutes_list: List[int], time_limit: float, regular_hours: int, ot_hours: int) -> tuple[int, int]:
    regular_total = sum(max(0, regular_hours * 60 - max(0, setup)) for setup in setup_minutes_list)
    ot_total = sum(max(0, ot_hours * 60) for _ in setup_minutes_list)
    if time_limit <= 0:
        return 0, 0
    return int(regular_total / time_limit), int(ot_total / time_limit)


def _calc_capacity_labor(worker_count: int, time_limit: float, regular_hours: int, ot_hours: int) -> tuple[int, int]:
    if time_limit <= 0:
        return 0, 0
    regular_qty = (worker_count * regular_hours * 60) / time_limit
    ot_qty = (worker_count * ot_hours * 60) / time_limit
    return int(regular_qty), int(ot_qty)


def _is_working_day(day: date, *, allow_sunday: bool, non_working_dates: Set[str]) -> bool:
    if day.isoformat() in non_working_dates:
        return False
    if not allow_sunday and day.weekday() == 6:
        return False
    return True


def _working_days_between(start_day: date, end_day: date, *, allow_sunday: bool, non_working_dates: Set[str]) -> int:
    if end_day < start_day:
        return 0
    days = 0
    cursor = start_day
    while cursor <= end_day:
        if _is_working_day(cursor, allow_sunday=allow_sunday, non_working_dates=non_working_dates):
            days += 1
        cursor += timedelta(days=1)
    return days


def normalize_operations(parsed: ParsedInput) -> List[Operation]:
    operations: List[Operation] = []
    for plan in parsed.main_plans:
        prev_by_line: Dict[str, str] = {}
        for raw in sorted(plan.operations, key=lambda x: (x.order_no, x.line_key, x.sequence)):
            if _text_key(raw.status) == "cho vat tu" and not parsed.rules.include_waiting_material_status:
                continue
            mode = ResourceMode.LABOR if _is_labor_operation(raw) else ResourceMode.MACHINE
            resource_candidates = [] if mode == ResourceMode.LABOR else list(raw.resources)
            op = Operation(
                operation_id=raw.operation_id,
                plan_no=raw.plan_no,
                order_no=raw.order_no,
                line_key=raw.line_key,
                customer=raw.customer,
                sequence=raw.sequence,
                phase_id=raw.phase_id,
                phase_name=raw.phase_name,
                phase_group=raw.phase_group,
                finished_product_code=raw.finished_product_code,
                finished_product_name=raw.finished_product_name,
                semi_finished_product_code=raw.semi_finished_product_code,
                semi_finished_product_name=raw.semi_finished_product_name,
                resource_candidates=resource_candidates,
                resource_names=raw.resource_names,
                resource_mode=mode,
                resource_worker=raw.resource_worker,
                resource_machine=raw.resource_machine,
                time_limit_min_per_unit=raw.time_limit_min_per_unit,
                total_qty=raw.total_qty,
                start_dt=raw.start_dt,
                end_dt=raw.end_dt,
                due_dt=raw.due_dt,
                predecessor_id=prev_by_line.get(raw.line_key),
                max_resource_worker=max(0, int(raw.max_resource_worker or 0)),
                is_missing_planning_values=bool(raw.is_missing_planning_values),
                input_production_capacity=float(raw.production_capacity_input or 0.0),
                input_overtime_capacity=float(raw.overtime_capacity_input or 0.0),
                input_daily_qty=list(raw.daily_qty_input or []),
                input_late_qty=int(raw.late_qty_input or 0),
                input_unplanned_qty=int(raw.unplanned_qty_input or 0),
                material_available_dt=raw.material_available_dt,
                setup_family=raw.setup_family,
            )
            prev_by_line[raw.line_key] = raw.operation_id
            operations.append(op)
    return operations


def compute_capacities(parsed: ParsedInput, operations: List[Operation]) -> None:
    non_working_dates = set(parsed.rules.non_working_dates or [])
    for op in operations:
        op.required_minutes = int(max(1, op.total_qty) * max(0.1, op.time_limit_min_per_unit))
        op.regular_minutes_per_day = parsed.rules.regular_hours * 60
        op.overtime_minutes_per_day = parsed.rules.overtime_hours * 60

        if op.resource_mode == ResourceMode.LABOR:
            reg, ot = _calc_capacity_labor(op.resource_worker, op.time_limit_min_per_unit, parsed.rules.regular_hours, parsed.rules.overtime_hours)
        else:
            setups = [max(0, r.setup_minutes) for r in op.resource_candidates] or [0]
            reg, ot = _calc_capacity_machine(setups, op.time_limit_min_per_unit, parsed.rules.regular_hours, parsed.rules.overtime_hours)

        if reg + ot <= 0 and (op.input_production_capacity > 0 or op.input_overtime_capacity > 0):
            reg = max(0, int(op.input_production_capacity or 0))
            ot = max(0, int(op.input_overtime_capacity or 0))

        op.production_capacity_calc = reg
        op.overtime_capacity_calc = ot

        days_before_due = max(
            1,
            _working_days_between(
                op.start_dt.date(),
                op.due_dt.date(),
                allow_sunday=parsed.rules.allow_sunday,
                non_working_dates=non_working_dates,
            ),
        )
        op.max_possible_qty_before_due = max(0, (reg + ot) * days_before_due)


def compute_resource_utilization(operations: List[Operation], scheduled_minutes: Dict[str, int], horizon_minutes: int) -> Dict[str, float]:
    by_resource_total: Dict[str, int] = defaultdict(int)
    for op in operations:
        for c in op.resource_candidates[:1]:
            by_resource_total[c.resource_id] += op.required_minutes

    utilization: Dict[str, float] = {}
    for rid, used in scheduled_minutes.items():
        cap = max(1, horizon_minutes)
        utilization[rid] = round(min(1.0, used / cap), 4)
    for rid in by_resource_total:
        utilization.setdefault(rid, 0.0)
    return utilization
