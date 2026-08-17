from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .dtos import Operation, Plan, Segment


def _daterange(d0: date, d1: date) -> Iterable[date]:
    cur = d0
    while cur <= d1:
        yield cur
        cur = cur + timedelta(days=1)


def _bucket_qty_by_day_from_segment(seg: Segment) -> Dict[str, float]:
    """Convert a time segment into day buckets.

    Policy:
    - If seg.day is present, use it directly.
    - Else if start/end are present, spread qty evenly per day across all intersected days.
      (This is a reasonable approximation for UI; scheduling accuracy is still kept in segments.)
    - If not enough info, allocate nothing.
    """

    if seg.day:
        return {str(seg.day): float(seg.qty or 0.0)}

    if not seg.start or not seg.end:
        return {}

    st = seg.start
    en = seg.end
    if en <= st:
        return {}

    # Determine covered dates
    st_d = st.date()
    # treat end as exclusive; subtract 1 microsecond to include correct last date
    en_d = (en - timedelta(microseconds=1)).date()

    days = list(_daterange(st_d, en_d))
    if not days:
        return {}

    qty = float(seg.qty or 0.0)
    per = qty / float(len(days)) if len(days) else 0.0
    out: Dict[str, float] = {}
    for d in days:
        out[d.isoformat()] = out.get(d.isoformat(), 0.0) + per
    return out


def calendarize_plan(plan: Plan) -> List[Dict[str, Any]]:
    """Build UI table rows: day/product/step/qty/resource.

    Returns rows sorted by day then dept/operation.

    Row schema:
    { day: 'YYYY-MM-DD', product: str, step: str, qty: number, resource: str }
    """

    # Aggregate by (day, op)
    qty_by_day_op: Dict[Tuple[str, str], float] = defaultdict(float)

    for op in plan.operations:
        segs = op.segments or []
        if not segs:
            # fallback treat as unscheduled; skip
            continue

        for seg in segs:
            if getattr(seg, "is_setup", False):
                continue
            buckets = _bucket_qty_by_day_from_segment(seg)
            for day, q in buckets.items():
                if q and q > 0:
                    qty_by_day_op[(day, op.op_id)] += float(q)

    rows: List[Dict[str, Any]] = []

    # Map op_id -> op for lookup
    op_by_id = {op.op_id: op for op in plan.operations}

    for (day, op_id), qty in qty_by_day_op.items():
        op = op_by_id.get(op_id)
        if not op:
            continue

        product = op.ten_ban_thanh_pham or op.ten_thanh_pham
        if not product:
            # fallback ids
            if op.btp_id is not None:
                product = f"BTP {op.btp_id}"
            elif op.tp_id is not None:
                product = f"TP {op.tp_id}"
            else:
                product = op_id

        step = op.ten_cong_doan or op.ma_cong_doan or ""

        # Resource text: prefer machine if present; else dept + labor count
        if op.machine:
            resource = str(op.machine)
        else:
            dept = op.ten_bo_phan or op.ma_cong_doan_lon or ""
            if op.nhan_su_phan_bo and op.nhan_su_phan_bo > 0:
                resource = f"{dept} • NS {op.nhan_su_phan_bo:g}" if dept else f"NS {op.nhan_su_phan_bo:g}"
            else:
                resource = dept

        rows.append(
            {
                "day": str(day),
                "product": str(product),
                "step": str(step),
                "qty": float(qty),
                "resource": str(resource or ""),
                "op_id": op_id,
                "dept": op.ten_bo_phan or op.ma_cong_doan_lon,
                "machine": op.machine,
                "thu_tu_sx": op.thu_tu_sx,
            }
        )

    # sort stable for UI
    def _sort_key(r: Dict[str, Any]):
        return (
            r.get("day") or "",
            r.get("dept") or "",
            r.get("machine") or "",
            int(r.get("thu_tu_sx") or 0),
            r.get("product") or "",
            r.get("op_id") or "",
        )

    rows.sort(key=_sort_key)

    # hide internal columns not needed by FE table (keep minimal)
    out = []
    for r in rows:
        out.append({"day": r["day"], "product": r["product"], "step": r["step"], "qty": r["qty"], "resource": r["resource"]})
    return out
