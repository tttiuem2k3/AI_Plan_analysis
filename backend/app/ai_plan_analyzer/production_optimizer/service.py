from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional
import logging

from .analyzer import analyze_solution, detect_conflicts
from .calculators import compute_capacities, normalize_operations
from .constraints import build_related_constraints
from .exceptions import InputValidationError
from .llm_payload import build_compact_llm_payload
from .models import KpiSnapshot, OptimizationResult, Operation, ParsedInput, SolverRawResult
from .parser import parse_input
from .proposal_builder import build_proposals
from .solver import solve_operations
from .unplanned_branch import (
    detect_unplanned_branch,
    recompute_missing_detail_fields,
)
from .validators import validate_parsed

logger = logging.getLogger(__name__)


class ProductionPlanningOptimizerService:
    """Backend-first production optimizer service.

    This service performs all heavy math/scheduling work before any LLM stage.
    It outputs:
    - full_result: rich machine result for backend/frontend/debug
    - llm_payload: compact, stable, token-efficient payload for LLM explanation only
    """

    def __init__(self, *, top_problem_rows: int = 8, top_conflicts: int = 5, top_options: int = 3) -> None:
        self.top_problem_rows = top_problem_rows
        self.top_conflicts = top_conflicts
        self.top_options = top_options

        self.parsed_input: Optional[ParsedInput] = None
        self.operations: List[Operation] = []
        self.validation_issues = []
        self.conflict_report = None
        self.solver_result: Optional[SolverRawResult] = None
        self.optimization_result: Optional[OptimizationResult] = None
        self.unplanned_branch_result: Dict[str, Any] = {}
        self.is_unplanned_branch: bool = False

    def load_input(self, data: dict) -> ParsedInput:
        self.parsed_input = parse_input(data)
        self.is_unplanned_branch = detect_unplanned_branch(self.parsed_input)
        return self.parsed_input

    def validate(self) -> list:
        if self.parsed_input is None:
            raise InputValidationError("Input must be loaded before validate")
        self.validation_issues = validate_parsed(self.parsed_input)

        hard_errors = [x for x in self.validation_issues if x.severity.value == "ERROR"]
        if hard_errors:
            details = "; ".join([f"{e.code}:{e.message}" for e in hard_errors[:8]])
            raise InputValidationError(f"Input validation failed with hard errors: {details}")
        return self.validation_issues

    def normalize_operations(self) -> list[Operation]:
        if self.parsed_input is None:
            raise InputValidationError("Input must be loaded before normalize")
        self.operations = normalize_operations(self.parsed_input)
        return self.operations

    def compute_capacities(self) -> None:
        if self.parsed_input is None:
            raise InputValidationError("Input must be loaded before compute_capacities")
        if not self.operations:
            raise InputValidationError("Operations must be normalized before compute_capacities")
        compute_capacities(self.parsed_input, self.operations)

    def detect_conflicts(self):
        if self.parsed_input is None:
            raise InputValidationError("Input must be loaded before detect_conflicts")
        if not self.operations:
            raise InputValidationError("Operations must be normalized before detect_conflicts")
        horizon_start = min(op.start_dt for op in self.operations)
        related_constraints = build_related_constraints(self.parsed_input, horizon_start)
        self.conflict_report = detect_conflicts(self.operations, related_constraints)
        return self.conflict_report

    def solve(self) -> SolverRawResult:
        if self.parsed_input is None:
            raise InputValidationError("Input must be loaded before solve")
        if not self.operations:
            raise InputValidationError("Operations must be normalized before solve")

        horizon_start = min(op.start_dt for op in self.operations)
        related_constraints = build_related_constraints(self.parsed_input, horizon_start)
        self.solver_result = solve_operations(
            self.operations,
            related_constraints,
            time_limit_s=8.0,
            enforce_dept_exclusive=bool(self.parsed_input.rules.dept_exclusive),
            allow_sunday=bool(self.parsed_input.rules.allow_sunday),
            non_working_dates=list(self.parsed_input.rules.non_working_dates or []),
        )
        return self.solver_result

    def analyze_result(self) -> OptimizationResult:
        if self.parsed_input is None or self.solver_result is None:
            raise InputValidationError("solve() must be called before analyze_result")
        if self.conflict_report is None:
            self.detect_conflicts()

        if self.is_unplanned_branch:
            self.unplanned_branch_result = recompute_missing_detail_fields(
                parsed=self.parsed_input,
                operations=self.operations,
                scheduled_operations=self.solver_result.scheduled_operations,
            )
        else:
            self.unplanned_branch_result = {}

        kpi, hotspots, problem_rows, aux = analyze_solution(self.operations, self.solver_result)
        options = build_proposals(self.parsed_input.rules.top_n, hotspots, problem_rows, asdict(kpi))

        full_result: Dict[str, Any] = {
            "validation_issues": [x.to_dict() for x in self.validation_issues],
            "baseline_validation_errors": [x.to_dict() for x in self.conflict_report.items],
            "solver": {
                "status": self.solver_result.status,
                "feasible": self.solver_result.feasible,
                "objective_value": self.solver_result.objective_value,
                "meta": self.solver_result.meta,
                "scheduled_operations": [asdict(x) for x in self.solver_result.scheduled_operations],
                "unscheduled_operation_ids": self.solver_result.unscheduled_operation_ids,
            },
            "kpi": asdict(kpi),
            "hotspots": asdict(hotspots),
            "business": {
                "problem_rows": problem_rows,
            },
            "options": [asdict(x) for x in options],
            "aux": aux,
            "unplanned_branch": {
                "detected": self.is_unplanned_branch,
                **(self.unplanned_branch_result or {}),
            },
        }

        llm_payload = self.build_llm_payload(
            kpi_snapshot=asdict(kpi),
            hotspots=hotspots,
            problem_rows=problem_rows,
            options=options,
        )

        self.optimization_result = OptimizationResult(full_result=full_result, llm_payload=llm_payload)
        return self.optimization_result

    def build_llm_payload(self, *, kpi_snapshot: Dict[str, Any], hotspots, problem_rows: List[Dict[str, Any]], options) -> Dict[str, Any]:
        if self.conflict_report is None:
            raise InputValidationError("detect_conflicts() must be called before build_llm_payload")
        kpi_obj = KpiSnapshot(
            feasible=bool(kpi_snapshot.get("feasible")),
            total_operations=int(kpi_snapshot.get("total_operations", 0)),
            late_ops_count=int(kpi_snapshot.get("late_ops_count", 0)),
            total_late_qty=int(kpi_snapshot.get("total_late_qty", 0)),
            total_unplanned_qty=int(kpi_snapshot.get("total_unplanned_qty", 0)),
            total_tardiness_minutes=int(kpi_snapshot.get("total_tardiness_minutes", 0)),
            overtime_used_minutes=int(kpi_snapshot.get("overtime_used_minutes", 0)),
            resource_utilization=dict(kpi_snapshot.get("resource_utilization", {})),
        )
        return build_compact_llm_payload(
            self.conflict_report,
            kpi=kpi_obj,
            hotspots=hotspots,
            problem_rows=problem_rows,
            options=options,
            top_conflicts=self.top_conflicts,
            top_rows=self.top_problem_rows,
            top_options=self.top_options,
        )

    def run(self, data: Dict[str, Any]) -> OptimizationResult:
        self.load_input(data)
        self.validate()
        self.normalize_operations()
        self.compute_capacities()
        self.detect_conflicts()
        self.solve()
        return self.analyze_result()
