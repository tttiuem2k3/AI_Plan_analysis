from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional


MoveType = Literal[
    "SWAP_ORDER_ON_MACHINE",
    "BATCH_SAME_SETUP_FAMILY",
    "MOVE_TO_ALTERNATE_MACHINE",
    "SHIFT_TO_FILL_IDLE_WINDOW",
    "PULL_FORWARD_CRITICAL_OP",
]


@dataclass(slots=True)
class Segment:
    """A scheduled segment for an operation.

    This module is designed to work with day-bucketed plans (per-day allocation) or
    time-segment plans (start/end). If you only have per-day quantities, set start/end
    to None and only fill qty.
    """

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    day: Optional[str] = None  # ISO date YYYY-MM-DD for day bucket
    qty: float = 0.0
    is_setup: bool = False


@dataclass(slots=True)
class Operation:
    """Atomic work unit for scheduling/analysis."""

    op_id: str
    plan_id: int

    # Business identifiers
    don_hang_id: Optional[int] = None
    so_don_hang: Optional[str] = None

    tp_id: Optional[int] = None
    ten_thanh_pham: Optional[str] = None
    btp_id: Optional[int] = None
    ten_ban_thanh_pham: Optional[str] = None

    # Routing / precedence
    thu_tu_sx: Optional[int] = None
    ma_cong_doan: Optional[str] = None
    ten_cong_doan: Optional[str] = None
    ma_cong_doan_lon: Optional[str] = None
    ten_bo_phan: Optional[str] = None

    # Resource assignment
    machine: Optional[str] = None  # machine key/name (if machine-based)
    loai_nguon_luc: Optional[str] = None  # e.g. "MAY" | "NHANSU" | "MAY+NHANSU"
    nhan_su_phan_bo: float = 0.0

    # Timing and quantities
    total_qty: float = 0.0
    dinh_muc_phut_moi_sp: float = 0.0
    setup_minutes: float = 0.0

    # Scheduling
    segments: List[Segment] = field(default_factory=list)

    # Extra bag for forward compatibility
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Plan:
    plan_id: int
    horizon_start: datetime
    horizon_end: datetime
    operations: List[Operation]

    # capacities are free-form structures; validator will interpret them via adapters
    constraints: Dict[str, Any] = field(default_factory=dict)
    capacity: Dict[str, Any] = field(default_factory=dict)
    due_dates: Dict[str, datetime] = field(default_factory=dict)  # op_id -> due


@dataclass(slots=True)
class KPIResult:
    late_ops: int
    total_tardiness_minutes: float
    total_setup_minutes: float

    # Optional breakdowns
    utilization_by_dept: Dict[str, float] = field(default_factory=dict)
    utilization_by_machine: Dict[str, float] = field(default_factory=dict)

    bottleneck_depts: List[str] = field(default_factory=list)
    bottleneck_machines: List[str] = field(default_factory=list)

    idle_windows: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ValidationError:
    code: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Move:
    type: MoveType
    reason: str = ""
    expected_impact: Dict[str, float] = field(default_factory=dict)

    # Generic fields usable by multiple move types
    machine: Optional[str] = None
    op_ids_before: Optional[List[str]] = None
    op_ids_after: Optional[List[str]] = None

    # Alternative machine transfer
    op_id: Optional[str] = None
    from_machine: Optional[str] = None
    to_machine: Optional[str] = None

    # Shifts
    from_day: Optional[str] = None
    to_day: Optional[str] = None


@dataclass(slots=True)
class PlanOption:
    proposal_id: str
    goal: str
    moves: List[Move]

    kpi_before: KPIResult
    kpi_after: Optional[KPIResult] = None
    delta: Dict[str, float] = field(default_factory=dict)

    validation_errors: List[ValidationError] = field(default_factory=list)
    notes: str = ""
