from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import List
import unicodedata

from .enums import Severity
from .models import ParsedInput, ValidationIssue


def _daily_qty_date(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").strip()[:10])
    except Exception:
        return None


def _text_key(value) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.lower().split())


def _computed_daily_capacity(op, *, regular_hours: int, overtime_hours: int) -> int:
    if op.time_limit_min_per_unit <= 0:
        return 0
    machine_resources = [r for r in (op.resources or []) if str(r.resource_id or "").strip().upper() != "M000"]
    if machine_resources:
        regular_total = sum(max(0, regular_hours * 60 - max(0, int(r.setup_minutes or 0))) for r in machine_resources)
        overtime_total = sum(max(0, overtime_hours * 60) for _ in machine_resources)
        return int(regular_total / op.time_limit_min_per_unit) + int(overtime_total / op.time_limit_min_per_unit)
    if op.resource_worker > 0:
        return int((op.resource_worker * regular_hours * 60) / op.time_limit_min_per_unit) + int(
            (op.resource_worker * overtime_hours * 60) / op.time_limit_min_per_unit
        )
    return 0


def validate_parsed(parsed: ParsedInput) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    non_working_dates = set(parsed.rules.non_working_dates or [])

    if not parsed.main_plans:
        issues.append(
            ValidationIssue(
                code="MISSING_MAIN_PLAN",
                severity=Severity.ERROR,
                message="Missing ProductionPlan for analysis.",
            )
        )
        return issues

    for plan in parsed.main_plans:
        seq_map = defaultdict(list)
        for op in plan.operations:
            seq_map[(op.order_no, op.line_key)].append(op.sequence)

            if op.time_limit_min_per_unit <= 0:
                issues.append(ValidationIssue("INVALID_TIME_LIMIT", Severity.ERROR, "TimeLimit must be > 0.", op.operation_id))
            if op.start_dt > op.end_dt:
                issues.append(ValidationIssue("INVALID_TIME_WINDOW", Severity.ERROR, "StartDate must be <= EndDate.", op.operation_id))
            if op.due_dt < op.start_dt:
                issues.append(ValidationIssue("DUE_BEFORE_START", Severity.WARNING, "DueDate is before StartDate; lateness risk is unavoidable.", op.operation_id))
            if op.material_available_dt is not None and op.start_dt < op.material_available_dt:
                issues.append(ValidationIssue("START_BEFORE_MATERIAL_READY", Severity.ERROR, "StartDate is before material availability date.", op.operation_id))
            if op.resource_machine > 0 and not op.resources:
                issues.append(ValidationIssue("MISSING_RESOURCE", Severity.ERROR, "Machine operation has empty ResourceIDs.", op.operation_id))
            if op.resource_machine <= 0 and any(str(r.resource_id or "").strip().upper() != "M000" for r in op.resources):
                issues.append(ValidationIssue("RESOURCE_TYPE_MISMATCH", Severity.WARNING, "Labor operation is assigned to machine resource code.", op.operation_id))
            if op.resource_machine <= 0 and not op.resources and op.resource_worker <= 0 and op.production_capacity_input <= 0:
                issues.append(ValidationIssue("MISSING_LABOR_CAPACITY", Severity.WARNING, "Labor operation has no worker count or input production capacity.", op.operation_id))

            if op.daily_qty_total > 0 and op.total_qty > 0 and op.daily_qty_total != op.total_qty:
                severity = Severity.ERROR if parsed.rules.strict_daily_qty else Severity.WARNING
                issues.append(
                    ValidationIssue(
                        "DAILY_QTY_MISMATCH",
                        severity,
                        "DailyQty total does not match TotalQuantity.",
                        op.operation_id,
                        context={"daily_qty_total": op.daily_qty_total, "total_qty": op.total_qty},
                    )
                )

            for idx, item in enumerate(op.daily_qty_input or []):
                day = _daily_qty_date((item or {}).get("ProductionDate") or (item or {}).get("Date") or (item or {}).get("Ngay"))
                qty = 0
                try:
                    qty = int(float((item or {}).get("Quantity") or (item or {}).get("Qty") or (item or {}).get("SoLuong") or 0))
                except Exception:
                    qty = 0
                daily_capacity_input = _computed_daily_capacity(
                    op,
                    regular_hours=parsed.rules.regular_hours,
                    overtime_hours=parsed.rules.overtime_hours,
                )
                if daily_capacity_input <= 0:
                    daily_capacity_input = max(0, int(op.production_capacity_input or 0)) + max(0, int(op.overtime_capacity_input or 0))
                if daily_capacity_input > 0 and qty > daily_capacity_input:
                    issues.append(
                        ValidationIssue(
                            "DAILY_QTY_EXCEEDS_DAILY_CAPACITY",
                            Severity.WARNING,
                            "DailyQty exceeds ProductionCapacity + OvertimeCapacity.",
                            op.operation_id,
                            context={"index": idx, "quantity": qty, "daily_capacity": daily_capacity_input},
                        )
                    )
                if day is None:
                    issues.append(
                        ValidationIssue(
                            "INVALID_DAILY_QTY_DATE",
                            Severity.ERROR,
                            "DailyQty.ProductionDate is not a valid date.",
                            op.operation_id,
                            context={"index": idx},
                        )
                    )
                    continue

                production_date = day.date()
                if production_date < op.start_dt.date() or production_date > op.end_dt.date():
                    issues.append(
                        ValidationIssue(
                            "DAILY_QTY_OUTSIDE_WINDOW",
                            Severity.ERROR,
                            "DailyQty is outside StartDate/EndDate.",
                            op.operation_id,
                            context={"index": idx, "production_date": production_date.isoformat()},
                        )
                    )
                if (not parsed.rules.allow_sunday and day.weekday() == 6) or production_date.isoformat() in non_working_dates:
                    issues.append(
                        ValidationIssue(
                            "DAILY_QTY_ON_NON_WORKING_DAY",
                            Severity.ERROR,
                            "DailyQty is on a non-working date.",
                            op.operation_id,
                            context={"index": idx, "production_date": production_date.isoformat()},
                        )
                    )

            if _text_key(op.status) == "cho vat tu" and not parsed.rules.include_waiting_material_status:
                issues.append(
                    ValidationIssue(
                        "WAITING_MATERIAL_EXCLUDED",
                        Severity.INFO,
                        "Waiting-material operation will be excluded from hard solve.",
                        op.operation_id,
                    )
                )

        for (order_no, _line_key), seqs in seq_map.items():
            if seqs != sorted(seqs):
                issues.append(
                    ValidationIssue(
                        "INVALID_PRODUCTION_SEQUENCE",
                        Severity.WARNING,
                        f"ProductionSequence is not ascending for order {order_no}.",
                    )
                )

    return issues
