from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Tuple, Any

from .dtos import Operation, Plan, Segment, ValidationError


def _is_labor_resource(op: Operation) -> bool:
    try:
        rid = getattr(op, "machine", None)
        if str(rid or "").upper() == "M000":
            return True
    except Exception:
        pass
    try:
        t = str(getattr(op, "loai_nguon_luc", "") or "").strip().lower()
        return t in ("labor", "nhan_cong", "nhân công")
    except Exception:
        return False


def validate_plan(plan: Plan) -> List[ValidationError]:
    """Validate constraints.

    This validator supports segment-based plans when Segment.start/end is provided.
    For day-bucket plans, only partial checks can be performed.

    Checks implemented:
      - machine overlap (segment-based)
      - department overlap (segment-based) using MaCongDoanLon
      - busy intervals from other plans (rule #3) via plan.constraints['busy_intervals']
      - precedence by thu_tu_sx within same TP/BTP chain (best-effort)
    """

    errors: List[ValidationError] = []

    # Busy intervals from other plans
    busy = (plan.constraints or {}).get("busy_intervals") or []
    busy_machine: Dict[str, List[Tuple[datetime, datetime, dict]]] = defaultdict(list)
    busy_dept: Dict[str, List[Tuple[datetime, datetime, dict]]] = defaultdict(list)

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
            if rtype == "machine":
                busy_machine[str(rid)].append((st, en, dict(b)))
            elif rtype == "dept":
                busy_dept[str(rid)].append((st, en, dict(b)))
        except Exception:
            continue

    # Build quick lookup for business-friendly fields
    op_by_id: Dict[str, Operation] = {str(op.op_id): op for op in getattr(plan, "operations", []) or []}

    def _op_brief(op_id: str) -> Dict[str, Any]:
        op = op_by_id.get(str(op_id))
        if not op:
            return {"op_id": str(op_id)}
        return {
            "op_id": str(op.op_id),
            "don_hang_id": op.don_hang_id,
            "so_don_hang": op.so_don_hang,
            "tp_id": op.tp_id,
            "ten_thanh_pham": op.ten_thanh_pham,
            "btp_id": op.btp_id,
            "ten_ban_thanh_pham": op.ten_ban_thanh_pham,
            "thu_tu_sx": op.thu_tu_sx,
            "ma_cong_doan": op.ma_cong_doan,
            "ten_cong_doan": op.ten_cong_doan,
            "ma_cong_doan_lon": op.ma_cong_doan_lon,
            "ten_bo_phan": op.ten_bo_phan,
            "machine": op.machine,
            "loai_nguon_luc": op.loai_nguon_luc,
        }

    # 1) Machine overlap: for each machine, segments must not overlap
    by_machine: Dict[str, List[Tuple[datetime, datetime, str]]] = defaultdict(list)
    for op in plan.operations:
        if not op.machine or _is_labor_resource(op):
            continue
        for seg in op.segments:
            if seg.start and seg.end:
                by_machine[str(op.machine)].append((seg.start, seg.end, op.op_id))

    for machine, segs in by_machine.items():
        # internal overlap
        segs_sorted = sorted(segs, key=lambda x: x[0])
        for i in range(1, len(segs_sorted)):
            prev_s, prev_e, prev_id = segs_sorted[i - 1]
            cur_s, cur_e, cur_id = segs_sorted[i]
            if cur_s < prev_e:
                errors.append(
                    ValidationError(
                        code="MACHINE_OVERLAP",
                        message=f"Machine '{machine}' overlap between {prev_id} and {cur_id}",
                        context={
                            "machine": machine,
                            "prev": {"op_id": prev_id, "start": prev_s.isoformat(), "end": prev_e.isoformat()},
                            "cur": {"op_id": cur_id, "start": cur_s.isoformat(), "end": cur_e.isoformat()},
                        },
                    )
                )

        # vs busy intervals
        for (s, e, op_id) in segs:
            for (bs, be, meta) in busy_machine.get(machine, []):
                if s < be and bs < e:
                    errors.append(
                        ValidationError(
                            code="MACHINE_BUSY_CONFLICT",
                            message=f"Machine '{machine}' conflicts with busy interval from other plan",
                            context={
                                "machine": machine,
                                "op": {"op_id": op_id, "start": s.isoformat(), "end": e.isoformat()},
                                "busy": {
                                    "start": bs.isoformat(),
                                    "end": be.isoformat(),
                                    "ke_hoach_id": meta.get("ke_hoach_id"),
                                    "segment_id": meta.get("segment_id"),
                                },
                            },
                        )
                    )

    # 1b) Department overlap (MaCongDoanLon)
    # Historically this was modeled as a strict no-overlap, but that is often too strong
    # for departments with multiple machines/staff. Keep it opt-in.
    enforce_dept_no_overlap = bool((plan.constraints or {}).get("enforce_dept_no_overlap"))
    if enforce_dept_no_overlap:
        by_dept: Dict[str, List[Tuple[datetime, datetime, str]]] = defaultdict(list)
        for op in plan.operations:
            dept = getattr(op, "ma_cong_doan_lon", None) or None
            if not dept:
                continue
            for seg in op.segments:
                if seg.start and seg.end:
                    by_dept[str(dept)].append((seg.start, seg.end, op.op_id))

        for dept, segs in by_dept.items():
            segs_sorted = sorted(segs, key=lambda x: x[0])
            for i in range(1, len(segs_sorted)):
                prev_s, prev_e, prev_id = segs_sorted[i - 1]
                cur_s, cur_e, cur_id = segs_sorted[i]
                if cur_s < prev_e:
                    errors.append(
                        ValidationError(
                            code="DEPT_OVERLAP",
                            message=f"Department '{dept}' overlap between {prev_id} and {cur_id}",
                            context={
                                "ma_cong_doan_lon": dept,
                                "ten_bo_phan": (op_by_id.get(str(prev_id)) or op_by_id.get(str(cur_id)) or Operation(op_id=str(prev_id), plan_id=plan.plan_id)).ten_bo_phan
                                if op_by_id else None,
                                "prev": {
                                    **_op_brief(str(prev_id)),
                                    "start": prev_s.isoformat(),
                                    "end": prev_e.isoformat(),
                                },
                                "cur": {
                                    **_op_brief(str(cur_id)),
                                    "start": cur_s.isoformat(),
                                    "end": cur_e.isoformat(),
                                },
                            },
                        )
                    )

            for (s, e, op_id) in segs:
                for (bs, be, meta) in busy_dept.get(dept, []):
                    if s < be and bs < e:
                        errors.append(
                            ValidationError(
                                code="DEPT_BUSY_CONFLICT",
                                message=f"Department '{dept}' conflicts with busy interval from other plan",
                                context={
                                    "ma_cong_doan_lon": dept,
                                    "ten_bo_phan": (op_by_id.get(str(op_id)) or Operation(op_id=str(op_id), plan_id=plan.plan_id)).ten_bo_phan
                                    if op_by_id else None,
                                    "op": {
                                        **_op_brief(str(op_id)),
                                        "start": s.isoformat(),
                                        "end": e.isoformat(),
                                    },
                                    "busy": {
                                        "start": bs.isoformat(),
                                        "end": be.isoformat(),
                                        "ke_hoach_id": meta.get("ke_hoach_id"),
                                        "ma_ke_hoach": meta.get("ma_ke_hoach"),
                                        "segment_id": meta.get("segment_id"),
                                    },
                                },
                            )
                        )

    # 2) Precedence: within same (don_hang, TP/BTP) chain, verify order by thu_tu_sx
    # Best-effort: use max end time of predecessor vs min start time of successor.
    grouped: Dict[tuple, List[Operation]] = defaultdict(list)
    for op in plan.operations:
        key = (op.don_hang_id, op.tp_id, op.btp_id)
        grouped[key].append(op)

    for key, ops in grouped.items():
        ops2 = [o for o in ops if o.thu_tu_sx is not None]
        ops2.sort(key=lambda o: int(o.thu_tu_sx or 0))
        for i in range(1, len(ops2)):
            prev = ops2[i - 1]
            cur = ops2[i]
            prev_end = _max_end(prev)
            cur_start = _min_start(cur)
            if prev_end and cur_start and cur_start < prev_end:
                errors.append(
                    ValidationError(
                        code="PRECEDENCE_VIOLATION",
                        message=f"Precedence violation: {prev.op_id} (order {prev.thu_tu_sx}) ends after {cur.op_id} starts",
                        context={
                            "group": {"don_hang_id": key[0], "tp_id": key[1], "btp_id": key[2]},
                            "prev": {"op_id": prev.op_id, "end": prev_end.isoformat()},
                            "cur": {"op_id": cur.op_id, "start": cur_start.isoformat()},
                        },
                    )
                )

    # 3) Generic resource overlap for non-machine resources (currently: labor pool M000).
    by_resource: Dict[str, List[Tuple[datetime, datetime, str]]] = defaultdict(list)
    for op in plan.operations:
        rid = getattr(op, "machine", None)
        if not rid or not _is_labor_resource(op):
            continue
        for seg in op.segments:
            if seg.start and seg.end:
                by_resource[str(rid)].append((seg.start, seg.end, op.op_id))

    for rid, segs in by_resource.items():
        segs_sorted = sorted(segs, key=lambda x: x[0])
        for i in range(1, len(segs_sorted)):
            prev_s, prev_e, prev_id = segs_sorted[i - 1]
            cur_s, cur_e, cur_id = segs_sorted[i]
            if cur_s < prev_e:
                errors.append(
                    ValidationError(
                        code="RESOURCE_OVERLAP",
                        message=f"Resource '{rid}' overlap between {prev_id} and {cur_id}",
                        context={
                            "resource_id": rid,
                            "prev": {
                                **_op_brief(str(prev_id)),
                                "start": prev_s.isoformat(),
                                "end": prev_e.isoformat(),
                            },
                            "cur": {
                                **_op_brief(str(cur_id)),
                                "start": cur_s.isoformat(),
                                "end": cur_e.isoformat(),
                            },
                        },
                    )
                )

    # 4) Horizon + due date + produced-before-due checks
    hz_start = getattr(plan, "horizon_start", None)
    hz_end = getattr(plan, "horizon_end", None)
    due_dates = getattr(plan, "due_dates", {}) or {}

    for op in plan.operations:
        op_id = str(op.op_id)
        segs = list(getattr(op, "segments", None) or [])
        if not segs:
            continue

        # 4a) Segment must stay within horizon
        for seg in segs:
            if seg.start and hz_start and seg.start < hz_start:
                errors.append(
                    ValidationError(
                        code="OUTSIDE_HORIZON",
                        message=f"Operation '{op_id}' starts before plan horizon",
                        context={
                            "op": {**_op_brief(op_id), "start": seg.start.isoformat()},
                            "horizon_start": hz_start.isoformat() if isinstance(hz_start, datetime) else None,
                        },
                    )
                )
            if seg.end and hz_end and seg.end > hz_end:
                errors.append(
                    ValidationError(
                        code="OUTSIDE_HORIZON",
                        message=f"Operation '{op_id}' ends after plan horizon",
                        context={
                            "op": {**_op_brief(op_id), "end": seg.end.isoformat()},
                            "horizon_end": hz_end.isoformat() if isinstance(hz_end, datetime) else None,
                        },
                    )
                )

        # 4b) Due date must not be exceeded
        op_due = due_dates.get(op_id) or getattr(op, "meta", {}).get("due_dt")
        max_end = _max_end(op)
        if isinstance(op_due, datetime) and isinstance(max_end, datetime) and max_end > op_due:
            errors.append(
                ValidationError(
                    code="DUE_DATE_VIOLATION",
                    message=f"Operation '{op_id}' exceeds due date",
                    context={
                        "op": {**_op_brief(op_id), "max_end": max_end.isoformat()},
                        "due_dt": op_due.isoformat(),
                    },
                )
            )

            # 4c) Produced qty before due must satisfy required qty
            produced_before_due = 0.0
            for seg in segs:
                if seg.end and seg.end <= op_due:
                    produced_before_due += float(getattr(seg, "qty", 0.0) or 0.0)
            required_qty = float(getattr(op, "total_qty", 0.0) or 0.0)
            if produced_before_due + 1e-6 < required_qty:
                errors.append(
                    ValidationError(
                        code="INSUFFICIENT_QTY_BEFORE_DUE",
                        message=f"Operation '{op_id}' cannot finish required quantity before due date",
                        context={
                            "op": _op_brief(op_id),
                            "required_qty": required_qty,
                            "produced_before_due": round(produced_before_due, 4),
                            "shortage_qty": round(max(0.0, required_qty - produced_before_due), 4),
                            "due_dt": op_due.isoformat(),
                        },
                    )
                )

    return errors


def _max_end(op: Operation) -> datetime | None:
    ends = [s.end for s in op.segments if s.end]
    return max(ends) if ends else None


def _min_start(op: Operation) -> datetime | None:
    starts = [s.start for s in op.segments if s.start]
    return min(starts) if starts else None
