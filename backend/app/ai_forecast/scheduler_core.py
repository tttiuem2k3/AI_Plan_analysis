from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple


class ResourceType(str, Enum):
    MACHINE = "MACHINE"
    LABOR = "LABOR"


@dataclass(slots=True)
class Operation:
    op_id: str
    line_key: str
    don_hang_id: int
    tp_dinh_muc_id: int
    btp_dinh_muc_id: int
    ma_cong_doan: str
    ma_cong_doan_lon: str
    thu_tu_sx: int
    qty: float
    run_minutes: int
    setup_minutes: int
    due_dt: datetime
    ma_nguon_luc_hint: Optional[str] = None
    resource_type: ResourceType = ResourceType.MACHINE
    allowed_machines: List[str] = None  # type: ignore[assignment]
    predecessors: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # đảm bảo list không dùng chung giữa các instance
        if self.allowed_machines is None:
            self.allowed_machines = []
        if self.predecessors is None:
            self.predecessors = []


@dataclass(slots=True)
class ScheduleSegment:
    op_id: str
    don_hang_id: int
    line_key: str
    tp_dinh_muc_id: int
    btp_dinh_muc_id: int
    thu_tu_sx: int
    ma_cong_doan: str
    ma_cong_doan_lon: str
    ma_nguon_luc: Optional[str]
    so_luong_sx: float
    setup_minutes: int
    run_minutes: int
    start_dt: datetime
    end_dt: datetime
    due_dt: datetime
    note: str = ""


