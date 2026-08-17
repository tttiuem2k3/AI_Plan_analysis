from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ...core.database import get_db, get_optional_db
from ...services.ai_plan_analysis_service import AIPlanAnalysisService
from ...ai_forecast.hr_forecast_service import HrForecastService
from ...ai_forecast.machine_forecast_service import MachineForecastService
from ...ai_forecast.available_resources_forecast_service import AvailableResourcesForecastService
from ...ai_forecast.warehouse_forecast_service import WarehouseForecastService
from ...ai_forecast.m01_production_forecast_service import M01ProductionForecastService
from ...utils.ai_flow_log import append_ai_flow_log


router = APIRouter(prefix="/kehoach", tags=["AI - Phân tích kế hoạch"])
logger = logging.getLogger("uvicorn.error")


def _to_float_or_none(v: object) -> float | None:
    if v is None:
        return None
    try:
        n = float(v)
    except Exception:
        return None
    if n != n:
        return None
    return n


def _round_pct_list(values: object, decimals: int = 2) -> list[float | None]:
    arr = values if isinstance(values, list) else []
    out: list[float | None] = []
    for v in arr:
        n = _to_float_or_none(v)
        out.append(round(n, decimals) if n is not None else None)
    return out


def _round_int_list(values: object) -> list[int | None]:
    arr = values if isinstance(values, list) else []
    out: list[int | None] = []
    for v in arr:
        n = _to_float_or_none(v)
        out.append(int(round(n)) if n is not None else None)
    return out


def _pick_first(src: dict, keys: list[str]) -> object:
    if not isinstance(src, dict):
        return None
    for k in keys:
        v = src.get(k)
        if v not in (None, ""):
            return v
    return None


def _compact_forecast_calendar_for_log(calendar: dict) -> dict:
    if not isinstance(calendar, dict):
        return {"Header": {}, "Days": [], "Rows": []}

    header = calendar.get("Header") if isinstance(calendar.get("Header"), dict) else {}
    if not header:
        header = calendar.get("header") if isinstance(calendar.get("header"), dict) else {}

    days = calendar.get("Days") if isinstance(calendar.get("Days"), list) else []
    if not days:
        days = calendar.get("days") if isinstance(calendar.get("days"), list) else []

    rows = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
    if not rows:
        rows = calendar.get("rows") if isinstance(calendar.get("rows"), list) else []

    compact_rows: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue

        compact_rows.append(
            {
                "TenBoPhan": _pick_first(r, ["TenBoPhan", "PhaseGroupName", "TenCongDoanLon", "MaCongDoanLon"]),
                "TenNguonLuc": _pick_first(r, ["TenNguonLuc", "ResourceNames", "NguonLucText", "machine_name"]),
                "MaNguonLuc": _pick_first(r, ["MaNguonLuc", "ResourceIDs"]),
                "SoNhanSuBoPhan": _pick_first(r, ["SoNhanSuBoPhan", "standard_staff_qty", "SoNhanSu", "staff"]),
                "planned_qty": _pick_first(r, ["planned_qty", "PlannedQty", "plannedQty", "PlannedTotal", "TotalQty", "SoLuongKeHoach", "KeHoachQty"]),
                "transaction_type": _pick_first(r, ["transaction_type", "TransactionType", "LoaiGiaoDich", "LoaiNhapXuat"]),
                "warehouse_id": _pick_first(r, ["warehouse_id", "WarehouseID", "MaKho", "Kho", "KhoID", "warehouse"]),
                "item_id": _pick_first(r, ["item_id", "ItemID", "MaMatHang", "MaHang", "MaBTP", "MaTP", "BTP_DinhMucID", "TP_DinhMucID"]),
                "item_name": _pick_first(r, ["TenBanThanhPham", "SemiFinishedProductName", "item_name", "ItemName", "TenThanhPham", "FinishedProductName"]),
                "process": _pick_first(r, ["TenCongDoan", "PhaseName", "process", "Process", "MaCongDoan"]),
            }
        )

    return {
        "Header": {
            "TuNgay": header.get("TuNgay"),
            "DenNgay": header.get("DenNgay"),
        },
        "Days": days,
        "Rows": compact_rows,
    }


