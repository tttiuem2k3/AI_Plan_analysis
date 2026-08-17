from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .dtos import KPIResult, Operation, Plan


def _op_end_time(op: Operation) -> datetime | None:
    # Prefer segment end; fallback to None
    ends = [s.end for s in op.segments if s.end is not None and not s.is_setup]
    if ends:
        return max(ends)
    return None


def compute_kpi(plan: Plan) -> KPIResult:
    """Compute baseline KPI.

    This implementation is intentionally conservative because the workspace plan can be
    day-bucketed without explicit start/end timestamps.

    You should improve it after mapping Segment.start/end from your calendar builder.
    """

    late_ops = 0
    tardiness_min = 0.0
    setup_min = 0.0

    util_by_machine: Dict[str, float] = defaultdict(float)
    util_by_dept: Dict[str, float] = defaultdict(float)

    # Chỉ tính late_ops/thời gian trễ nếu thực sự có thao tác trễ (end_dt > due và đủ thông tin)
    for op in plan.operations:
        setup_min += float(op.setup_minutes or 0.0)

        due = plan.due_dates.get(op.op_id)
        end_dt = _op_end_time(op)
        # Bỏ qua nếu thiếu due hoặc end_dt
        if not due or not end_dt:
            continue
        if end_dt > due:
            late_ops += 1
            tardiness_min += (end_dt - due).total_seconds() / 60.0

        # Utilization: sum of run minutes per machine/dept based on qty * dm
        run_min = float(op.total_qty or 0.0) * float(op.dinh_muc_phut_moi_sp or 0.0)
        if op.machine:
            util_by_machine[op.machine] += run_min + float(op.setup_minutes or 0.0)
        if op.ten_bo_phan:
            util_by_dept[op.ten_bo_phan] += run_min

    # Bottleneck: top-3 by load
    bottleneck_machines = [k for k, _ in sorted(util_by_machine.items(), key=lambda x: x[1], reverse=True)[:3]]
    bottleneck_depts = [k for k, _ in sorted(util_by_dept.items(), key=lambda x: x[1], reverse=True)[:3]]

    return KPIResult(
        late_ops=late_ops,
        total_tardiness_minutes=round(tardiness_min, 2),
        total_setup_minutes=round(setup_min, 2),
        utilization_by_dept=dict(util_by_dept),
        utilization_by_machine=dict(util_by_machine),
        bottleneck_depts=bottleneck_depts,
        bottleneck_machines=bottleneck_machines,
        idle_windows=[],
    )


def find_hotspots(plan: Plan, kpi: KPIResult) -> Dict[str, Any]:
    """Return compact slices the LLM should focus on."""

    # Hotspot ops: late ops + ops on bottleneck machines
    late: List[Dict[str, Any]] = []
    bottleneck_ops: List[Dict[str, Any]] = []

    bn_machines = set(kpi.bottleneck_machines)

    for op in plan.operations:
        end_dt = _op_end_time(op)
        due = plan.due_dates.get(op.op_id)
        if due and end_dt and end_dt > due:
            late.append(
                {
                    "op_id": op.op_id,
                    "machine": op.machine,
                    "dept": op.ten_bo_phan,
                    "due": due.isoformat(),
                    "end": end_dt.isoformat(),
                    "tardiness_min": round((end_dt - due).total_seconds() / 60.0, 2),
                }
            )
        if op.machine and op.machine in bn_machines:
            bottleneck_ops.append(
                {
                    "op_id": op.op_id,
                    "machine": op.machine,
                    "dept": op.ten_bo_phan,
                    "ten_bo_phan": op.ten_bo_phan,
                    "ten_cong_doan": op.ten_cong_doan,
                    "ten_thanh_pham": op.ten_thanh_pham,
                    "ten_ban_thanh_pham": op.ten_ban_thanh_pham,
                    "so_don_hang": op.so_don_hang,
                    "qty": op.total_qty,
                    "setup_min": op.setup_minutes,
                }
            )

    return {
        "late_ops": late[:30],
        "bottleneck_machines": list(bn_machines),
        "bottleneck_ops": bottleneck_ops[:50],
    }