def schedule_greedy(
    operations: List[Operation] | None = None,
    ops: List[Operation] | None = None,
    *,
    start_dt: datetime | None = None,
    horizon_start: datetime | None = None,
    horizon_end: datetime | None = None,
    busy_by_machine: Optional[Dict[str, List[Tuple[datetime, datetime]]]] = None,
    busy_machine: Optional[Dict[str, List[Tuple[datetime, datetime]]]] = None,
    busy_by_dept: Optional[Dict[str, List[Tuple[datetime, datetime]]]] = None,
    busy_dept: Optional[Dict[str, List[Tuple[datetime, datetime]]]] = None,
) -> List[ScheduleSegment]:
    """Best-effort greedy scheduler.

    Quy tắc lịch làm việc:
    - KHÔNG sản xuất vào Chủ nhật.
    - Nếu thời điểm bắt đầu rơi vào Chủ nhật -> dời sang 00:00 ngày hôm sau (Thứ 2).
    - Nếu đoạn sản xuất (start->end) cắt qua Chủ nhật -> dời start sang 00:00 Thứ 2.

    Trả về danh sách segment (ScheduleSegment) để tương thích với plan_from_order_service.

    Tương thích tham số:
    - code mới/đang dùng: schedule_greedy(operations=..., start_dt=..., busy_by_machine=..., busy_by_dept=...)
    - code cũ: schedule_greedy(ops=..., horizon_start=..., horizon_end=..., busy_machine=..., busy_dept=...)
    """

    # normalize inputs
    ops = operations if operations is not None else (ops or [])

    if horizon_start is None:
        horizon_start = start_dt or datetime.now()
    if horizon_end is None:
        # default horizon: 30 ngày
        horizon_end = horizon_start + timedelta(days=30)

    # accept both names
    busy_machine = busy_by_machine if busy_by_machine is not None else busy_machine
    busy_dept = busy_by_dept if busy_by_dept is not None else busy_dept

    busy_machine = {**(busy_machine or {})}
    busy_dept = {**(busy_dept or {})}

    def _dur(op: Operation) -> timedelta:
        minutes = int(max(0, (op.setup_minutes or 0) + (op.run_minutes or 0)))
        return timedelta(minutes=minutes)

    def _is_free(arr: List[Tuple[datetime, datetime]], st: datetime, en: datetime) -> bool:
        for a, b in arr:
            # overlap if [st,en) intersects [a,b)
            if st < b and en > a:
                return False
        return True

    def _merge_intervals(iv: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
        if not iv:
            return []
        iv2 = sorted(iv, key=lambda x: x[0])
        out: List[Tuple[datetime, datetime]] = [iv2[0]]
        for s, e in iv2[1:]:
            ps, pe = out[-1]
            if s <= pe:
                out[-1] = (ps, max(pe, e))
            else:
                out.append((s, e))
        return out

    def _next_day_start(dt: datetime) -> datetime:
        """00:00 của ngày hôm sau."""
        d0 = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return d0 + timedelta(days=1)

    def _shift_if_sunday(st: datetime) -> datetime:
        # Python weekday: Mon=0..Sun=6
        if st.weekday() == 6:
            return _next_day_start(st)
        return st

    def _crosses_sunday(st: datetime, en: datetime) -> bool:
        # any instant within [st,en) that is Sunday
        if en <= st:
            return False
        cur = st
        # fast path: within same day and not Sunday
        if cur.date() == (en - timedelta(microseconds=1)).date():
            return cur.weekday() == 6

        # scan day boundaries (max duration in this greedy is expected small; safe fallback)
        day = st.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = (en - timedelta(microseconds=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        while day <= end_day:
            if day.weekday() == 6:
                # Sunday exists in covered range
                # Ensure the day interval intersects [st,en)
                ds = day
                de = day + timedelta(days=1)
                if st < de and en > ds:
                    return True
            day = day + timedelta(days=1)
        return False

    def _find_earliest(
        arr: List[Tuple[datetime, datetime]],
        st0: datetime,
        dur: timedelta,
        extra_blocks: List[Tuple[datetime, datetime]] | None = None,
    ) -> datetime:
        st = _shift_if_sunday(st0)
        blocks = list(arr)
        if extra_blocks:
            blocks.extend(extra_blocks)
        blocks = _merge_intervals(blocks)

        # Try incremental search by jumping to end of overlapping intervals
        for _ in range(8000):
            st = _shift_if_sunday(st)
            en = st + dur

            # Nếu interval cắt qua Chủ nhật -> dời sang ngày hôm sau (00:00 Thứ 2 nếu đang ở CN)
            if _crosses_sunday(st, en):
                # dời tới 00:00 của ngày sau nếu start là CN; nếu CN nằm giữa khoảng,
                # dời tới ngay sau CN (00:00 Thứ 2) bằng cách nhảy theo từng ngày cho tới khi không còn CN.
                # Cách đơn giản: tăng st lên đầu ngày kế tiếp cho tới khi không cross CN.
                st = _next_day_start(st)
                continue

            if _is_free(blocks, st, en):
                return st

            # Jump to the max end among overlaps (ensures we skip full blocked region)
            overlap_ends = [b for a, b in blocks if st < b and en > a]
            if overlap_ends:
                st = max(overlap_ends)
                continue
            st = st + timedelta(minutes=1)

        return st

    # Normalize allowed_machines
    for op in ops:
        if op.allowed_machines is None:
            op.allowed_machines = []

    ops_sorted = sorted(
        ops,
        key=lambda o: (
            o.due_dt,
            o.line_key,
            o.thu_tu_sx,
            o.op_id,
        ),
    )

    segs: List[ScheduleSegment] = []
    end_by_op_id: Dict[str, datetime] = {}

    def _ready_time(op: Operation) -> datetime:
        """Earliest allowed start time based on precedence constraints."""

        preds = list(getattr(op, "predecessors", None) or [])
        if not preds:
            return horizon_start
        ends = [end_by_op_id[p] for p in preds if p in end_by_op_id]
        if not ends:
            # If predecessors aren't scheduled yet, best-effort fallback.
            return horizon_start
        return max([horizon_start] + ends)

    for op in ops_sorted:
        dur = _dur(op)
        ready = _ready_time(op)
        if dur.total_seconds() <= 0:
            # segment 0 phút vẫn tạo để pipeline không gãy
            st0 = _shift_if_sunday(ready)
            segs.append(
                ScheduleSegment(
                    op_id=op.op_id,
                    don_hang_id=int(op.don_hang_id),
                    line_key=str(op.line_key),
                    tp_dinh_muc_id=int(op.tp_dinh_muc_id),
                    btp_dinh_muc_id=int(op.btp_dinh_muc_id),
                    thu_tu_sx=int(op.thu_tu_sx),
                    ma_cong_doan=str(op.ma_cong_doan),
                    ma_cong_doan_lon=str(op.ma_cong_doan_lon),
                    ma_nguon_luc=None,
                    so_luong_sx=float(op.qty or 0.0),
                    setup_minutes=int(op.setup_minutes or 0),
                    run_minutes=int(op.run_minutes or 0),
                    start_dt=st0,
                    end_dt=st0,
                    due_dt=op.due_dt,
                    note="zero_duration",
                )
            )
            end_by_op_id[str(op.op_id)] = st0
            continue

        if op.resource_type == ResourceType.MACHINE:
            candidates: List[str] = []
            if op.ma_nguon_luc_hint:
                candidates.append(str(op.ma_nguon_luc_hint))
            candidates.extend([str(x) for x in (op.allowed_machines or []) if x])
            if not candidates:
                candidates = ["M000"]

            best: Tuple[datetime, str] | None = None
            for m in candidates:
                arr = busy_machine.setdefault(m, [])
                st = _find_earliest(arr, ready, dur)
                if best is None or st < best[0]:
                    best = (st, m)

            st, m = best if best else (horizon_start, candidates[0])
            st = _shift_if_sunday(st)
            en = st + dur
            if _crosses_sunday(st, en):
                st = _find_earliest(busy_machine.setdefault(m, []), _next_day_start(st), dur)
                en = st + dur

            if en > horizon_end:
                en = horizon_end
                st = min(st, en)
            busy_machine.setdefault(m, []).append((st, en))

            end_by_op_id[str(op.op_id)] = en

            segs.append(
                ScheduleSegment(
                    op_id=op.op_id,
                    don_hang_id=int(op.don_hang_id),
                    line_key=str(op.line_key),
                    tp_dinh_muc_id=int(op.tp_dinh_muc_id),
                    btp_dinh_muc_id=int(op.btp_dinh_muc_id),
                    thu_tu_sx=int(op.thu_tu_sx),
                    ma_cong_doan=str(op.ma_cong_doan),
                    ma_cong_doan_lon=str(op.ma_cong_doan_lon),
                    ma_nguon_luc=str(m) if m else None,
                    so_luong_sx=float(op.qty or 0.0),
                    setup_minutes=int(op.setup_minutes or 0),
                    run_minutes=int(op.run_minutes or 0),
                    start_dt=st,
                    end_dt=en,
                    due_dt=op.due_dt,
                    note="no_sunday_shift_next",
                )
            )

        else:
            dept = str(op.ma_cong_doan_lon or "D000")
            arr = busy_dept.setdefault(dept, [])
            # If this labor op is tied to a specific resource id (e.g. NL001),
            # enforce resource non-overlap in addition to dept non-overlap.
            res_id = None
            try:
                rid = str(op.ma_nguon_luc_hint or "").strip()
                if rid and rid.upper() != "M000":
                    res_id = rid
            except Exception:
                res_id = None

            extra = busy_machine.setdefault(res_id, []) if res_id else None

            st = _find_earliest(arr, ready, dur, extra_blocks=extra)
            st = _shift_if_sunday(st)
            en = st + dur
            if _crosses_sunday(st, en):
                st = _find_earliest(arr, _next_day_start(st), dur, extra_blocks=extra)
                en = st + dur

            if en > horizon_end:
                en = horizon_end
                st = min(st, en)
            arr.append((st, en))

            if res_id:
                busy_machine.setdefault(res_id, []).append((st, en))

            end_by_op_id[str(op.op_id)] = en

            segs.append(
                ScheduleSegment(
                    op_id=op.op_id,
                    don_hang_id=int(op.don_hang_id),
                    line_key=str(op.line_key),
                    tp_dinh_muc_id=int(op.tp_dinh_muc_id),
                    btp_dinh_muc_id=int(op.btp_dinh_muc_id),
                    thu_tu_sx=int(op.thu_tu_sx),
                    ma_cong_doan=str(op.ma_cong_doan),
                    ma_cong_doan_lon=str(op.ma_cong_doan_lon),
                    ma_nguon_luc=str(res_id) if res_id else None,
                    so_luong_sx=float(op.qty or 0.0),
                    setup_minutes=int(op.setup_minutes or 0),
                    run_minutes=int(op.run_minutes or 0),
                    start_dt=st,
                    end_dt=en,
                    due_dt=op.due_dt,
                    note="labor_no_sunday_shift_next",
                )
            )

    return segs
