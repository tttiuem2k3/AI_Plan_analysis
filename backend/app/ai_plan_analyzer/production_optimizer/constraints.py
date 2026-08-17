from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .models import ParsedInput


@dataclass(slots=True)
class BusyInterval:
    resource_id: str
    start_min: int
    end_min: int
    plan_no: str


@dataclass(slots=True)
class ConstraintBundle:
    resource_busy_intervals: Dict[str, List[BusyInterval]]
    phase_group_busy_intervals: Dict[str, List[BusyInterval]]


def build_related_constraints(parsed: ParsedInput, horizon_start) -> ConstraintBundle:
    resource_busy: Dict[str, List[BusyInterval]] = {}
    phase_busy: Dict[str, List[BusyInterval]] = {}

    for plan in parsed.related_plans:
        for op in plan.operations:
            start_min = int((op.start_dt - horizon_start).total_seconds() / 60)
            end_min = int((op.end_dt - horizon_start).total_seconds() / 60)
            if end_min <= start_min:
                continue

            if op.phase_group:
                phase_busy.setdefault(op.phase_group, []).append(
                    BusyInterval(resource_id=op.phase_group, start_min=start_min, end_min=end_min, plan_no=plan.plan_no)
                )

            for r in op.resources:
                resource_busy.setdefault(r.resource_id, []).append(
                    BusyInterval(resource_id=r.resource_id, start_min=start_min, end_min=end_min, plan_no=plan.plan_no)
                )

    return ConstraintBundle(resource_busy_intervals=resource_busy, phase_group_busy_intervals=phase_busy)
