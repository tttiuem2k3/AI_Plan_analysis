from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import logging

from .models import ParsedInput, PlanData, RawOperation, ResourceCandidate, RulesConfig

logger = logging.getLogger(__name__)


def _pick(d: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _to_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    s = str(v or "").strip().replace("Z", "+00:00")
    if not s:
        raise ValueError("datetime value is empty")
    for fmt in (None, "%d/%m/%Y", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            if fmt is None:
                return datetime.fromisoformat(s)
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    # relaxed fallback for common partial-strings
    try:
        return datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except Exception:
        pass
    raise ValueError(f"invalid datetime: {v}")


def _to_dt_or_none(v: Any) -> Optional[datetime]:
    if v in (None, ""):
        return None
    try:
        return _to_dt(v)
    except Exception:
        return None


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _to_bool(v: Any, default: bool = False) -> bool:
    if v in (None, ""):
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "co", "có"):
        return True
    if s in ("0", "false", "no", "n", "off", "khong", "không"):
        return False
    return default


def _to_date_str(v: Any) -> str:
    dt = _to_dt_or_none(v)
    if dt:
        return dt.date().isoformat()
    s = str(v or "").strip()
    return s[:10] if s else ""


def _parse_resources(row: Dict[str, Any]) -> List[ResourceCandidate]:
    resources_raw = _pick(row, ["ResourceIDs", "MaNguonLuc"], default=[])
    out: List[ResourceCandidate] = []
    if isinstance(resources_raw, list):
        for item in resources_raw:
            if isinstance(item, dict):
                rid = _pick(item, ["ResourceID", "resource_id", "MaNguonLuc"])
                if rid:
                    out.append(ResourceCandidate(resource_id=str(rid), setup_minutes=_to_int(_pick(item, ["TimeResource", "setup_minutes"], 0))))
            elif item not in (None, ""):
                out.append(ResourceCandidate(resource_id=str(item), setup_minutes=0))
    elif resources_raw not in (None, ""):
        text = str(resources_raw)
        for rid in [x.strip() for x in text.split(",") if x.strip()]:
            out.append(ResourceCandidate(resource_id=rid, setup_minutes=0))
    return out


def _parse_plan(plan_obj: Dict[str, Any], fallback_index: int) -> PlanData:
    master = plan_obj.get("Master") if isinstance(plan_obj.get("Master"), dict) else {}
    detail = plan_obj.get("Detail") if isinstance(plan_obj.get("Detail"), list) else []

    plan_no = str(_pick(master, ["VoucherNo", "MaKeHoach", "APK"], default=f"PLAN_{fallback_index}"))
    status = str(_pick(master, ["Status", "TrangThai"], default=""))

    operations: List[RawOperation] = []
    start_manufacturing = _to_dt_or_none(_pick(master, ["StartManufactering", "StartManufacturing", "TuNgay"]))
    end_manufacturing = _to_dt_or_none(_pick(master, ["EndDateManufactering", "EndDateManufacturing", "DenNgay"]))

    default_start = start_manufacturing or datetime.now()
    default_end = end_manufacturing
    if default_end is None:
        default_end = default_start

    for idx, row in enumerate(detail):
        if not isinstance(row, dict):
            continue
        resources = _parse_resources(row)
        sequence = _to_int(_pick(row, ["ProductionSequence", "ThuTuSX"], idx + 1), idx + 1)

        order_no = str(_pick(row, ["OrderNo", "SoDonHang", "DonHangID"], ""))
        btp_code = str(_pick(row, ["SemiFinishedProductCode", "MaBTP", "IdBTP"], ""))
        finished_product_code = str(_pick(row, ["FinishedProductCode", "MaTP"], ""))
        finished_product_name = str(_pick(row, ["FinishedProductName", "FinishedProductNameEn", "TenThanhPham"], ""))
        product_key = finished_product_code or finished_product_name
        line_key = str(_pick(row, ["LineKey"], "") or f"{order_no}::{product_key}")
        line_suffix = btp_code or f"ROW_{idx + 1}"
        op_id = f"{plan_no}::{order_no}::{sequence}::{line_suffix}"

        dq_raw = row.get("DailyQty") if isinstance(row.get("DailyQty"), list) else row.get("DailyQuantity")
        daily_qty_total = 0
        daily_qty_input: List[Dict[str, Any]] = []
        if isinstance(dq_raw, list):
            daily_qty_input = [x for x in dq_raw if isinstance(x, dict)]
            daily_qty_total = sum(_to_int(_pick(x, ["Quantity", "Qty", "SoLuong"], 0), 0) for x in dq_raw if isinstance(x, dict))

        start_dt = _to_dt_or_none(_pick(row, ["StartDate", "Start", "StartDT"]))
        end_dt = _to_dt_or_none(_pick(row, ["EndDate", "End", "EndDT"]))
        due_dt = _to_dt_or_none(_pick(row, ["DueDate", "DueDT", "Due"]))

        if start_dt is None:
            start_dt = default_start
        if end_dt is None:
            end_dt = due_dt or default_end or start_dt
        if due_dt is None:
            due_dt = end_dt

        if end_dt < start_dt:
            end_dt = start_dt

        material_available_dt = _to_dt_or_none(
            _pick(
                row,
                [
                    "MaterialAvailableDate",
                    "MaterialReadyDate",
                    "NgayCoVatTu",
                    "NgaySanSangVatTu",
                    "AvailableDate",
                ],
            )
        )
        if material_available_dt is not None and material_available_dt > start_dt:
            start_dt = material_available_dt
            if end_dt < start_dt:
                end_dt = start_dt

        max_resource_worker = _to_int(_pick(row, ["MaxResourceWorker", "SoNhanSuBoPhan", "ResourceWorkerMax", "max_resource_worker"], 0), 0)
        if max_resource_worker <= 0:
            max_resource_worker = _to_int(_pick(row, ["ResourceWorker"], 0), 0)

        production_capacity_input = _to_float(_pick(row, ["ProductionCapacity", "NangLucSanXuat"], 0.0), 0.0)
        overtime_capacity_input = _to_float(_pick(row, ["OvertimeCapacity", "NangLucTangCa"], 0.0), 0.0)

        is_missing_planning_values = bool(
            (end_manufacturing is None)
            and (
                _to_dt_or_none(_pick(row, ["EndDate", "End", "EndDT"])) is None
                or production_capacity_input <= 0
                or overtime_capacity_input <= 0
                or daily_qty_total <= 0
            )
        )

        operations.append(
            RawOperation(
                plan_no=plan_no,
                operation_id=op_id,
                order_no=order_no,
                line_key=line_key,
                customer=str(_pick(row, ["CustomerName", "KhachHang"], "")),
                sequence=sequence,
                phase_id=str(_pick(row, ["PhaseID", "MaCongDoan"], "")),
                phase_name=str(_pick(row, ["PhaseName", "TenCongDoan"], "")),
                phase_group=str(_pick(row, ["PhaseGroupName", "MaCongDoanLon", "TenBoPhan"], "")),
                finished_product_code=finished_product_code,
                finished_product_name=finished_product_name,
                semi_finished_product_code=btp_code,
                semi_finished_product_name=str(_pick(row, ["SemiFinishedProductName", "TenBanThanhPham"], "")),
                resources=resources,
                resource_names=str(_pick(row, ["ResourceNames", "TenNguonLuc"], "")),
                resource_worker=_to_int(_pick(row, ["ResourceWorker"], 0), 0),
                resource_machine=_to_int(_pick(row, ["ResourceManchine", "ResourceMachine"], len(resources)), len(resources)),
                time_limit_min_per_unit=_to_float(_pick(row, ["TimeLimit", "DinhMucPhutMoiSP"], 0.0), 0.0),
                total_qty=_to_int(_pick(row, ["TotalQuantity", "SoLuong", "Qty"], 0), 0),
                start_dt=start_dt,
                end_dt=end_dt,
                due_dt=due_dt,
                status=status,
                production_capacity_input=production_capacity_input,
                overtime_capacity_input=overtime_capacity_input,
                daily_qty_total=daily_qty_total,
                daily_qty_input=daily_qty_input,
                late_qty_input=_to_int(_pick(row, ["LateQty"], 0), 0),
                unplanned_qty_input=_to_int(_pick(row, ["UnplannedQty"], 0), 0),
                max_resource_worker=max_resource_worker,
                is_missing_planning_values=is_missing_planning_values,
                material_available_dt=material_available_dt,
                setup_family=str(
                    _pick(
                        row,
                        ["SetupFamily", "setup_family", "NhomSetup", "MaNhomSetup", "SemiFinishedProductCode", "MaBTP"],
                        "",
                    )
                ),
            )
        )

    return PlanData(
        plan_no=plan_no,
        status=status,
        operations=operations,
        start_manufacturing=start_manufacturing,
        end_manufacturing=end_manufacturing,
    )


def parse_input(data: Dict[str, Any]) -> ParsedInput:
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")

    rules_raw = data.get("Rules") if isinstance(data.get("Rules"), dict) else data.get("rules") or {}
    rules = RulesConfig(
        top_n=_to_int(_pick(rules_raw, ["Top_n", "top_n", "topN"], 3), 3),
        dept_exclusive=_to_bool(_pick(rules_raw, ["DeptExclusive", "deptExclusive"], True), True),
        regular_hours=min(8, max(1, _to_int(_pick(rules_raw, ["RegularHours", "regular_hours", "WorkHoursPerDay"], 8), 8))),
        overtime_hours=min(4, max(0, _to_int(_pick(rules_raw, ["OvertimeHours", "overtime_hours", "OTHoursPerDay"], 4), 4))),
        strict_daily_qty=_to_bool(_pick(rules_raw, ["StrictDailyQty", "strict_daily_qty"], False), False),
        include_waiting_material_status=_to_bool(_pick(rules_raw, ["IncludeWaitingMaterial", "include_waiting_material_status"], False), False),
        allow_sunday=_to_bool(_pick(rules_raw, ["AllowSunday", "allow_sunday", "WorkOnSunday", "work_on_sunday"], True), True),
        non_working_dates=[
            x
            for x in (
                _to_date_str(v)
                for v in (
                    _pick(rules_raw, ["NonWorkingDates", "non_working_dates", "Holidays", "holidays"], [])
                    if isinstance(_pick(rules_raw, ["NonWorkingDates", "non_working_dates", "Holidays", "holidays"], []), list)
                    else []
                )
            )
            if x
        ],
    )

    production_plan = data.get("ProductionPlan")
    if isinstance(production_plan, dict):
        main_list = [production_plan]
    elif isinstance(production_plan, list):
        main_list = [p for p in production_plan if isinstance(p, dict)]
    else:
        main_list = []

    related_raw = data.get("RelatedProductionPlans")
    if related_raw is None:
        related_raw = data.get("relatedProductionPlans")
    if related_raw is None:
        related_raw = data.get("related_plans")
    related_list = related_raw if isinstance(related_raw, list) else []

    main_plans = [_parse_plan(p, i + 1) for i, p in enumerate(main_list)]
    related_plans = [_parse_plan(p, i + 1 + len(main_plans)) for i, p in enumerate(related_list) if isinstance(p, dict)]

    logger.info("Parsed input: %s main plans, %s related plans", len(main_plans), len(related_plans))
    return ParsedInput(rules=rules, main_plans=main_plans, related_plans=related_plans)
