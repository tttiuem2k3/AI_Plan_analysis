from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from .enums import MoveType, ResourceMode, Severity


@dataclass(slots=True)
class RulesConfig:
    top_n: int = 3
    dept_exclusive: bool = True
    regular_hours: int = 8
    overtime_hours: int = 4
    strict_daily_qty: bool = False
    include_waiting_material_status: bool = False
    allow_sunday: bool = True
    non_working_dates: List[str] = field(default_factory=list)


@dataclass(slots=True)
class ResourceCandidate:
    resource_id: str
    setup_minutes: int = 0


@dataclass(slots=True)
class RawOperation:
    plan_no: str
    operation_id: str
    order_no: str
    line_key: str
    customer: str
    sequence: int
    phase_id: str
    phase_name: str
    phase_group: str
    finished_product_code: str
    finished_product_name: str
    semi_finished_product_code: str
    semi_finished_product_name: str
    resources: List[ResourceCandidate]
    resource_names: str
    resource_worker: int
    resource_machine: int
    time_limit_min_per_unit: float
    total_qty: int
    start_dt: datetime
    end_dt: datetime
    due_dt: datetime
    status: str
    production_capacity_input: float
    overtime_capacity_input: float
    daily_qty_total: int = 0
    daily_qty_input: List[Dict[str, Any]] = field(default_factory=list)
    late_qty_input: int = 0
    unplanned_qty_input: int = 0
    max_resource_worker: int = 0
    is_missing_planning_values: bool = False
    material_available_dt: Optional[datetime] = None
    setup_family: str = ""


@dataclass(slots=True)
class PlanData:
    plan_no: str
    status: str
    operations: List[RawOperation] = field(default_factory=list)
    start_manufacturing: Optional[datetime] = None
    end_manufacturing: Optional[datetime] = None


@dataclass(slots=True)
class ParsedInput:
    rules: RulesConfig
    main_plans: List[PlanData]
    related_plans: List[PlanData]


@dataclass(slots=True)
class ValidationIssue:
    code: str
    severity: Severity
    message: str
    operation_id: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "operation_id": self.operation_id,
            "context": self.context,
        }


@dataclass(slots=True)
class Operation:
    operation_id: str
    plan_no: str
    order_no: str
    line_key: str
    customer: str
    sequence: int
    phase_id: str
    phase_name: str
    phase_group: str
    finished_product_code: str
    finished_product_name: str
    semi_finished_product_code: str
    semi_finished_product_name: str
    resource_candidates: List[ResourceCandidate]
    resource_names: str
    resource_mode: ResourceMode
    resource_worker: int
    resource_machine: int
    time_limit_min_per_unit: float
    total_qty: int
    start_dt: datetime
    end_dt: datetime
    due_dt: datetime
    required_minutes: int = 0
    regular_minutes_per_day: int = 0
    overtime_minutes_per_day: int = 0
    production_capacity_calc: int = 0
    overtime_capacity_calc: int = 0
    max_possible_qty_before_due: int = 0
    predecessor_id: Optional[str] = None
    max_resource_worker: int = 0
    recommended_resource_worker: Optional[int] = None
    is_missing_planning_values: bool = False
    recomputed_end_dt: Optional[datetime] = None
    recomputed_daily_qty: List[Dict[str, Any]] = field(default_factory=list)
    recomputed_late_qty: int = 0
    recomputed_unplanned_qty: int = 0
    recomputed_production_capacity: int = 0
    recomputed_overtime_capacity: int = 0
    input_production_capacity: float = 0.0
    input_overtime_capacity: float = 0.0
    input_daily_qty: List[Dict[str, Any]] = field(default_factory=list)
    input_late_qty: int = 0
    input_unplanned_qty: int = 0
    material_available_dt: Optional[datetime] = None
    setup_family: str = ""


@dataclass(slots=True)
class ConflictItem:
    code: str
    severity: Severity
    operation_id: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "operation_id": self.operation_id,
            "message": self.message,
            "context": self.context,
        }


@dataclass(slots=True)
class ConflictReport:
    items: List[ConflictItem] = field(default_factory=list)


@dataclass(slots=True)
class ScheduledOperation:
    operation_id: str
    plan_no: str
    phase_group: str
    assigned_resource: str
    start_min: int
    end_min: int
    start_dt: datetime
    end_dt: datetime
    due_dt: datetime
    tardiness_minutes: int
    daily_qty: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SolverRawResult:
    status: str
    feasible: bool
    objective_value: float
    horizon_start: datetime
    scheduled_operations: List[ScheduledOperation] = field(default_factory=list)
    unscheduled_operation_ids: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class KpiSnapshot:
    feasible: bool
    total_operations: int
    late_ops_count: int
    total_late_qty: int
    total_unplanned_qty: int
    total_tardiness_minutes: int
    overtime_used_minutes: int
    resource_utilization: Dict[str, float]


@dataclass(slots=True)
class Hotspots:
    bottleneck_machines: List[str]
    bottleneck_phase_groups: List[str]
    bottleneck_ops: List[str]
    blocked_operations: List[str]


@dataclass(slots=True)
class ProposalMove:
    type: MoveType
    reason: str
    operation_id: str
    machine: Optional[str] = None
    from_machine: Optional[str] = None
    to_machine: Optional[str] = None
    op_ids_after: List[str] = field(default_factory=list)
    expected_impact: Dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ProposalOption:
    id: str
    rank: int
    score: float
    title: str
    reason_summary: str
    moves: List[ProposalMove]
    expected_impact: Dict[str, int]


@dataclass(slots=True)
class OptimizationResult:
    full_result: Dict[str, Any]
    llm_payload: Dict[str, Any]


def to_dict_dataclass(obj: Any) -> Dict[str, Any]:
    return asdict(obj)
