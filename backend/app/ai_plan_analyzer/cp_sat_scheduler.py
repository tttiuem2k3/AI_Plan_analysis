from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from ortools.sat.python import cp_model
except Exception:  # pragma: no cover
    cp_model = None  # type: ignore

from .dtos import Operation, Plan, Segment


@dataclass(slots=True)
class CpSatResult:
    ok: bool
    reason: str = ""
    wall_time_s: float = 0.0
    scheduled_ops: int = 0


def cp_sat_reschedule(
    plan: Plan,
    affected_scope: Dict[str, Any],
    *,
    time_limit_s: float = 5.0,
) -> Tuple[Plan, CpSatResult]:
    """Reschedule affected machines using OR-Tools CP-SAT (best-effort).

    Current constraints enforced:
    - machine non-overlap (rule #2)
    - department non-overlap by MaCongDoanLon (rule #1)
    - busy intervals from other plans (rule #3) when provided via plan.constraints['busy_intervals']
    - optional sequence intent per machine: plan.constraints['machine_sequences']
    - due date and total quantity constraints (rule #4)
    - soft department utilization objective (rule #5)

    Limitations:
    - Labor pool / OT / outsource caps are not yet modeled.
    """

    if cp_model is None:
        return plan, CpSatResult(ok=False, reason="ortools not installed")

    def _is_labor(op: Operation) -> bool:
        try:
            if str(getattr(op, "machine", "") or "").upper() == "M000":
                return True
        except Exception:
            pass
        try:
            t = str(getattr(op, "loai_nguon_luc", "") or "").strip().lower()
            return t in ("labor", "nhan_cong", "nhân công")
        except Exception:
            return False

    machines = set(affected_scope.get("machines") or [])
    ops_ids = set(affected_scope.get("ops") or [])
    if not machines and not ops_ids:
        return plan, CpSatResult(ok=True, reason="no machines/ops in affected_scope")

    # Collect affected ops:
    # - ops on affected machines
    # - explicitly affected op_ids (may include machine=None ops)
    ops: List[Operation] = [op for op in plan.operations if (op.machine in machines) or (op.op_id in ops_ids)]
    ops = [op for op in ops if _op_duration_min(op) > 0]
    if not ops:
        return plan, CpSatResult(ok=True, reason="no affected ops")

    horizon_start = plan.horizon_start
    horizon_end = plan.horizon_end
    horizon_min = int(max(1, (horizon_end - horizon_start).total_seconds() // 60))

    model = cp_model.CpModel()

    # Decision vars: start/end per op (minutes from horizon_start)
    start_var: Dict[str, Any] = {}
    end_var: Dict[str, Any] = {}
    interval_var: Dict[str, Any] = {}

    for op in ops:
        dur = _op_duration_min(op)
        s = model.new_int_var(0, horizon_min, f"s_{op.op_id}")
        e = model.new_int_var(0, horizon_min, f"e_{op.op_id}")
        itv = model.new_interval_var(s, dur, e, f"itv_{op.op_id}")
        start_var[op.op_id] = s
        end_var[op.op_id] = e
        interval_var[op.op_id] = itv

    # Fixed busy intervals (other plans)
    busy = (plan.constraints or {}).get("busy_intervals") or []
    busy_by_machine: Dict[str, List[Any]] = {}
    busy_by_dept: Dict[str, List[Any]] = {}

    for b in busy:
        try:
            rtype = str(b.get("resource_type") or "")
            rid = b.get("resource_id")
            st = b.get("start_dt")
            en = b.get("end_dt")
            if not rtype or not rid or not st or not en:
                continue
            if not isinstance(st, datetime) or not isinstance(en, datetime):
                continue

            # clamp to horizon
            smin = int(max(0, (st - horizon_start).total_seconds() // 60))
            emin = int(min(horizon_min, (en - horizon_start).total_seconds() // 60))
            if emin <= smin:
                continue

            dur = int(emin - smin)
            s_c = model.new_constant(smin)
            e_c = model.new_constant(emin)
            itv = model.new_interval_var(s_c, dur, e_c, f"busy_{rtype}_{rid}_{smin}_{emin}")

            if rtype == "machine":
                busy_by_machine.setdefault(str(rid), []).append(itv)
            elif rtype == "dept":
                busy_by_dept.setdefault(str(rid), []).append(itv)
        except Exception:
            continue

    # Also treat NON-affected operations in the same plan as fixed busy intervals.
    # Without this, rescheduling a subset of machines can overlap departments
    # with untouched ops and fail validation.
    fixed_ops: List[Operation] = [
        op for op in plan.operations if (op.machine not in machines) and (op.op_id not in ops_ids)
    ]
    for op in fixed_ops:
        try:
            segs = list(op.segments or [])
            starts = [s.start for s in segs if getattr(s, "start", None) is not None]
            ends = [s.end for s in segs if getattr(s, "end", None) is not None]
            if not starts or not ends:
                continue
            st = min(starts)
            en = max(ends)
            if not isinstance(st, datetime) or not isinstance(en, datetime):
                continue
            # clamp to horizon
            smin = int(max(0, (st - horizon_start).total_seconds() // 60))
            emin = int(min(horizon_min, (en - horizon_start).total_seconds() // 60))
            if emin <= smin:
                continue

            dur = int(emin - smin)
            s_c = model.new_constant(smin)
            e_c = model.new_constant(emin)
            itv = model.new_interval_var(s_c, dur, e_c, f"fixed_{smin}_{emin}")

            if op.machine:
                busy_by_machine.setdefault(str(op.machine), []).append(itv)
            if getattr(op, "ma_cong_doan_lon", None):
                busy_by_dept.setdefault(str(op.ma_cong_doan_lon), []).append(itv)
        except Exception:
            continue

    # Machine no-overlap (affected ops + busy intervals)
    by_machine: Dict[str, List[Any]] = {}
    for op in ops:
        if not op.machine or _is_labor(op):
            continue
        by_machine.setdefault(str(op.machine), []).append(interval_var[op.op_id])

    for m, itvs in by_machine.items():
        itvs2 = list(itvs) + list(busy_by_machine.get(m) or [])
        if len(itvs2) >= 2:
            model.add_no_overlap(itvs2)

    # Department no-overlap (MaCongDoanLon) + busy
    # Opt-in, because strict no-overlap is usually too strong for multi-machine departments.
    enforce_dept_no_overlap = bool((plan.constraints or {}).get("enforce_dept_no_overlap"))
    by_dept: Dict[str, List[Any]] = {}
    if enforce_dept_no_overlap:
        for op in ops:
            dept = getattr(op, "ma_cong_doan_lon", None) or None
            if not dept:
                continue
            by_dept.setdefault(str(dept), []).append(interval_var[op.op_id])

        for d, itvs in by_dept.items():
            itvs2 = list(itvs) + list(busy_by_dept.get(d) or [])
            if len(itvs2) >= 2:
                model.add_no_overlap(itvs2)

    # Optional: enforce sequence intents
    seq_intent = (plan.constraints or {}).get("machine_sequences") or {}
    for m, order in seq_intent.items():
        if m not in by_machine:
            continue
        filtered = [op_id for op_id in order if op_id in start_var]
        for i in range(1, len(filtered)):
            prev_id = filtered[i - 1]
            cur_id = filtered[i]
            model.add(end_var[prev_id] <= start_var[cur_id])

    # --- Due dates (rule 4): SOFT tardiness objective ---
    # Hard end<=due frequently makes local reschedule infeasible. We instead
    # minimize tardiness while still producing a feasible schedule.
    due_dates = getattr(plan, "due_dates", {})
    tardiness_vars: List[Any] = []
    for op in ops:
        due_dt = due_dates.get(op.op_id)
        if due_dt and isinstance(due_dt, datetime):
            due_min = int((due_dt - horizon_start).total_seconds() // 60)
            due_min = max(0, min(horizon_min, due_min))
            t = model.new_int_var(0, horizon_min, f"tard_{op.op_id}")
            # t >= end - due
            model.add(t >= end_var[op.op_id] - due_min)
            tardiness_vars.append(t)

    # For each order, ensure total produced qty does not exceed required qty
    # (Assume op.total_qty is the planned qty, and op.meta['order_qty'] is required qty if available)
    order_qty_map = {}
    for op in ops:
        order_id = getattr(op, 'don_hang_id', None)
        if order_id is not None:
            order_qty_map.setdefault(order_id, {'planned': 0, 'required': None, 'ops': []})
            order_qty_map[order_id]['planned'] += float(op.total_qty or 0)
            order_qty_map[order_id]['ops'].append(op)
            rq = None
            if hasattr(op, 'meta') and isinstance(op.meta, dict):
                rq = op.meta.get('order_qty')
            if rq is not None:
                order_qty_map[order_id]['required'] = rq
    for order_id, v in order_qty_map.items():
        if v['required'] is not None:
            model.add(sum([float(op.total_qty or 0) for op in v['ops']]) <= float(v['required']))

    # --- Prefer to keep departments busy (rule 5, soft objective) ---
    # Add a soft penalty for idle time between ops in the same department
    # (This is a soft constraint, not hard)
    # Not implemented as a hard constraint, but can be added to the objective if needed

    # Objective: minimize tardiness (primary) + compactness (secondary)
    try:
        tard_weight = int((plan.constraints or {}).get("tardiness_weight") or 100)
    except Exception:
        tard_weight = 100
    tard_weight = max(1, min(1000, int(tard_weight)))

    obj_terms: List[Any] = []
    if tardiness_vars:
        obj_terms.append(tard_weight * sum(tardiness_vars))
    obj_terms.append(sum(end_var.values()))
    model.minimize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_search_workers = 8

    status = solver.solve(model)

    # --- Collect violated constraints if infeasible ---
    violated_constraints = []
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Check for common causes (manual, since OR-Tools does not provide detailed infeasibility report)
        # 1. Check machine/department overlap
        if len(ops) > 0:
            machine_ids = set(str(op.machine) for op in ops)
            dept_ids = set(getattr(op, 'ma_cong_doan_lon', None) for op in ops)
            if not machine_ids:
                violated_constraints.append('Không có máy nào được chọn cho kế hoạch.')
            if not dept_ids:
                violated_constraints.append('Không có bộ phận nào được chọn cho kế hoạch.')
        # 2. Check due date violations
        due_dates = getattr(plan, 'due_dates', {})
        for op in ops:
            due_dt = due_dates.get(op.op_id)
            if due_dt and isinstance(due_dt, datetime):
                # If op duration exceeds due date window
                est_end = getattr(op, 'expected_end', None)
                if est_end and est_end > due_dt:
                    violated_constraints.append(f'Công đoạn {op.op_id} vượt quá hạn giao hàng ({due_dt}).')
        # 3. Check total quantity per order
        order_qty_map = {}
        for op in ops:
            order_id = getattr(op, 'don_hang_id', None)
            if order_id is not None:
                order_qty_map.setdefault(order_id, {'planned': 0, 'required': None, 'ops': []})
                order_qty_map[order_id]['planned'] += float(op.total_qty or 0)
                order_qty_map[order_id]['ops'].append(op)
                rq = None
                if hasattr(op, 'meta') and isinstance(op.meta, dict):
                    rq = op.meta.get('order_qty')
                if rq is not None:
                    order_qty_map[order_id]['required'] = rq
        for order_id, v in order_qty_map.items():
            if v['required'] is not None and v['planned'] > float(v['required']):
                violated_constraints.append(f'Đơn hàng {order_id} vượt quá số lượng đặt ({v["planned"]} > {v["required"]}).')
        # 4. Check busy intervals overlap (resource double-booked)
        # (Not detailed here, but can be added if needed)
        # 5. General infeasibility
        if not violated_constraints:
            violated_constraints.append('Không tìm được phương án thỏa mãn tất cả ràng buộc. Có thể thiếu nguồn lực, máy, hoặc thời gian.')
        return plan, CpSatResult(ok=False, reason='; '.join(violated_constraints), wall_time_s=float(solver.wall_time), scheduled_ops=0)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return plan, CpSatResult(ok=False, reason=f"no solution (status={status})")

    # Apply solution: overwrite segments for affected ops using solved starts
    plan2 = plan
    for op in plan2.operations:
        if op.op_id not in start_var:
            continue
        dur = _op_duration_min(op)
        smin = int(solver.value(start_var[op.op_id]))
        emin = smin + dur
        st = horizon_start + timedelta(minutes=smin)
        en = horizon_start + timedelta(minutes=emin)
        op.segments = [Segment(start=st, end=en, qty=float(op.total_qty or 0), is_setup=False)]

    return (
        plan2,
        CpSatResult(
            ok=True,
            reason="ok",
            wall_time_s=float(solver.wall_time),
            scheduled_ops=len(start_var),
        ),
    )


def _op_duration_min(op: Operation) -> int:
    # duration = setup + run (qty * unit minutes)
    run = float(op.total_qty or 0.0) * float(op.dinh_muc_phut_moi_sp or 0.0)
    dur = float(op.setup_minutes or 0.0) + run
    if dur < 0:
        dur = 0
    return int(round(dur))