def _compact_forecast_input_for_log(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {"payload": None}

    sys_data = payload.get("calendar") or payload.get("systemData") or payload.get("system_data")
    cal = sys_data if isinstance(sys_data, dict) else {}

    hr_in = payload.get("hr_forecast") if isinstance(payload.get("hr_forecast"), dict) else {}
    stages_raw = hr_in.get("stages") if isinstance(hr_in.get("stages"), list) else []

    stages_compact: list[dict] = []
    for s in stages_raw:
        if not isinstance(s, dict):
            continue
        stages_compact.append(
            {
                "major_process": _pick_first(s, ["major_process", "major_process_name", "stage", "TenCongDoanLon"]),
                "standard_staff_qty": _pick_first(s, ["standard_staff_qty", "SoNhanSu", "staff"]),
            }
        )

    compact_payload = {
        "planId": payload.get("planId") or payload.get("KeHoachID"),
        "calendar": _compact_forecast_calendar_for_log(cal),
    }

    if stages_compact:
        compact_payload["hr_forecast"] = {"stages": stages_compact}

    return compact_payload


def _compact_forecast_output_for_log(output_payload: dict | None) -> dict:
    if not isinstance(output_payload, dict):
        return {"payload": None}

    hr = output_payload.get("hr_forecast") if isinstance(output_payload.get("hr_forecast"), dict) else {}
    mc = output_payload.get("machine_forecast") if isinstance(output_payload.get("machine_forecast"), dict) else {}
    ar = output_payload.get("available_resources_forecast") if isinstance(output_payload.get("available_resources_forecast"), dict) else {}
    wh = output_payload.get("warehouse_forecast") if isinstance(output_payload.get("warehouse_forecast"), dict) else {}
    m01 = output_payload.get("m01_forecast") if isinstance(output_payload.get("m01_forecast"), dict) else {}
    days = output_payload.get("days") if isinstance(output_payload.get("days"), list) else []

    return {
        "days": days,
        "hr_forecast": {
            "avg_actual_staff_pct": hr.get("avg_actual_staff_pct") if isinstance(hr.get("avg_actual_staff_pct"), list) else [],
        },
        "machine_forecast": {
            "avg_machine_productivity_pct": mc.get("avg_machine_productivity_pct") if isinstance(mc.get("avg_machine_productivity_pct"), list) else [],
            "avg_machine_usage_pct": mc.get("avg_machine_usage_pct") if isinstance(mc.get("avg_machine_usage_pct"), list) else [],
        },
        "available_resources_forecast": {
            "available_resources_pct": ar.get("available_resources_pct") if isinstance(ar.get("available_resources_pct"), list) else [],
        },
        "warehouse_forecast": {
            "total_opening_stock": wh.get("total_opening_stock") if isinstance(wh.get("total_opening_stock"), list) else [],
            "total_import_qty": wh.get("total_import_qty") if isinstance(wh.get("total_import_qty"), list) else [],
            "total_export_qty": wh.get("total_export_qty") if isinstance(wh.get("total_export_qty"), list) else [],
        },
        "m01_forecast": {
            "total_actual_qty": m01.get("total_actual_qty") if isinstance(m01.get("total_actual_qty"), list) else [],
            "total_defect_qty": m01.get("total_defect_qty") if isinstance(m01.get("total_defect_qty"), list) else [],
        },
    }


def _log_model_forecast_payload_io(
    *,
    endpoint: str,
    input_payload: dict | None,
    output_payload: dict | None = None,
    status: str = "ok",
    error_detail: object = None,
) -> None:
    compact_input = _compact_forecast_input_for_log(input_payload)
    compact_output = _compact_forecast_output_for_log(output_payload)

    append_ai_flow_log(
        "input_forecast.txt",
        {
            "endpoint": endpoint,
            "status": status,
            "payload": compact_input,
        },
        title="api_input",
    )

    if status == "ok":
        out = {
            "endpoint": endpoint,
            "status": status,
            "payload": compact_output,
        }
    else:
        out = {
            "endpoint": endpoint,
            "status": status,
            "error": error_detail,
        }

    append_ai_flow_log("output_forecast.txt", out, title="api_output")


@router.post("/{ke_hoach_id}/ai-analysis", summary="AI phân tích kế hoạch sản xuất")
def ai_analysis_kehoach(ke_hoach_id: int, payload: dict, db: Session = Depends(get_db)):
    system_payload = AIPlanAnalysisService.build_system_payload(db, ke_hoach_id)
    user_rules = payload.get("rules") if isinstance(payload, dict) else None
    return AIPlanAnalysisService.analyze_with_llm(db, system_payload, user_rules=user_rules)


@router.post(
    "/ai-analysis-payload",
    summary="AI phân tích kế hoạch từ payload calendar (không dùng SQL trên server)",
)
def ai_analysis_kehoach_payload(payload: dict = Body(...), db: Session | None = Depends(get_optional_db)):
    plan_id = None
    if isinstance(payload, dict):
        plan_id = payload.get("planId") or payload.get("KeHoachID")
    logger.info(
        "[AI_ANALYSIS_PAYLOAD] %s called (planId=%s)",
        "/api/v1/kehoach/ai-analysis-payload",
        plan_id,
    )
    result = AIPlanAnalysisService.analyze_with_llm_from_payload_auto(db, payload)
    return result


@router.post(
    "/ai-analysis-payload-batch",
    summary="AI phân tích nhiều kế hoạch từ payload calendar (batch)",
)
def ai_analysis_kehoach_payload_batch(payload: dict = Body(...), db: Session = Depends(get_db)):
    return AIPlanAnalysisService.analyze_with_llm_from_payload_auto_batch(db, payload)


def _normalize_payload_calendar(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload phải là JSON object")

    sys_data = payload.get("calendar") or payload.get("systemData") or payload.get("system_data")
    if not isinstance(sys_data, dict):
        raise HTTPException(status_code=400, detail="Thiếu calendar/systemData")

    if "Header" in sys_data or "Rows" in sys_data or "Days" in sys_data:
        cal = {
            "Header": sys_data.get("Header") or {},
            "Days": sys_data.get("Days") or [],
            "Rows": sys_data.get("Rows") or [],
        }
    else:
        cal = {
            "Header": sys_data.get("header") or {},
            "Days": sys_data.get("days") or [],
            "Rows": sys_data.get("rows") or [],
        }

    if not isinstance(cal.get("Header"), dict):
        cal["Header"] = {}
    if not isinstance(cal.get("Days"), list):
        cal["Days"] = []
    if not isinstance(cal.get("Rows"), list):
        cal["Rows"] = []
    if not cal.get("Rows"):
        raise HTTPException(status_code=400, detail="calendar.Rows trống")

    return cal


def _derive_hr_stages_from_calendar(calendar: dict, payload: dict) -> list[dict]:
    explicit: list[dict] = []
    if isinstance(payload, dict):
        hr_in = payload.get("hr_forecast") if isinstance(payload.get("hr_forecast"), dict) else {}
        raw_stages = hr_in.get("stages") if isinstance(hr_in.get("stages"), list) else []
        for it in raw_stages:
            if isinstance(it, dict):
                explicit.append(it)

    if explicit:
        return explicit

    rows = calendar.get("Rows") if isinstance(calendar, dict) and isinstance(calendar.get("Rows"), list) else []
    by_stage: dict[str, int] = {}

    for r in rows:
        if not isinstance(r, dict):
            continue

        stage_name = str(
            r.get("TenBoPhan")
            or r.get("PhaseGroupName")
            or r.get("TenCongDoanLon")
            or r.get("MaCongDoanLon")
            or ""
        ).strip()
        if not stage_name:
            continue

        staff_raw = r.get("SoNhanSuBoPhan")
        if staff_raw is None:
            staff_raw = r.get("standard_staff_qty")
        if staff_raw is None:
            staff_raw = r.get("SoNhanSu")
        if staff_raw is None:
            staff_raw = r.get("staff")

        try:
            staff = int(float(staff_raw))
        except Exception:
            staff = 0

        if staff <= 0:
            continue

        if stage_name not in by_stage:
            by_stage[stage_name] = staff
        else:
            by_stage[stage_name] = max(int(by_stage[stage_name]), int(staff))

    return [{"major_process": k, "standard_staff_qty": v} for k, v in by_stage.items()]


@router.post(
    "/model-forecast-payload",
    summary="Chạy các model dự báo riêng từ payload calendar (không dùng SQL)",
)
def model_forecast_payload(payload: dict = Body(...), db: Session = Depends(get_db)):
    _ = db
    endpoint = "/api/v1/kehoach/model-forecast-payload"
    input_payload = payload if isinstance(payload, dict) else {"raw": payload}

    try:
        calendar = _normalize_payload_calendar(payload)

        out: dict = {}

        # Always run HR model in this endpoint; derive stages from calendar when client doesn't pass them.
        stages = _derive_hr_stages_from_calendar(calendar, payload)
        hr = HrForecastService.forecast_avg_actual_staff_pct_from_calendar(
            calendar=calendar,
            stages=stages,
        )
        out["days"] = hr.days
        out["hr_forecast"] = {
            "avg_actual_staff_pct": _round_pct_list(hr.avg_actual_staff_pct),
            "meta": hr.meta,
        }

        machine = MachineForecastService.forecast_avg_machine_productivity_pct_from_calendar(calendar=calendar)
        out["machine_forecast"] = {
            "avg_machine_productivity_pct": _round_pct_list(machine.avg_machine_productivity_pct),
            "avg_machine_usage_pct": _round_pct_list(machine.avg_machine_usage_pct),
            "meta": machine.meta,
        }

        available = AvailableResourcesForecastService.forecast_from_hr_and_machine(
            hr_days=hr.days,
            hr_avg_actual_staff_pct=hr.avg_actual_staff_pct,
            machine_days=machine.days,
            machine_avg_machine_productivity_pct=machine.avg_machine_productivity_pct,
        )
        out["available_resources_forecast"] = {
            "available_resources_pct": _round_pct_list(available.available_resources_pct),
            "meta": available.meta,
        }

        warehouse = WarehouseForecastService.forecast_daily_transaction_qty_from_calendar(calendar=calendar)
        out["warehouse_forecast"] = {
            "total_opening_stock": _round_int_list(warehouse.total_opening_stock),
            "total_import_qty": _round_int_list(warehouse.total_import_qty),
            "total_export_qty": _round_int_list(warehouse.total_export_qty),
            "meta": warehouse.meta,
        }

        m01 = M01ProductionForecastService.forecast_daily_production_from_calendar(calendar=calendar)
        out["m01_forecast"] = {
            "total_actual_staff_qty": _round_int_list(m01.total_actual_staff_qty),
            "total_machine_qty": _round_int_list(m01.total_machine_qty),
            "total_ot_staff_qty": _round_int_list(m01.total_ot_staff_qty),
            "total_outsourced_staff_qty": _round_int_list(m01.total_outsourced_staff_qty),
            "total_actual_qty": _round_int_list(m01.total_actual_qty),
            "total_defect_qty": _round_int_list(m01.total_defect_qty),
            "meta": m01.meta,
        }

        _log_model_forecast_payload_io(
            endpoint=endpoint,
            input_payload=input_payload,
            output_payload=out,
            status="ok",
        )
        return out
    except HTTPException as e:
        _log_model_forecast_payload_io(
            endpoint=endpoint,
            input_payload=input_payload,
            output_payload=None,
            status="error",
            error_detail={
                "status_code": int(getattr(e, "status_code", 400) or 400),
                "detail": getattr(e, "detail", str(e)),
            },
        )
        raise
    except Exception as e:
        _log_model_forecast_payload_io(
            endpoint=endpoint,
            input_payload=input_payload,
            output_payload=None,
            status="error",
            error_detail={"error": str(e)},
        )
        raise




@router.post("/{ke_hoach_id}/ai-apply", summary="Áp dụng phương án AI vào kế hoạch sản xuất")
def ai_apply_kehoach(ke_hoach_id: int, payload: dict, db: Session = Depends(get_db)):
    moves = []
    if isinstance(payload, dict):
        raw = payload.get("moves")
        if isinstance(raw, list):
            moves = [m for m in raw if isinstance(m, dict)]
    return AIPlanAnalysisService.apply_moves_to_plan(db, ke_hoach_id=int(ke_hoach_id), moves_raw=moves)


@router.get("/{ke_hoach_id}/hr-forecast", summary="Dự báo năng suất hoạt động nhân sự (%) theo ngày")
def hr_forecast_kehoach(ke_hoach_id: int, db: Session = Depends(get_db)):
    out = HrForecastService.forecast_avg_actual_staff_pct_for_plan(db, int(ke_hoach_id))
    return {
        "plan_id": int(ke_hoach_id),
        "days": out.days,
        "avg_actual_staff_pct": out.avg_actual_staff_pct,
        "meta": out.meta,
    }


@router.get("/{ke_hoach_id}/machine-forecast", summary="Dự báo năng suất hoạt động máy móc (%) theo ngày")
def machine_forecast_kehoach(ke_hoach_id: int, db: Session = Depends(get_db)):
    out = MachineForecastService.forecast_avg_machine_productivity_pct_for_plan(db, int(ke_hoach_id))
    return {
        "plan_id": int(ke_hoach_id),
        "days": out.days,
        "avg_machine_productivity_pct": out.avg_machine_productivity_pct,
        "avg_machine_usage_pct": out.avg_machine_usage_pct,
        "meta": out.meta,
    }


@router.get("/{ke_hoach_id}/model-forecast", summary="Chạy các model dự báo riêng theo kế hoạch")
def model_forecast_kehoach(ke_hoach_id: int, db: Session = Depends(get_db)):
    hr = HrForecastService.forecast_avg_actual_staff_pct_for_plan(db, int(ke_hoach_id))
    machine = MachineForecastService.forecast_avg_machine_productivity_pct_for_plan(db, int(ke_hoach_id))
    return {
        "plan_id": int(ke_hoach_id),
        "hr_forecast": {
            "days": hr.days,
            "avg_actual_staff_pct": hr.avg_actual_staff_pct,
            "meta": hr.meta,
        },
        "machine_forecast": {
            "days": machine.days,
            "avg_machine_productivity_pct": machine.avg_machine_productivity_pct,
            "avg_machine_usage_pct": machine.avg_machine_usage_pct,
            "meta": machine.meta,
        },
    }
