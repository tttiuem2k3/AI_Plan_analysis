from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from math import ceil
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote
import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

import os
import re

from ..core.config import settings
from ..utils.ai_flow_log import append_ai_flow_log, append_numbered_ai_flow_log

from ..ai_plan_analyzer.kpi import compute_kpi, find_hotspots
from ..ai_plan_analyzer.moves import apply_moves
from ..ai_plan_analyzer.rescheduler import local_reschedule
from ..ai_plan_analyzer.service import PlanAIService
from ..ai_plan_analyzer.service import _parse_move
from ..ai_plan_analyzer.openai_client import OpenAIClientError, call_gpt
from ..ai_plan_analyzer.prompt_builder import build_payload, build_system_prompt, build_user_prompt
from ..ai_plan_analyzer.validator import validate_plan
from ..api.v1.plan_ai_router import CalendarPayloadPlanBuilder, KeHoachPlanBuilder, _BusyIntervalsPlanBuilder
from ..ai_plan_analyzer.production_optimizer.service import ProductionPlanningOptimizerService
from ..ai_plan_analyzer.production_optimizer.exceptions import InputValidationError
from ..models.ke_hoach import KeHoachSX_ChiTiet
from ..models.nguon_luc import DM_CongDoanLon, DM_NguonLuc
from ..repositories.ke_hoach_repo import KeHoachRepo


OUTSOURCE_STAFF_MAX = 15


def _log_ai_analysis_payload_input(input_payload: Any) -> int:
    """Append the raw API request body as soon as the endpoint starts."""

    return append_numbered_ai_flow_log(
        "input_analysis.txt",
        input_payload,
        title="api_input",
    )


def _log_ai_analysis_payload_output(
    *,
    log_no: int,
    output_payload: Any = None,
    status: str = "ok",
    error_detail: Any = None,
) -> None:
    """Append the raw API response body with the same sequence number as input."""

    out_content: Any = output_payload if status == "ok" else error_detail
    append_numbered_ai_flow_log(
        "output_analysis.txt",
        out_content,
        title="api_output",
        log_no=log_no,
    )


class AIPlanAnalysisService:
    @staticmethod
    def analyze_with_llm_from_payload_auto(
        db: Session | None,
        payload: Dict[str, Any],
        *,
        log_io: bool = True,
    ) -> Dict[str, Any]:
        """Auto handler for /kehoach/ai-analysis-payload.

        Supports 2 input styles:

        A) Minimal (DB-backed, accurate conflict detection like /{id}/ai-analysis):
        {
          "planId": 116,
          "rules": {...}
        }

        B) Payload calendar (no DB fetch for plan data):
        {
          "planId": 116,
          "calendar"|"systemData": {"Header":...,"Days":...,"Rows":...},
          "otherPlans": [ ... ]  // optional -> converted to busy_intervals
        }
        """

        input_payload = payload
        log_no = _log_ai_analysis_payload_input(input_payload) if log_io else 0
        try:
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="Payload phải là JSON object")

            # Accept API log/test wrapper shape: {"endpoint":..., "status":..., "payload": {...}}
            if isinstance(payload.get("payload"), dict) and not any(
                k in payload
                for k in (
                    "ProductionPlan",
                    "RelatedProductionPlans",
                    "calendar",
                    "systemData",
                    "system_data",
                    "planId",
                    "KeHoachID",
                )
            ):
                payload = payload.get("payload") or {}

            # Multi-main-plan mode for payload style:
            # - Analyze each main plan separately
            # - Return a single combined response
            force_single = bool(payload.get("_force_single_plan"))
            pp_raw = payload.get("ProductionPlan")
            if not force_single and isinstance(pp_raw, list):
                main_plans = [p for p in pp_raw if isinstance(p, dict)]
                if len(main_plans) > 1:
                    client_out = AIPlanAnalysisService._analyze_multi_plan_with_common_output(
                        db,
                        payload,
                        main_plans,
                    )
                    if log_io:
                        _log_ai_analysis_payload_output(
                            log_no=log_no,
                            output_payload=client_out,
                            status="ok",
                        )
                    return client_out

            _validate_ai_analysis_payload_format(payload)

            payload = _normalize_ai_analysis_payload(payload)

            plan_id = _safe_int(payload.get("planId") or payload.get("KeHoachID"), 0)

            user_rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else None

            # If client did not provide calendar/systemData, fall back to DB (keeps previous accuracy)
            sys_data = payload.get("calendar") or payload.get("systemData") or payload.get("system_data")
            if not isinstance(sys_data, dict):
                if db is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Thieu calendar/systemData khi SQL dang tat",
                    )
                if plan_id <= 0:
                    raise HTTPException(status_code=400, detail="Thiếu planId")
                system_payload = AIPlanAnalysisService.build_system_payload(db, int(plan_id))
                raw_result = AIPlanAnalysisService.analyze_with_llm(db, system_payload, user_rules=user_rules)
                client_out = _to_client_ai_analysis_response(raw_result)
                if log_io:
                    _log_ai_analysis_payload_output(
                        log_no=log_no,
                        output_payload=client_out,
                        status="ok",
                    )
                return client_out

            raw_result = AIPlanAnalysisService.analyze_with_llm_from_calendar_payload(db, payload)
            client_out = _to_client_ai_analysis_response(raw_result)
            if log_io:
                _log_ai_analysis_payload_output(
                    log_no=log_no,
                    output_payload=client_out,
                    status="ok",
                )
            return client_out
        except HTTPException as e:
            if log_io:
                _log_ai_analysis_payload_output(
                    log_no=log_no,
                    output_payload=None,
                    status="error",
                    error_detail={
                        "status_code": int(getattr(e, "status_code", 400) or 400),
                        "detail": getattr(e, "detail", str(e)),
                    },
                )
            raise
        except Exception as e:
            if log_io:
                _log_ai_analysis_payload_output(
                    log_no=log_no,
                    output_payload=None,
                    status="error",
                    error_detail={"error": str(e)},
                )
            raise

    @staticmethod
    def _analyze_multi_plan_with_common_output(
        db: Session,
        payload: Dict[str, Any],
        main_plans: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        related = payload.get("RelatedProductionPlans")
        if related is None:
            related = payload.get("relatedProductionPlans")
        if related is None:
            related = payload.get("related_plans")
        base_related = related if isinstance(related, list) else []

        per_plan_results: List[Dict[str, Any]] = []
        merged_suggestions: List[Dict[str, Any]] = []

        for idx, main_plan in enumerate(main_plans):
            item_payload = dict(payload)
            item_payload["ProductionPlan"] = main_plan
            item_payload["_force_single_plan"] = True

            related_for_item = list(base_related)
            for j, other in enumerate(main_plans):
                if j != idx and isinstance(other, dict):
                    related_for_item.append(other)

            if related_for_item:
                item_payload["RelatedProductionPlans"] = related_for_item
            else:
                item_payload.pop("RelatedProductionPlans", None)

            item_result = AIPlanAnalysisService.analyze_with_llm_from_payload_auto(db, item_payload, log_io=False)

            overall = item_result.get("overall_analysis") if isinstance(item_result.get("overall_analysis"), dict) else {}
            plan_info = overall.get("plan") if isinstance(overall.get("plan"), dict) else {}
            if isinstance(plan_info.get("plans"), list) and plan_info.get("plans"):
                plan_info = plan_info.get("plans")[0] if isinstance(plan_info.get("plans")[0], dict) else {}
            analysis_text = str(overall.get("analysis") or "").strip()

            plan_code = plan_info.get("code") or plan_info.get("id") or f"plan_{idx + 1}"
            plan_label = str(plan_code)

            suggestions = plan_info.get("suggestions") if isinstance(plan_info.get("suggestions"), list) else []
            tagged_suggestions: List[Dict[str, Any]] = []
            for s_idx, s in enumerate(suggestions):
                if not isinstance(s, dict):
                    continue
                s2 = dict(s)
                old_key = str(s2.get("key") or f"PA{s_idx + 1}")
                s2["key"] = f"{old_key}_P{idx + 1}"
                title = str(s2.get("title") or "").strip()
                s2["title"] = f"[{plan_label}] {title}" if title else f"[{plan_label}]"
                tagged_suggestions.append(s2)
                merged_suggestions.append(s2)

            per_plan_results.append(
                {
                    "plan": plan_info,
                    "analysis": analysis_text,
                    "suggestions": tagged_suggestions,
                    "source_rows": item_result.get("source_rows") if isinstance(item_result.get("source_rows"), list) else [],
                    "recomputed_plan": item_result.get("recomputed_plan") if isinstance(item_result.get("recomputed_plan"), dict) else {},
                }
            )

        summary_lines: List[str] = [
            "Nội dung phân tích tổng hợp nhiều kế hoạch:",
            f"- Tổng số kế hoạch đã phân tích: {len(per_plan_results)}.",
        ]
        for i, item in enumerate(per_plan_results, start=1):
            p = item.get("plan") if isinstance(item.get("plan"), dict) else {}
            p_code = p.get("code") or p.get("id") or f"plan_{i}"
            p_status = p.get("status")
            p_analysis = str(item.get("analysis") or "").strip().replace("\n", " ")
            if len(p_analysis) > 240:
                p_analysis = p_analysis[:237].rstrip() + "..."

            status_txt = f", trạng thái: {p_status}" if p_status not in (None, "") else ""
            analysis_txt = p_analysis if p_analysis else "không có nội dung phân tích chi tiết"
            summary_lines.append(f"- KH {p_code}{status_txt}: {analysis_txt}.")

        plans_summary = []
        for item in per_plan_results:
            plan_obj = item.get("plan") if isinstance(item.get("plan"), dict) else {}
            plan_suggestions = item.get("suggestions") if isinstance(item.get("suggestions"), list) else []
            plans_summary.append(
                {
                    "APK": _pick_first(plan_obj, ["APK", "PlanAPK", "PlanApk", "KeHoachAPK"]),
                    "code": plan_obj.get("code"),
                    "from": plan_obj.get("from"),
                    "to": plan_obj.get("to"),
                    "status": plan_obj.get("status"),
                    "suggestions": plan_suggestions,
                }
            )

        return {
            "overall_analysis": {
                "plan": {
                    "count": len(per_plan_results),
                    "plans": plans_summary,
                },
                "analysis": "\n".join(summary_lines),
            }
        }

    @staticmethod
    def analyze_with_llm_from_payload_auto_batch(db: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Batch auto handler.

        Accepted shapes:

        1) By ids (simplest for client; DB-backed):
        {"planIds": [116, 117], "rules": {...}, "continue_on_error": true}

        2) By plan payload list (calendar payloads or minimal items):
        {"plans": [ {"planId":116, ...}, {"planId":117, ...} ], "rules": {...}}
        """

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload phải là JSON object")

        continue_on_error = bool(payload.get("continue_on_error"))

        global_rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else None
        global_constraints = payload.get("constraints") if isinstance(payload.get("constraints"), dict) else None

        # Style 1: planIds
        plan_ids = payload.get("planIds") or payload.get("keHoachIds") or payload.get("ke_hoach_ids")
        if isinstance(plan_ids, list) and plan_ids:
            items: List[Dict[str, Any]] = []
            for idx, pid in enumerate(plan_ids):
                try:
                    merged_item: Dict[str, Any] = {
                        "planId": pid,
                    }
                    if global_rules is not None:
                        merged_item["rules"] = global_rules
                    if global_constraints is not None:
                        merged_item["constraints"] = global_constraints
                    res = AIPlanAnalysisService.analyze_with_llm_from_payload_auto(db, merged_item)
                    items.append({"index": idx, "ok": True, "planId": int(pid), "result": res})
                except HTTPException as e:
                    err = {
                        "index": idx,
                        "ok": False,
                        "planId": pid,
                        "status_code": int(getattr(e, "status_code", 400) or 400),
                        "detail": getattr(e, "detail", str(e)),
                    }
                    items.append(err)
                    if not continue_on_error:
                        raise
                except Exception as e:
                    err = {"index": idx, "ok": False, "planId": pid, "error": str(e)}
                    items.append(err)
                    if not continue_on_error:
                        raise HTTPException(status_code=500, detail=err)
            return {"count": len(items), "items": items}

        # Style 2: plans
        plans = payload.get("plans")
        if not isinstance(plans, list) or not plans:
            raise HTTPException(status_code=400, detail="Thiếu planIds hoặc plans")

        items2: List[Dict[str, Any]] = []
        for idx, p in enumerate(plans):
            if not isinstance(p, dict):
                err = {"index": idx, "ok": False, "error": "Item trong plans phải là object"}
                items2.append(err)
                if not continue_on_error:
                    raise HTTPException(status_code=400, detail=err)
                continue

            merged = dict(p)
            if global_rules is not None and not isinstance(merged.get("rules"), dict):
                merged["rules"] = global_rules
            if global_constraints is not None and not isinstance(merged.get("constraints"), dict):
                merged["constraints"] = global_constraints

            plan_id = merged.get("planId") or merged.get("KeHoachID")
            try:
                res = AIPlanAnalysisService.analyze_with_llm_from_payload_auto(db, merged)
                items2.append({"index": idx, "ok": True, "planId": plan_id, "result": res})
            except HTTPException as e:
                err = {
                    "index": idx,
                    "ok": False,
                    "planId": plan_id,
                    "status_code": int(getattr(e, "status_code", 400) or 400),
                    "detail": getattr(e, "detail", str(e)),
                }
                items2.append(err)
                if not continue_on_error:
                    raise
            except Exception as e:
                err = {"index": idx, "ok": False, "planId": plan_id, "error": str(e)}
                items2.append(err)
                if not continue_on_error:
                    raise HTTPException(status_code=500, detail=err)

        return {"count": len(items2), "items": items2}
    @staticmethod
    def build_system_payload(db: Session, ke_hoach_id: int) -> Dict[str, Any]:
        calendar = KeHoachRepo.get_calendar_view(db, ke_hoach_id)
        if not calendar:
            raise HTTPException(status_code=404, detail="Không tìm thấy kế hoạch")

        return {
            "planId": int(ke_hoach_id),
            "calendar": calendar,
            "affectedPlans": [],
        }

    @staticmethod
    def analyze_with_llm(
        db: Session,
        system_payload: Dict[str, Any],
        user_rules: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Luồng thật: kế hoạch → OR-Tools/validator chuẩn hoá + đọc lỗi → gọi LLM → chuẩn hoá output → trả về FE."""

        plan_id = int(system_payload.get("planId") or 0)
        if plan_id <= 0:
            raise HTTPException(status_code=400, detail="Thiếu planId")

        cal = system_payload.get("calendar") or {}
        header = (cal.get("Header") or {}) if isinstance(cal, dict) else {}

        _enrich_calendar_department_staff(db, cal)
        business_context = _build_business_context_from_calendar(cal)

        # horizon from header; fallback 30 days
        now = datetime.now().replace(second=0, microsecond=0)
        horizon_start = now
        horizon_end = now + timedelta(days=30)
        try:
            tu = header.get("TuNgay")
            den = header.get("DenNgay")
            if tu is not None:
                horizon_start = datetime.combine(tu, datetime.min.time())
            if den is not None:
                horizon_end = datetime.combine(den, datetime.max.time()).replace(microsecond=0)
        except Exception:
            pass

        # Prefer LLM flow via PlanAIService; fallback to deterministic when missing API key.
        top_n = 6
        try:
            if isinstance(user_rules, dict) and user_rules.get("top_n") is not None:
                top_n = int(user_rules.get("top_n"))
        except Exception:
            top_n = 6
        top_n = max(1, min(20, int(top_n)))

        inner = KeHoachPlanBuilder()
        builder = _BusyIntervalsPlanBuilder(inner, db=db, plan_id=int(plan_id))

        provider = str(os.getenv("LLM_PROVIDER") or getattr(settings, "LLM_PROVIDER", None) or "openai").strip().lower()
        if provider == "gemini":
            llm_enabled = bool(os.getenv("GEMINI_API_KEY") or getattr(settings, "GEMINI_API_KEY", None))
        elif provider == "openrouter":
            llm_enabled = bool(os.getenv("OPENROUTER_API_KEY") or getattr(settings, "OPENROUTER_API_KEY", None))
        else:
            llm_enabled = bool(os.getenv("OPENAI_API_KEY") or getattr(settings, "OPENAI_API_KEY", None))

        if not llm_enabled:
            required_key = (
                "GEMINI_API_KEY"
                if provider == "gemini"
                else "OPENROUTER_API_KEY"
                if provider == "openrouter"
                else "OPENAI_API_KEY"
            )
            raise HTTPException(
                status_code=503,
                detail=f"Thiếu cấu hình LLM bắt buộc cho provider '{provider}'. Vui lòng thiết lập {required_key}.",
            )

        svc = PlanAIService(plan_builder=builder)
        result = svc.analyze_and_propose(
            db,
            int(plan_id),
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            top_n=top_n,
            extra_context=business_context,
        )

        machine_name_map = _build_machine_name_map(db, business_context)
        btp_metrics_map = _build_btp_metrics_map(business_context)

        analysis_text = _analysis_dict_to_text(result.get("analysis"))
        if not analysis_text:
            # LLM may omit analysis; fall back to deterministic from current data.
            problem_rows = (business_context or {}).get("problem_rows") or []
            if not isinstance(problem_rows, list):
                problem_rows = []
            plan0 = builder.get_plan(db, int(plan_id), horizon_start, horizon_end)
            try:
                plan0.constraints = builder.get_constraints(db, horizon_start, horizon_end) or {}
            except Exception:
                plan0.constraints = {}
            errors0 = validate_plan(plan0)
            kpi0 = compute_kpi(plan0)
            hotspots0 = find_hotspots(plan0, kpi0)
            items0 = _build_overall_analysis_items(problem_rows, errors0 or [], hotspots0)
            analysis_text = _analysis_items_to_text(items0)

        # Normalize display formatting from LLM
        analysis_text = _sanitize_analysis_text(analysis_text)
        analysis_text = _patch_machine_codes(analysis_text, machine_name_map)
        analysis_text = _sanitize_jargon(_remove_redundant_machine_parens(analysis_text))
        analysis_text = _soften_conflict_claims_if_no_conflicts(
            analysis_text,
            baseline_validation_summary=result.get("baseline_validation_summary"),
        )
        analysis_text = _patch_analysis_with_dependency_summary(
            analysis_text,
            business_context=business_context,
            baseline_validation_summary=result.get("baseline_validation_summary"),
        )

        suggestions, ai_plans = _map_options_to_ui(
            result.get("options") or [],
            machine_name_map=machine_name_map,
            current_plan_code=str(header.get("MaKeHoach") or header.get("KeHoachID") or "") or None,
            btp_metrics_map=btp_metrics_map,
        )

        # Ensure Template 6 (due date adjustment) appears when the plan still has late/unplanned qty.
        try:
            has_tpl6 = any(
                isinstance(s, dict)
                and isinstance(s.get("meta"), dict)
                and int(s["meta"].get("template_no") or 0) == 6
                for s in (suggestions or [])
            )
        except Exception:
            has_tpl6 = False

        if not has_tpl6:
            tpl6 = _build_due_date_extension_suggestion((business_context or {}).get("problem_rows") or [])
            if tpl6:
                tpl6_key = f"PA{len(suggestions) + 1}"
                tpl6["key"] = tpl6_key
                suggestions.append(tpl6)

        suggestions, ai_plans = _apply_due_date_only_mode_if_needed(
            suggestions,
            ai_plans,
            kpi_like=result.get("baseline_kpi"),
        )

        return {
            "plan": {
                "APK": _pick_first(header, ["APK","APK_Master", "APKMaster", "PlanAPK", "PlanApk", "KeHoachAPK"]),
                "code": header.get("MaKeHoach") or header.get("KeHoachID") or plan_id,
                "from": header.get("TuNgay"),
                "to": header.get("DenNgay"),
                "status": header.get("TrangThai"),
            },
            "baseline_kpi": result.get("baseline_kpi"),
            "suggestions": suggestions,
            "ai_plans": ai_plans,
            "source_rows": cal.get("Rows") if isinstance(cal, dict) and isinstance(cal.get("Rows"), list) else [],
            "analysis": analysis_text,
            "llm_error": result.get("llm_error"),
            "debug": {
                "rules": user_rules or {},
                "llm_called": True,
                "model": (
                    os.getenv("GEMINI_MODEL")
                    or getattr(settings, "GEMINI_MODEL", None)
                    or "gemini-1.5-flash"
                    if provider == "gemini"
                    else os.getenv("OPENROUTER_MODEL")
                    or getattr(settings, "OPENROUTER_MODEL", None)
                    or "qwen/qwen3.6-plus"
                    if provider == "openrouter"
                    else os.getenv("OPENAI_MODEL")
                    or getattr(settings, "OPENAI_MODEL", None)
                    or "gpt-4.1"
                ),
                "telemetry": result.get("telemetry"),
                "has_issues": result.get("has_issues"),
                "llm_skipped": result.get("llm_skipped"),
                "llm_skip_reason": result.get("llm_skip_reason"),
                "baseline_validation_summary": result.get("baseline_validation_summary"),
                "baseline_validation_explain": result.get("baseline_validation_explain"),
                "prompt": result.get("debug_prompt"),
                "llm_io": _compact_llm_io_for_client(
                    result.get("debug_llm_io"),
                    include_raw=bool((user_rules or {}).get("debug_full")),
                ),
            },
        }

        # ---- Deterministic fallback (no API key) ----
        plan = builder.get_plan(db, int(plan_id), horizon_start, horizon_end)
        try:
            plan.constraints = builder.get_constraints(db, horizon_start, horizon_end) or {}
        except Exception:
            plan.constraints = {}
        try:
            plan.capacity = builder.get_capacity(db, horizon_start, horizon_end) or {}
        except Exception:
            plan.capacity = {}
        try:
            plan.due_dates = builder.get_due_dates(db, int(plan_id)) or {}
        except Exception:
            plan.due_dates = {}

        errors = validate_plan(plan)
        kpi = compute_kpi(plan)
        hotspots = find_hotspots(plan, kpi)
        problem_rows = business_context.get("problem_rows") if isinstance(business_context, dict) else []
        if not isinstance(problem_rows, list):
            problem_rows = []
        analysis_items = _build_overall_analysis_items(problem_rows, errors or [], hotspots)
        analysis = _analysis_items_to_text(analysis_items)
        suggestions = _build_suggestions_1_to_6(problem_rows, errors or [], hotspots)

        err_dicts = [asdict(e) for e in (errors or [])]
        err_summary: Dict[str, int] = {}
        for e in errors or []:
            code = str(getattr(e, "code", "") or "UNKNOWN")
            err_summary[code] = int(err_summary.get(code, 0)) + 1

        return {
            "plan": {
                "id": plan_id,
                "APK": _pick_first(header, ["APK", "PlanAPK", "PlanApk", "KeHoachAPK"]),
                "code": header.get("MaKeHoach") or header.get("KeHoachID") or plan_id,
                "from": header.get("TuNgay"),
                "to": header.get("DenNgay"),
                "status": header.get("TrangThai"),
            },
            "baseline_kpi": asdict(kpi),
            "suggestions": suggestions,
            "ai_plans": {},
            "source_rows": cal.get("Rows") if isinstance(cal, dict) and isinstance(cal.get("Rows"), list) else [],
            "analysis": analysis,
            "llm_error": "Missing OPENAI_API_KEY (fallback deterministic)",
            "debug": {
                "rules": user_rules or {},
                "llm_called": False,
                "baseline_validation_summary": err_summary,
                "baseline_validation_explain": err_dicts[:50],
                "hotspots": hotspots,
            },
        }

    @staticmethod
    def analyze_with_llm_from_calendar_payload(db: Session | None, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze a plan using client-provided calendar JSON (no plan fetch from DB).

        Expected payload (best-effort; extra fields ignored):
        {
          "planId": 123,
          "systemData": {"header": {...}, "days": [...], "rows": [...]}
          // or "calendar": {"Header": {...}, "Days": [...], "Rows": [...]}
          "rules": {...},
          "constraints": {"busy_intervals": [...]}
        }
        """

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload phải là JSON object")

        _validate_ai_analysis_payload_format(payload)

        payload = _normalize_ai_analysis_payload(payload)

        plan_id = _safe_int(payload.get("planId") or payload.get("KeHoachID"), 0)

        user_rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else None

        # Normalize calendar shape to {Header, Days, Rows}
        sys_data = payload.get("calendar") or payload.get("systemData") or payload.get("system_data")
        if not isinstance(sys_data, dict):
            raise HTTPException(status_code=400, detail="Thiếu calendar/systemData")

        if "Header" in sys_data or "Rows" in sys_data or "Days" in sys_data:
            calendar = {
                "Header": sys_data.get("Header") or {},
                "Days": sys_data.get("Days") or [],
                "Rows": sys_data.get("Rows") or [],
            }
        else:
            calendar = {
                "Header": sys_data.get("header") or {},
                "Days": sys_data.get("days") or [],
                "Rows": sys_data.get("rows") or [],
            }

        if not isinstance(calendar.get("Header"), dict):
            calendar["Header"] = {}
        if not isinstance(calendar.get("Days"), list):
            calendar["Days"] = []
        if not isinstance(calendar.get("Rows"), list):
            calendar["Rows"] = []
        if not calendar.get("Rows"):
            raise HTTPException(status_code=400, detail="calendar.Rows trống")

        if plan_id <= 0:
            h0 = calendar.get("Header") if isinstance(calendar.get("Header"), dict) else {}
            plan_id = _safe_int((h0 or {}).get("KeHoachID"), 0)
        if plan_id <= 0:
            plan_id = 1

        header = calendar.get("Header") or {}
        business_context = _build_business_context_from_calendar(calendar)

        # Horizon: prefer payload override, else header TuNgay/DenNgay, else 30 days.
        now = datetime.now().replace(second=0, microsecond=0)
        horizon_start = now
        horizon_end = now + timedelta(days=30)

        def _parse_dt(v: Any) -> Optional[datetime]:
            if v is None or v == "":
                return None
            if isinstance(v, datetime):
                return v
            try:
                # date -> datetime
                if isinstance(v, date) and not isinstance(v, datetime):
                    return datetime.combine(v, datetime.min.time())
            except Exception:
                pass
            if isinstance(v, str):
                s = v.strip().replace("Z", "+00:00")
                if not s:
                    return None
                try:
                    return datetime.fromisoformat(s)
                except Exception:
                    try:
                        return datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        try:
                            return datetime.strptime(s[:10], "%Y-%m-%d")
                        except Exception:
                            return None
            return None

        try:
            if payload.get("horizon_start") is not None:
                hs = _parse_dt(payload.get("horizon_start"))
                if hs:
                    horizon_start = hs
            if payload.get("horizon_end") is not None:
                he = _parse_dt(payload.get("horizon_end"))
                if he:
                    horizon_end = he
        except Exception:
            pass

        try:
            if payload.get("horizon_start") is None and header.get("TuNgay") is not None:
                tu = _parse_dt(header.get("TuNgay"))
                if tu:
                    horizon_start = datetime.combine(tu.date(), datetime.min.time())
            if payload.get("horizon_end") is None and header.get("DenNgay") is not None:
                den = _parse_dt(header.get("DenNgay"))
                if den:
                    horizon_end = datetime.combine(den.date(), datetime.max.time()).replace(microsecond=0)
        except Exception:
            pass

        # top_n
        top_n = 6
        try:
            if isinstance(user_rules, dict) and user_rules.get("top_n") is not None:
                top_n = int(user_rules.get("top_n"))
        except Exception:
            top_n = 6
        top_n = max(1, min(20, int(top_n)))

        # Busy intervals can be provided by client; otherwise none.
        constraints_in = payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {}
        busy_intervals = constraints_in.get("busy_intervals") if isinstance(constraints_in, dict) else None
        if not isinstance(busy_intervals, list):
            busy_intervals = []

        # Optional: accept other plans as calendars and convert them into busy intervals
        # (equivalent to KeHoachRepo.get_busy_intervals_other_plans but client-provided).
        other_plans = payload.get("otherPlans")
        if other_plans is None:
            other_plans = payload.get("affectedPlans")
        if other_plans is None:
            other_plans = payload.get("other_plans")

        def _normalize_calendar_obj(x: Any) -> Optional[Dict[str, Any]]:
            if not isinstance(x, dict):
                return None
            sys_data = x.get("calendar") or x.get("systemData") or x.get("system_data") or x
            if not isinstance(sys_data, dict):
                return None
            if "Header" in sys_data or "Rows" in sys_data or "Days" in sys_data:
                cal0 = {
                    "Header": sys_data.get("Header") or {},
                    "Days": sys_data.get("Days") or [],
                    "Rows": sys_data.get("Rows") or [],
                }
            else:
                cal0 = {
                    "Header": sys_data.get("header") or {},
                    "Days": sys_data.get("days") or [],
                    "Rows": sys_data.get("rows") or [],
                }
            if not isinstance(cal0.get("Header"), dict):
                cal0["Header"] = {}
            if not isinstance(cal0.get("Days"), list):
                cal0["Days"] = []
            if not isinstance(cal0.get("Rows"), list):
                cal0["Rows"] = []
            return cal0

        related_plans_for_ortools: list[dict] = []
        if isinstance(other_plans, list) and other_plans:
            derived_busy: list[dict] = []
            for op in other_plans:
                cal_other = _normalize_calendar_obj(op)
                if not isinstance(cal_other, dict):
                    continue
                related_plans_for_ortools.append(cal_other)
                h_other = cal_other.get("Header") or {}
                other_pid = h_other.get("KeHoachID") or h_other.get("planId") or h_other.get("PlanId")
                other_code = h_other.get("MaKeHoach") or h_other.get("KeHoachCode")
                other_pid_int = _safe_int(other_pid, 0)
                for r in cal_other.get("Rows") or []:
                    if not isinstance(r, dict):
                        continue
                    st_raw = _pick_first(r, ["StartDT", "Start", "StartDate"])
                    en_raw = _pick_first(r, ["EndDT", "End", "EndDate"])
                    st_dt = _parse_dt(st_raw)
                    en_dt = _parse_dt(en_raw)
                    if not st_dt or not en_dt:
                        continue

                    # machine busy (split CSV)
                    m_raw = _pick_first(r, ["MaNguonLuc", "ResourceIDs"])
                    m_list: list[str] = []
                    if m_raw not in (None, ""):
                        if isinstance(m_raw, list):
                            m_list = [str(x).strip() for x in m_raw if str(x or "").strip()]
                        else:
                            m_list = [x.strip() for x in str(m_raw).split(",") if x and str(x).strip()]

                    for m in m_list:
                        derived_busy.append(
                            {
                                "resource_type": "machine",
                                "resource_id": str(m),
                                "start_dt": st_dt,
                                "end_dt": en_dt,
                                "ke_hoach_id": other_pid_int if other_pid_int > 0 else None,
                                "ma_ke_hoach": str(other_code) if other_code not in (None, "") else None,
                            }
                        )

                    # dept busy
                    dept_code = _pick_first(r, ["MaCongDoanLon", "PhaseGroupID"])
                    if dept_code not in (None, ""):
                        derived_busy.append(
                            {
                                "resource_type": "dept",
                                "resource_id": str(dept_code),
                                "start_dt": st_dt,
                                "end_dt": en_dt,
                                "ke_hoach_id": other_pid_int if other_pid_int > 0 else None,
                                "ma_ke_hoach": str(other_code) if other_code not in (None, "") else None,
                            }
                        )

            if derived_busy:
                busy_intervals = list(busy_intervals) + derived_busy

        # Accept both internal busy-interval schema (resource_type/resource_id/start_dt/end_dt)
        # and a simplified client schema ({machine|dept, start, end, reason}).
        normalized_busy: list[dict] = []
        for b in busy_intervals:
            if not isinstance(b, dict):
                continue

            # Already in internal schema
            if b.get("resource_type") and b.get("resource_id") and b.get("start_dt") and b.get("end_dt"):
                normalized_busy.append(b)
                continue

            rid = b.get("resource_id")
            rtype = b.get("resource_type")

            # Simplified schema
            if rtype in (None, ""):
                if b.get("machine") not in (None, ""):
                    rtype = "machine"
                    rid = b.get("machine")
                elif b.get("dept") not in (None, ""):
                    rtype = "dept"
                    rid = b.get("dept")

            st = b.get("start_dt") if b.get("start_dt") not in (None, "") else b.get("start")
            en = b.get("end_dt") if b.get("end_dt") not in (None, "") else b.get("end")
            st_dt = _parse_dt(st)
            en_dt = _parse_dt(en)
            if not rtype or rid in (None, "") or not st_dt or not en_dt:
                continue

            nb = dict(b)
            nb["resource_type"] = str(rtype)
            nb["resource_id"] = str(rid)
            nb["start_dt"] = st_dt
            nb["end_dt"] = en_dt
            normalized_busy.append(nb)

        busy_intervals = normalized_busy

        enforce_dept_no_overlap = None
        try:
            if isinstance(user_rules, dict) and user_rules.get("deptExclusive") is not None:
                enforce_dept_no_overlap = bool(user_rules.get("deptExclusive"))
        except Exception:
            enforce_dept_no_overlap = None

        builder = CalendarPayloadPlanBuilder(
            calendar,
            busy_intervals=busy_intervals,
            related_production_plans=related_plans_for_ortools,
            enforce_dept_no_overlap=enforce_dept_no_overlap,
        )

        return _run_optimizer_and_map_raw(
            payload=payload,
            calendar=calendar,
            user_rules=user_rules,
            plan_id=int(plan_id),
        )

        # Deterministic fallback
        plan = builder.get_plan(db, int(plan_id), horizon_start, horizon_end)
        try:
            plan.constraints = builder.get_constraints(db, horizon_start, horizon_end) or {}
        except Exception:
            plan.constraints = {}
        try:
            plan.capacity = builder.get_capacity(db, horizon_start, horizon_end) or {}
        except Exception:
            plan.capacity = {}
        try:
            plan.due_dates = builder.get_due_dates(db, int(plan_id)) or {}
        except Exception:
            plan.due_dates = {}

        errors = validate_plan(plan)
        kpi = compute_kpi(plan)
        hotspots = find_hotspots(plan, kpi)
        problem_rows = business_context.get("problem_rows") if isinstance(business_context, dict) else []
        if not isinstance(problem_rows, list):
            problem_rows = []
        analysis_items = _build_overall_analysis_items(problem_rows, errors or [], hotspots)
        analysis = _analysis_items_to_text(analysis_items)
        suggestions = _build_suggestions_1_to_6(problem_rows, errors or [], hotspots)

        err_dicts = [asdict(e) for e in (errors or [])]
        err_summary: Dict[str, int] = {}
        for e in errors or []:
            code = str(getattr(e, "code", "") or "UNKNOWN")
            err_summary[code] = int(err_summary.get(code, 0)) + 1

        return {
            "plan": {
                "id": plan_id,
                "APK": _pick_first(header, ["APK", "PlanAPK", "PlanApk", "KeHoachAPK"]),
                "code": header.get("MaKeHoach") or header.get("KeHoachID") or plan_id,
                "from": header.get("TuNgay"),
                "to": header.get("DenNgay"),
                "status": header.get("TrangThai"),
            },
            "baseline_kpi": asdict(kpi),
            "suggestions": suggestions,
            "ai_plans": {},
            "source_rows": calendar.get("Rows") if isinstance(calendar, dict) and isinstance(calendar.get("Rows"), list) else [],
            "analysis": analysis,
            "llm_error": "Missing OPENAI_API_KEY (fallback deterministic)",
            "debug": {
                "rules": user_rules or {},
                "llm_called": False,
                "baseline_validation_summary": err_summary,
                "baseline_validation_explain": err_dicts[:50],
                "hotspots": hotspots,
            },
        }

    @staticmethod
    def apply_moves_to_plan(
        db: Session,
        *,
        ke_hoach_id: int,
        moves_raw: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Apply selected moves into the current plan and persist segments to DB."""

        plan_id = int(ke_hoach_id)
        if plan_id <= 0:
            raise HTTPException(status_code=400, detail="Thiếu ke_hoach_id")

        calendar = KeHoachRepo.get_calendar_view(db, plan_id)
        if not calendar:
            raise HTTPException(status_code=404, detail="Không tìm thấy kế hoạch")

        header = calendar.get("Header") or {}
        tu = header.get("TuNgay")
        den = header.get("DenNgay")

        # Horizon from header (best-effort)
        now = datetime.now().replace(second=0, microsecond=0)
        horizon_start = now
        horizon_end = now + timedelta(days=30)
        try:
            if tu is not None:
                horizon_start = datetime.combine(tu, datetime.min.time())
            if den is not None:
                horizon_end = datetime.combine(den, datetime.max.time()).replace(microsecond=0)
        except Exception:
            pass

        inner = KeHoachPlanBuilder()
        builder = _BusyIntervalsPlanBuilder(inner, db=db, plan_id=int(plan_id))

        plan = builder.get_plan(db, int(plan_id), horizon_start, horizon_end)

    @staticmethod
    def analyze_with_llm_from_calendar_payload_batch(db: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze multiple plans in one request.

        Expected payload shape:
        {
          "plans": [ {"planId": 1, "calendar": {...}, "rules": {...}, "constraints": {...}}, ... ],
          "rules": {...},         # optional defaults for all items
          "constraints": {...},   # optional defaults for all items
          "continue_on_error": true|false
        }
        """

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload phải là JSON object")

        plans = payload.get("plans")
        if not isinstance(plans, list) or not plans:
            raise HTTPException(status_code=400, detail="Thiếu plans (list)")

        continue_on_error = bool(payload.get("continue_on_error"))

        global_rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else None
        global_constraints = payload.get("constraints") if isinstance(payload.get("constraints"), dict) else None

        items: List[Dict[str, Any]] = []
        for idx, p in enumerate(plans):
            if not isinstance(p, dict):
                err = {"index": idx, "ok": False, "error": "Item trong plans phải là object"}
                items.append(err)
                if not continue_on_error:
                    raise HTTPException(status_code=400, detail=err)
                continue

            merged = dict(p)

            # Apply global defaults (item-level wins)
            if global_rules is not None and not isinstance(merged.get("rules"), dict):
                merged["rules"] = global_rules
            if global_constraints is not None and not isinstance(merged.get("constraints"), dict):
                merged["constraints"] = global_constraints

            plan_id = merged.get("planId") or merged.get("KeHoachID")
            try:
                res = AIPlanAnalysisService.analyze_with_llm_from_calendar_payload(db, merged)
                items.append({"index": idx, "ok": True, "planId": plan_id, "result": res})
            except HTTPException as e:
                err = {
                    "index": idx,
                    "ok": False,
                    "planId": plan_id,
                    "status_code": int(getattr(e, "status_code", 400) or 400),
                    "detail": getattr(e, "detail", str(e)),
                }
                items.append(err)
                if not continue_on_error:
                    raise
            except Exception as e:
                err = {"index": idx, "ok": False, "planId": plan_id, "error": str(e)}
                items.append(err)
                if not continue_on_error:
                    raise HTTPException(status_code=500, detail=err)

        return {"count": len(items), "items": items}
        try:
            plan.constraints = builder.get_constraints(db, horizon_start, horizon_end) or {}
        except Exception:
            plan.constraints = {}
        try:
            plan.due_dates = builder.get_due_dates(db, int(plan_id)) or {}
        except Exception:
            plan.due_dates = {}

        moves = [_parse_move(m) for m in (moves_raw or []) if isinstance(m, dict)]
        if not moves:
            raise HTTPException(status_code=400, detail="Thiếu moves để áp dụng")

        patched, scope = apply_moves(plan, moves)
        patched = local_reschedule(patched, scope)

        affected_machines = set(scope.get("machines") or [])
        if not affected_machines:
            raise HTTPException(
                status_code=400,
                detail="Phương án không xác định được máy/nguồn lực bị ảnh hưởng để cập nhật. (Thiếu machine/to_machine?)",
            )

        errors = validate_plan(patched)
        if errors:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Phương án áp dụng tạo xung đột lịch (cần điều chỉnh trước khi lưu).",
                    "validation_errors": [asdict(e) for e in errors],
                    "affected_scope": scope,
                },
            )

        persisted_ops = 0
        persisted_segments = 0

        for op in getattr(patched, "operations", []) or []:
            if affected_machines and (op.machine not in affected_machines):
                continue
            if not op.segments:
                continue

            st = None
            try:
                starts = [s.start for s in op.segments if getattr(s, "start", None) is not None]
                st = min(starts) if starts else None
            except Exception:
                st = None
            if st is None:
                continue

            key = _parse_op_id_for_db(str(op.op_id))
            if not key:
                continue

            q = db.query(KeHoachSX_ChiTiet).filter(KeHoachSX_ChiTiet.KeHoachID == int(plan_id))
            if key.get("DonHangID") is not None:
                q = q.filter(KeHoachSX_ChiTiet.DonHangID == int(key["DonHangID"]))
            if key.get("LineKey") is not None:
                q = q.filter(KeHoachSX_ChiTiet.LineKey == str(key["LineKey"]))
            if key.get("TP_DinhMucID") is not None:
                q = q.filter(KeHoachSX_ChiTiet.TP_DinhMucID == int(key["TP_DinhMucID"]))
            if key.get("BTP_DinhMucID") is not None:
                q = q.filter(KeHoachSX_ChiTiet.BTP_DinhMucID == int(key["BTP_DinhMucID"]))
            if key.get("MaCongDoan") is not None:
                q = q.filter(KeHoachSX_ChiTiet.MaCongDoan == str(key["MaCongDoan"]))
            if key.get("ThuTuSX") is not None:
                q = q.filter(KeHoachSX_ChiTiet.ThuTuSX == int(key["ThuTuSX"]))

            rows = q.order_by(KeHoachSX_ChiTiet.SegmentID.asc()).all()
            if not rows:
                continue

            cur = st
            for r in rows:
                try:
                    if op.machine:
                        r.MaNguonLuc = str(op.machine)
                except Exception:
                    pass

                setup_min = int(getattr(r, "SetupMinutes", 0) or 0)
                run_min = int(getattr(r, "RunMinutes", 0) or 0)
                dur = max(0, setup_min + run_min)

                r.StartDT = cur
                r.EndDT = cur + timedelta(minutes=dur) if dur > 0 else cur
                cur = r.EndDT
                persisted_segments += 1

            persisted_ops += 1

        db.commit()

        calendar2 = KeHoachRepo.get_calendar_view(db, plan_id)
        return {
            "ok": True,
            "ke_hoach_id": int(plan_id),
            "persisted_ops": int(persisted_ops),
            "persisted_segments": int(persisted_segments),
            "affected_scope": scope,
            "calendar": calendar2,
        }


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v in (None, ""):
            return int(default)
        if isinstance(v, bool):
            return int(default)
        if isinstance(v, int):
            return int(v)
        if isinstance(v, float):
            return int(v)
        s = str(v).strip()
        if not s:
            return int(default)
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
        return int(float(s))
    except Exception:
        return int(default)


def _id_token(v: Any, default: str = "0") -> str:
    if v in (None, ""):
        return str(default)
    try:
        if isinstance(v, bool):
            return str(default)
        if isinstance(v, int):
            return str(int(v))
        if isinstance(v, float):
            return str(int(v))
        s = str(v).strip()
        if not s:
            return str(default)
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return str(int(s))
        try:
            return str(int(float(s)))
        except Exception:
            return "S" + quote(s, safe="")
    except Exception:
        return str(default)


def _pick_first(d: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            break
        if k in d and d.get(k) not in (None, ""):
            return d.get(k)
    return default


def _parse_date_any(v: Any) -> Optional[datetime]:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date) and not isinstance(v, datetime):
        return datetime.combine(v, datetime.min.time())
    s = str(v).strip()
    if not s:
        return None
    s2 = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s2)
    except Exception:
        pass
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except Exception:
            continue
    return None


def _to_iso_date_or_raw(v: Any) -> Any:
    dt = _parse_date_any(v)
    if dt is not None:
        return dt.date().isoformat()
    return v


def _normalize_resource_ids_any(v: Any) -> tuple[Any, Optional[List[Dict[str, Any]]]]:
    """Normalize ResourceIDs from multiple client shapes.

    Accepted shapes:
    - "M014, M015"
    - ["M014", "M015"]
    - [{"ResourceID":"M014","TimeResource":420}, ...]
    """

    if v in (None, ""):
        return v, None

    if isinstance(v, list):
        codes: List[str] = []
        details: List[Dict[str, Any]] = []
        for item in v:
            if isinstance(item, dict):
                rid = _pick_first(item, ["ResourceID", "resource_id", "MaNguonLuc", "MachineID"])
                if rid not in (None, ""):
                    rids = str(rid).strip()
                    if rids:
                        codes.append(rids)
                        details.append(
                            {
                                "ResourceID": rids,
                                "TimeResource": _pick_first(item, ["TimeResource", "time_resource", "Minutes"]),
                            }
                        )
            else:
                s = str(item or "").strip()
                if s:
                    codes.append(s)

        codes_joined = ", ".join([c for c in codes if c])
        return codes_joined, (details if details else None)

    if isinstance(v, dict):
        rid = _pick_first(v, ["ResourceID", "resource_id", "MaNguonLuc", "MachineID"])
        if rid in (None, ""):
            return "", None
        rids = str(rid).strip()
        return rids, [{"ResourceID": rids, "TimeResource": _pick_first(v, ["TimeResource", "time_resource", "Minutes"])}]

    return v, None


def _validate_ai_analysis_payload_format(payload: Dict[str, Any]) -> None:
    """Validate ProductionPlan payload shape (best-effort, non-breaking for legacy styles)."""

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload phải là JSON object")

    pp = payload.get("ProductionPlan")
    if pp is None:
        return
    def _validate_one_plan(plan_obj: Dict[str, Any], prefix: str) -> None:
        master = plan_obj.get("Master")
        detail = plan_obj.get("Detail")

        if master is not None and not isinstance(master, dict):
            raise HTTPException(status_code=400, detail=f"{prefix}.Master phải là object")
        if detail is not None and not isinstance(detail, list):
            raise HTTPException(status_code=400, detail=f"{prefix}.Detail phải là list")

        if isinstance(detail, list):
            for i, row in enumerate(detail):
                if not isinstance(row, dict):
                    raise HTTPException(status_code=400, detail=f"{prefix}.Detail[{i}] phải là object")

                dq = row.get("DailyQty")
                if dq is None:
                    dq = row.get("DailyQuantity")
                if dq is not None and not isinstance(dq, list):
                    raise HTTPException(status_code=400, detail=f"{prefix}.Detail[{i}].DailyQty phải là list")

                if isinstance(dq, list):
                    for j, item in enumerate(dq):
                        if not isinstance(item, dict):
                            raise HTTPException(
                                status_code=400,
                                detail=f"{prefix}.Detail[{i}].DailyQty[{j}] phải là object",
                            )
                        pd = _pick_first(item, ["ProductionDate", "Date", "Ngay"])
                        if pd not in (None, "") and _parse_date_any(pd) is None:
                            raise HTTPException(
                                status_code=400,
                                detail=f"{prefix}.Detail[{i}].DailyQty[{j}].ProductionDate không đúng định dạng ngày",
                            )
                        qty_raw = _pick_first(item, ["Quantity", "Qty", "SoLuong"])
                        if qty_raw not in (None, ""):
                            try:
                                float(qty_raw)
                            except Exception:
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"{prefix}.Detail[{i}].DailyQty[{j}].Quantity phải là số",
                                )

                for fld in ("StartDate", "EndDate", "DueDate"):
                    v = _pick_first(row, [fld])
                    if v not in (None, "") and _parse_date_any(v) is None:
                        raise HTTPException(
                            status_code=400,
                            detail=f"{prefix}.Detail[{i}].{fld} không đúng định dạng ngày/giờ",
                        )

        if isinstance(master, dict):
            for fld in ("CreateDate", "StartManufactering", "EndDateManufactering"):
                v = master.get(fld)
                if v not in (None, "") and _parse_date_any(v) is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{prefix}.Master.{fld} không đúng định dạng ngày/giờ",
                    )

    if isinstance(pp, list):
        if not pp:
            raise HTTPException(status_code=400, detail="ProductionPlan không được rỗng")
        for i, item in enumerate(pp):
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail=f"ProductionPlan[{i}] phải là object")
            _validate_one_plan(item, f"ProductionPlan[{i}]")
        return

    if not isinstance(pp, dict):
        raise HTTPException(status_code=400, detail="ProductionPlan phải là object hoặc list object")

    _validate_one_plan(pp, "ProductionPlan")


def _normalize_detail_row_from_client(r: Dict[str, Any]) -> Dict[str, Any]:
    resource_ids_raw = _pick_first(r, ["ResourceIDs", "MaNguonLuc"])
    resource_ids_norm, resource_id_details = _normalize_resource_ids_any(resource_ids_raw)

    out: Dict[str, Any] = {
        "APK_MT2140": _pick_first(r, ["APK_Master", "APKMaster", "APK_MT2140"]),
        "IdTP": _pick_first(r, ["APK_MT2141", "IdTP", "TP_DinhMucID"]),
        "IdBTP": _pick_first(r, ["APK_MT2142", "IdBTP", "BTP_DinhMucID"]),
        "TP_DinhMucID": _pick_first(r, ["TP_DinhMucID", "APK_MT2141", "IdTP"]),
        "BTP_DinhMucID": _pick_first(r, ["BTP_DinhMucID", "APK_MT2142", "IdBTP"]),
        "DonHangID": _pick_first(r, ["OrderNo", "DonHangID"]),
        "SoDonHang": _pick_first(r, ["OrderNo", "SoDonHang"]),
        "SoChungTu": _pick_first(r, ["OrderNo", "SoChungTu"]),
        "KhachHang": _pick_first(r, ["CustomerName", "KhachHang"]),
        "LineKey": _pick_first(r, ["LineKey"]),
        "ThuTuSX": _pick_first(r, ["ProductionSequence", "ThuTuSX"]),
        "MaCongDoan": _pick_first(r, ["PhaseID", "MaCongDoan"]),
        "TenCongDoan": _pick_first(r, ["PhaseName", "TenCongDoan"]),
        "MaCongDoanLon": _pick_first(r, ["PhaseGroupID", "MaCongDoanLon", "PhaseGroupName", "TenBoPhan"]),
        "TenBoPhan": _pick_first(r, ["PhaseGroupName", "TenBoPhan"]),
        "MaNguonLuc": resource_ids_norm,
        "TenNguonLuc": _pick_first(r, ["ResourceNames", "TenNguonLuc"]),
        "MaTP": _pick_first(r, ["FinishedProductCode", "MaTP"]),
        "MaBTP": _pick_first(r, ["SemiFinishedProductCode", "MaBTP"]),
        "TenThanhPham": _pick_first(r, ["FinishedProductName", "TenThanhPham"]),
        "TenBanThanhPham": _pick_first(r, ["SemiFinishedProductName", "TenBanThanhPham"]),
        "TotalQty": _pick_first(r, ["TotalQuantity", "TotalQty", "SoLuongSX"]),
        "Start": _pick_first(r, ["StartDate", "Start", "StartDT"]),
        "End": _pick_first(r, ["EndDate", "End", "EndDT"]),
        "DueDT": _pick_first(r, ["DueDate", "DueDT"]),
        "DinhMucThoiGian": _pick_first(r, ["TimeLimit", "DinhMucThoiGian"]),
        "NangLucSanXuat": _pick_first(r, ["ProductionCapacity", "NangLucSanXuat", "CapacityPerDay"]),
        "NangLucTangCa": _pick_first(r, ["OvertimeCapacity", "NangLucTangCa", "OvertimeCapacityPerDay"]),
        "SoNhanSuBoPhan": _pick_first(r, ["ResourceWorker", "SoNhanSuBoPhan"]),
        "SoLuongMayToiDa": _pick_first(r, ["ResourceManchine", "ResourceMachine", "SoLuongMayToiDa"]),
        "SoLuongNguonLuc": _pick_first(r, ["SoLuongNguonLuc", "ResourceManchine", "ResourceMachine", "ResourceWorker"]),
        "IsBlockedByPredecessor": _pick_first(r, ["IsBlockedByPredecessor"]),
        "BlockedBy": _pick_first(r, ["BlockedBy"]),
        "BlockedReason": _pick_first(r, ["BlockedReason"]),
        "HasCapacity": _pick_first(r, ["HasCapacity"]),
        "IsOverCapacity": _pick_first(r, ["IsOverCapacity"]),
        "OverCapacityQty": _pick_first(r, ["OverCapacityQty"]),
        "IsOvertime": _pick_first(r, ["IsOvertime"]),
        "IsLate": _pick_first(r, ["IsLate"]),
        "LateQty": _pick_first(r, ["LateQty"]),
        "UnplannedQty": _pick_first(r, ["UnplannedQty"]),
    }

    if out.get("Start") in (None, ""):
        out["Start"] = _pick_first(r, ["StartDate", "Start", "StartDT"])
    if out.get("End") in (None, ""):
        out["End"] = _pick_first(r, ["EndDate", "End", "EndDT"])
    if out.get("DueDT") in (None, ""):
        out["DueDT"] = _pick_first(r, ["DueDate", "DueDT"])

    dq_raw = r.get("DailyQty") if isinstance(r.get("DailyQty"), list) else r.get("DailyQuantity")
    if isinstance(dq_raw, list):
        dq_out: List[Dict[str, Any]] = []
        planned_total = 0.0
        for item in dq_raw:
            if not isinstance(item, dict):
                continue
            pdate = _pick_first(item, ["ProductionDate", "Date", "Ngay"])
            qty = _pick_first(item, ["Quantity", "Qty", "SoLuong"])
            planned_total += _safe_float(qty)
            dq_out.append(
                {
                    "ProductionDate": _to_iso_date_or_raw(pdate),
                    "Quantity": qty,
                }
            )
        out["DailyQty"] = dq_out
        out["PlannedTotal"] = _safe_int(planned_total, 0)
        out["TotalDoneQty"] = _safe_int(planned_total, 0)

    if out.get("CapacityPerDay") in (None, ""):
        out["CapacityPerDay"] = out.get("NangLucSanXuat")
    if out.get("OvertimeCapacityPerDay") in (None, ""):
        out["OvertimeCapacityPerDay"] = out.get("NangLucTangCa")

    if out.get("StartDT") in (None, ""):
        out["StartDT"] = out.get("Start")
    if out.get("EndDT") in (None, ""):
        out["EndDT"] = out.get("End")

    if out.get("LineKey") in (None, ""):
        out["LineKey"] = "::".join(
            [
                str(_pick_first(r, ["OrderNo", "DonHangID"]) or ""),
                str(_pick_first(r, ["FinishedProductCode", "MaTP"]) or ""),
                str(_pick_first(r, ["SemiFinishedProductCode", "MaBTP"]) or ""),
            ]
        )

    if resource_id_details:
        out["ResourceIDDetails"] = resource_id_details

    for k, v in r.items():
        if k not in out and k not in (None, ""):
            out[k] = v

    return out


def _normalize_plan_like_to_calendar(plan_obj: Dict[str, Any]) -> Dict[str, Any]:
    master = plan_obj.get("Master") if isinstance(plan_obj.get("Master"), dict) else {}
    detail = plan_obj.get("Detail") if isinstance(plan_obj.get("Detail"), list) else []

    def _infer_plan_id_from_master(master_obj: Dict[str, Any]) -> Optional[int]:
        # Prefer explicit numeric-like ids first
        for k in ("KeHoachID", "PlanId", "planId", "ID", "Id"):
            pid = _safe_int(master_obj.get(k), 0)
            if pid > 0:
                return int(pid)

        # Try APK when it is numeric-like
        pid_apk = _safe_int(_pick_first(master_obj, ["APK"]), 0)
        if pid_apk > 0:
            return int(pid_apk)

        # Last fallback: parse trailing number from voucher code, e.g. KHSX/03/2026/001 -> 1
        voucher = str(_pick_first(master_obj, ["VoucherNo", "MaKeHoach"]) or "").strip()
        if voucher:
            m = re.search(r"(\d+)(?!.*\d)", voucher)
            if m:
                try:
                    v = int(m.group(1))
                    if v > 0:
                        return v
                except Exception:
                    pass
        return None

    inferred_pid = _infer_plan_id_from_master(master)

    header: Dict[str, Any] = {
        "KeHoachID": int(inferred_pid) if inferred_pid else None,
        "APK": _pick_first(master, ["APK"]),
        "PlanAPK": _pick_first(master, ["APK"]),
        "MaKeHoach": _pick_first(master, ["VoucherNo"]),
        "NgayTao": _pick_first(master, ["CreateDate"]),
        "TuNgay": _to_iso_date_or_raw(_pick_first(master, ["StartManufactering", "StartManufacturing"])),
        "DenNgay": _to_iso_date_or_raw(_pick_first(master, ["EndDateManufactering", "EndDateManufacturing"])),
        "TrangThai": _pick_first(master, ["Status"]),
    }

    rows: List[Dict[str, Any]] = []
    day_set: set[str] = set()
    for item in detail:
        if not isinstance(item, dict):
            continue
        nr = _normalize_detail_row_from_client(item)
        rows.append(nr)
        dq = item.get("DailyQty")
        if isinstance(dq, list):
            for d in dq:
                if not isinstance(d, dict):
                    continue
                ds = _to_iso_date_or_raw(d.get("ProductionDate"))
                if isinstance(ds, str) and ds:
                    day_set.add(ds[:10])

    voucher_no = str(_pick_first(master, ["VoucherNo", "MaKeHoach"]) or "").strip()
    if voucher_no:
        for r in rows:
            if not isinstance(r, dict):
                continue
            order_no = _pick_first(r, ["SoDonHang", "SoChungTu", "DonHangID", "OrderNo"])
            if order_no in (None, ""):
                cust = str(_pick_first(r, ["KhachHang", "CustomerName"]) or "").strip()
                fallback_order = f"{voucher_no}-{cust}" if cust else voucher_no
                r["SoDonHang"] = fallback_order
                r["SoChungTu"] = fallback_order
                r["DonHangID"] = fallback_order

            line_key = str(r.get("LineKey") or "")
            if not line_key or line_key.startswith("::"):
                r["LineKey"] = "::".join(
                    [
                        str(r.get("SoDonHang") or r.get("SoChungTu") or r.get("DonHangID") or ""),
                        str(_pick_first(r, ["MaTP", "FinishedProductCode"]) or ""),
                        str(_pick_first(r, ["MaBTP", "SemiFinishedProductCode"]) or ""),
                    ]
                )

    days = sorted(day_set)
    return {"Header": header, "Days": days, "Rows": rows}


def _normalize_ai_analysis_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    pp_raw = payload.get("ProductionPlan")
    if not isinstance(pp_raw, (dict, list)):
        return payload

    pp_main: Optional[Dict[str, Any]] = None
    pp_extra: List[Dict[str, Any]] = []
    if isinstance(pp_raw, dict):
        pp_main = pp_raw
    elif isinstance(pp_raw, list):
        valid_plans = [p for p in pp_raw if isinstance(p, dict)]
        if valid_plans:
            pp_main = valid_plans[0]
            pp_extra = valid_plans[1:]

    if not isinstance(pp_main, dict):
        return payload

    out: Dict[str, Any] = {}

    pid_in = _safe_int(payload.get("planId") or payload.get("KeHoachID"), 0)
    if pid_in > 0:
        out["planId"] = pid_in

    rules_raw = payload.get("rules") if isinstance(payload.get("rules"), dict) else None
    if rules_raw is None and isinstance(payload.get("Rules"), dict):
        rules_raw = payload.get("Rules")
    if rules_raw is None and isinstance(payload.get("RULES"), dict):
        rules_raw = payload.get("RULES")

    if isinstance(rules_raw, dict):
        rules_norm = dict(rules_raw)
        if rules_norm.get("top_n") is None and rules_norm.get("topN") is not None:
            rules_norm["top_n"] = rules_norm.get("topN")
        if rules_norm.get("top_n") is None and rules_norm.get("Top_n") is not None:
            rules_norm["top_n"] = rules_norm.get("Top_n")
        if rules_norm.get("top_n") is None and rules_norm.get("TopN") is not None:
            rules_norm["top_n"] = rules_norm.get("TopN")
        if rules_norm.get("deptExclusive") is None and rules_norm.get("DeptExclusive") is not None:
            rules_norm["deptExclusive"] = rules_norm.get("DeptExclusive")
        if rules_norm.get("deptExclusive") is None and rules_norm.get("dept_exclusive") is not None:
            rules_norm["deptExclusive"] = rules_norm.get("dept_exclusive")
        out["rules"] = rules_norm

    if isinstance(payload.get("constraints"), dict):
        out["constraints"] = payload.get("constraints")
    if isinstance(payload.get("hr_forecast"), dict):
        out["hr_forecast"] = payload.get("hr_forecast")
    if payload.get("horizon_start") not in (None, ""):
        out["horizon_start"] = payload.get("horizon_start")
    if payload.get("horizon_end") not in (None, ""):
        out["horizon_end"] = payload.get("horizon_end")

    main_cal = _normalize_plan_like_to_calendar(pp_main)
    out["calendar"] = main_cal

    pid = _safe_int(((main_cal.get("Header") or {}).get("KeHoachID")), 0)
    if pid > 0 and out.get("planId") in (None, 0):
        out["planId"] = pid

    # Keep planId stable even when Master.APK is GUID (non-numeric)
    if out.get("planId") in (None, 0):
        hdr = main_cal.get("Header") if isinstance(main_cal.get("Header"), dict) else {}
        pid_from_voucher = _safe_int(_pick_first(hdr, ["MaKeHoach"]), 0)
        if pid_from_voucher > 0:
            out["planId"] = int(pid_from_voucher)

    related = payload.get("RelatedProductionPlans")
    if related is None:
        related = payload.get("relatedProductionPlans")
    if related is None:
        related = payload.get("related_plans")
    if pp_extra:
        related_list = related if isinstance(related, list) else []
        related = list(related_list) + pp_extra
    others: List[Dict[str, Any]] = []
    if isinstance(related, list):
        for rp in related:
            if not isinstance(rp, dict):
                continue
            plan_like = rp.get("CurrentPlan") if isinstance(rp.get("CurrentPlan"), dict) else rp
            if not isinstance(plan_like, dict):
                continue
            cal = _normalize_plan_like_to_calendar(plan_like)
            if isinstance(cal.get("Rows"), list) and cal.get("Rows"):
                others.append(cal)
    if others:
        out["otherPlans"] = others

    return out


def _calendar_to_plan_like(calendar: Dict[str, Any]) -> Dict[str, Any]:
    header = calendar.get("Header") if isinstance(calendar.get("Header"), dict) else {}
    rows = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []

    master = {
        "APK": _pick_first(header, ["PlanAPK", "KeHoachID", "APK"]),
        "VoucherNo": _pick_first(header, ["MaKeHoach", "VoucherNo", "KeHoachCode"]),
        "CreateDate": _pick_first(header, ["NgayTao", "CreateDate"]),
        "StartManufactering": _pick_first(header, ["TuNgay", "StartManufactering", "StartDate"]),
        "EndDateManufactering": _pick_first(header, ["DenNgay", "EndDateManufactering", "EndDate"]),
        "Status": _pick_first(header, ["TrangThai", "Status"]),
    }

    detail: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue

        resource_ids: List[Dict[str, Any]] = []
        if isinstance(r.get("ResourceIDDetails"), list):
            for x in r.get("ResourceIDDetails"):
                if isinstance(x, dict) and _pick_first(x, ["ResourceID", "resource_id", "MaNguonLuc"]) not in (None, ""):
                    resource_ids.append(
                        {
                            "ResourceID": str(_pick_first(x, ["ResourceID", "resource_id", "MaNguonLuc"])),
                            "TimeResource": _safe_int(_pick_first(x, ["TimeResource", "setup_minutes", "Minutes"]), 0),
                        }
                    )
        elif r.get("MaNguonLuc") not in (None, ""):
            for rid in [x.strip() for x in str(r.get("MaNguonLuc") or "").split(",") if str(x).strip()]:
                resource_ids.append({"ResourceID": rid, "TimeResource": 0})

        # Fallback for labor rows where machine code is omitted.
        if not resource_ids:
            nguon_luc_text = str(_pick_first(r, ["NguonLucText", "TenNguonLuc"]) or "").strip().lower()
            if "nhân công" in nguon_luc_text or "nhan cong" in nguon_luc_text:
                resource_ids.append({"ResourceID": "M000", "TimeResource": 0})

        total_qty = _safe_int(
            _pick_first(r, ["TotalQty", "PlannedTotal", "SoLuongKeHoach", "TotalQuantity", "planned_qty"]),
            0,
        )
        time_limit = _safe_float(
            _pick_first(r, ["DinhMucPhutMoiSP", "DinhMucThoiGian", "TimeLimit"]),
            0.0,
        )
        if time_limit <= 0:
            # Keep solver stable for legacy rows with missing/zero định mức.
            time_limit = 1.0

        start_date = _pick_first(r, ["StartDT", "Start", "StartDate", "PlanWindowStart", "TuNgay"])
        end_date = _pick_first(r, ["EndDT", "End", "EndDate", "PlanWindowEnd", "DenNgay"])
        due_date = _pick_first(r, ["DueDT", "DueDate", "DenNgay"]) or _pick_first(
            header, ["DenNgay", "EndDateManufactering", "EndDate"]
        )

        resource_worker = _safe_int(_pick_first(r, ["NhanCong", "ResourceWorker", "SoNhanSuBoPhan"]), 0)
        resource_machine = _safe_int(
            _pick_first(r, ["SoMay", "ResourceManchine", "ResourceMachine", "SoLuongNguonLuc"]),
            len(resource_ids),
        )
        if resource_machine <= 0 and resource_ids:
            resource_machine = len(resource_ids)

        detail.append(
            {
                "OrderNo": _pick_first(r, ["SoDonHang", "DonHangID", "OrderNo"]),
                "CustomerName": _pick_first(r, ["KhachHang", "CustomerName"]),
                "ProductionSequence": _safe_int(_pick_first(r, ["ThuTuSX", "ProductionSequence"]), 0),
                "PhaseID": _pick_first(r, ["MaCongDoan", "PhaseID"]),
                "PhaseName": _pick_first(r, ["TenCongDoan", "PhaseName"]),
                "PhaseGroupName": _pick_first(r, ["TenBoPhan", "MaCongDoanLon", "PhaseGroupName"]),
                "ResourceIDs": resource_ids,
                "ResourceNames": _pick_first(r, ["NguonLucText", "TenNguonLuc", "ResourceNames"]),
                "FinishedProductCode": _pick_first(r, ["MaTP", "FinishedProductCode"]),
                "FinishedProductName": _pick_first(r, ["TenThanhPham", "FinishedProductName"]),
                "SemiFinishedProductCode": _pick_first(r, ["MaBTP", "SemiFinishedProductCode"]),
                "SemiFinishedProductName": _pick_first(r, ["TenBanThanhPham", "SemiFinishedProductName"]),
                "ResourceWorker": resource_worker,
                "ResourceMachine": resource_machine,
                "TimeLimit": time_limit,
                "TotalQuantity": total_qty,
                "StartDate": start_date,
                "EndDate": end_date,
                "DueDate": due_date,
                "ProductionCapacity": _safe_float(_pick_first(r, ["CapacityPerDay", "NangLucSanXuat", "ProductionCapacity"]), 0.0),
                "OvertimeCapacity": _safe_float(_pick_first(r, ["OvertimeCapacityPerDay", "NangLucTangCa", "OvertimeCapacity"]), 0.0),
                "DailyQty": r.get("DailyQty") if isinstance(r.get("DailyQty"), list) else [],
                "LateQty": _safe_int(_pick_first(r, ["LateQty"]), 0),
                "UnplannedQty": _safe_int(_pick_first(r, ["UnplannedQty"]), 0),
            }
        )

    return {"Master": master, "Detail": detail}


def _template_no_from_compact_option(opt: Dict[str, Any]) -> int:
    moves = opt.get("moves") if isinstance(opt.get("moves"), list) else []
    move_types = {str(m.get("type") or "") for m in moves if isinstance(m, dict)}
    if "REQUEST_DUE_DATE_EXTENSION" in move_types:
        return 6
    if "HIRE_OUTSOURCE_LABOR" in move_types:
        return 4
    if "INCREASE_OT_OR_ADD_SHIFT" in move_types:
        return 1
    if "MOVE_TO_ALTERNATE_MACHINE" in move_types or "SWAP_ORDER_ON_MACHINE" in move_types:
        return 2
    return 3


def _analysis_text_from_compact_payload(compact_payload: Dict[str, Any]) -> str:
    kpi = compact_payload.get("kpi_snapshot") if isinstance(compact_payload.get("kpi_snapshot"), dict) else {}
    hotspots = compact_payload.get("hotspots") if isinstance(compact_payload.get("hotspots"), dict) else {}
    errors = compact_payload.get("errors") if isinstance(compact_payload.get("errors"), dict) else {}
    business = compact_payload.get("business") if isinstance(compact_payload.get("business"), dict) else {}
    problem_rows = business.get("problem_rows") if isinstance(business.get("problem_rows"), list) else []

    def _fmt_code_name(code: Any, name: Any) -> str:
        c = str(code or "").strip()
        n = str(name or "").strip()
        if c and n:
            return f"{c}-{n}"
        return c or n or "không đủ dữ liệu"

    total_ops = _safe_int(kpi.get("total_operations"), 0)
    late_ops = _safe_int(kpi.get("late_ops_count"), 0)
    late_qty = _safe_int(kpi.get("total_late_qty"), 0)
    unplanned_qty = _safe_int(kpi.get("total_unplanned_qty"), 0)
    tardiness = _safe_int(kpi.get("total_tardiness_minutes"), 0)
    overtime_used = _safe_int(kpi.get("overtime_used_minutes"), 0)
    conflict_errors = _safe_int(errors.get("baseline_validation_errors_count"), 0)

    top_row: Dict[str, Any] = {}
    top_score = -1.0
    blocked_count = 0
    phase_group_load: Dict[str, float] = {}
    machine_load: Dict[str, float] = {}
    labor_conflict_score = 0.0

    for row in problem_rows:
        if not isinstance(row, dict):
            continue
        lq = _safe_float(row.get("late_qty"), 0.0)
        uq = _safe_float(row.get("unplanned_qty"), 0.0)
        score = lq + uq
        if score > top_score:
            top_score = score
            top_row = row

        dep = row.get("dependency") if isinstance(row.get("dependency"), dict) else {}
        if bool(dep.get("is_blocked_by_predecessor")):
            blocked_count += 1

        pg = str(row.get("phase_group") or "").strip()
        if pg:
            phase_group_load[pg] = phase_group_load.get(pg, 0.0) + score

        res = row.get("resource") if isinstance(row.get("resource"), dict) else {}
        res_code = str(res.get("code") or "").strip().upper()
        res_name = str(res.get("name") or "").strip().lower()
        is_labor = ("M000" in res_code) or ("LABOR" in res_code) or ("nhân công" in res_name) or ("nhan cong" in res_name)
        if is_labor:
            labor_conflict_score += score
            continue
        if res_code:
            key = _fmt_code_name(res_code, str(res.get("name") or "").strip())
            machine_load[key] = machine_load.get(key, 0.0) + score

    bottleneck_machines = hotspots.get("bottleneck_machines") if isinstance(hotspots.get("bottleneck_machines"), list) else []
    bottleneck_groups = hotspots.get("bottleneck_phase_groups") if isinstance(hotspots.get("bottleneck_phase_groups"), list) else []

    # Never render labor as machine in analysis text.
    bottleneck_machines = [
        str(x).strip()
        for x in bottleneck_machines
        if str(x).strip()
        and "LABOR" not in str(x).upper()
        and "NHÂN CÔNG" not in str(x).upper()
        and "NHAN CONG" not in str(x).upper()
        and "M000" not in str(x).upper()
    ]

    if not bottleneck_machines:
        bottleneck_machines = [k for k, _ in sorted(machine_load.items(), key=lambda x: x[1], reverse=True)[:3]]
    if not bottleneck_groups:
        bottleneck_groups = [k for k, _ in sorted(phase_group_load.items(), key=lambda x: x[1], reverse=True)[:3]]

    so_don_hang = str(top_row.get("so_don_hang") or "").strip()
    khach_hang = str(top_row.get("khach_hang") or "").strip()
    order_txt = f" của đơn hàng {so_don_hang}-{khach_hang}" if so_don_hang and khach_hang else ""
    btp_txt = _fmt_code_name(top_row.get("semi_finished_product_code"), top_row.get("semi_finished_product_name"))
    tp_txt = _fmt_code_name(top_row.get("finished_product_code"), top_row.get("finished_product_name"))
    phase_txt = _fmt_code_name(top_row.get("phase_id"), top_row.get("phase_name"))
    pg_txt = str(top_row.get("phase_group") or "").strip() or "không đủ dữ liệu"

    machine_txt = ", ".join([str(x) for x in bottleneck_machines[:3] if str(x).strip()]) or "không có"
    group_txt = ", ".join([str(x) for x in bottleneck_groups[:3] if str(x).strip()]) or "không có"

    conflict_clause = (
        f"Có {conflict_errors} lỗi xung đột/ràng buộc tải trong kế hoạch hiện tại"
        if conflict_errors > 0
        else "Chưa ghi nhận lỗi xung đột cứng trong kế hoạch hiện tại, nhưng vẫn có quá tải thực tế"
    )
    ot_clause = "chưa sử dụng tăng ca" if overtime_used <= 0 else f"đã sử dụng tăng ca {overtime_used} phút"

    resource_focus = f"điểm nghẽn tập trung tại máy {machine_txt}"
    if labor_conflict_score > 0:
        resource_focus += " và nguồn lực nhân công"
    resource_focus += f" và bộ phận {group_txt}"

    if late_qty > 0 or tardiness > 0:
        status_sentence = (
            f"Thành phẩm {tp_txt} (BTP {btp_txt}){order_txt} đang trễ tiến độ; "
            f"công đoạn ảnh hưởng lớn nhất là {phase_txt} thuộc bộ phận {pg_txt}. "
            f"Tổng công đoạn {total_ops}, trong đó trễ {late_ops}."
        )
    else:
        status_sentence = (
            f"Thành phẩm {tp_txt} (BTP {btp_txt}){order_txt} chưa ghi nhận sản lượng trễ tiến độ, "
            f"nhưng còn {unplanned_qty} sản phẩm chưa lên kế hoạch. "
            f"Công đoạn ảnh hưởng lớn nhất là {phase_txt} thuộc bộ phận {pg_txt}."
        )

    lines = [
        "Nội dung phân tích kế hoạch sản xuất:",
        f"1. Thành phẩm {tp_txt} (BTP {btp_txt}){order_txt} đang trễ tiến độ; công đoạn ảnh hưởng lớn nhất là {phase_txt} thuộc bộ phận {pg_txt}. Tổng công đoạn {total_ops}, trong đó trễ {late_ops}.",
        f"2. {conflict_clause}; {resource_focus}, làm tăng nguy cơ chồng chéo lịch ở các công đoạn liên quan.",
        f"3. Khối lượng chậm gồm {late_qty} sản phẩm trễ tiến độ và {unplanned_qty} sản phẩm chưa lên kế hoạch; tổng thời gian chậm {tardiness} phút. Hiện có {blocked_count} công đoạn bị chặn bởi quan hệ trước-sau.",
        f"4. Kế hoạch hiện tại {ot_clause}, cần ưu tiên gỡ công đoạn chặn đầu chuỗi, tái phân bổ máy thay thế theo mã máy, đồng thời cân nhắc điều chỉnh ngày giao nếu sau tối ưu vẫn còn thiếu sản lượng.",
    ]
    lines[1] = f"1. {status_sentence}"
    return "\n".join(lines)


def _format_expected_impact_line(expected: Dict[str, Any]) -> str:
    late_delta = _safe_int((expected or {}).get("late_ops_delta"), 0)
    tardy_delta = _safe_int((expected or {}).get("tardiness_minutes_delta"), 0)

    def _fmt_change(v: int, noun: str) -> str:
        if v < 0:
            return f"giảm {abs(v)} {noun}"
        if v > 0:
            return f"tăng {v} {noun}"
        return f"không đổi {noun}"

    p1 = _fmt_change(late_delta, "công đoạn trễ")
    p2 = _fmt_change(tardy_delta, "phút chậm tiến độ")
    return f"Tác động dự kiến: {p1}, {p2}."


def _extract_common_kpi_fields(kpi_like: Any) -> Dict[str, Any]:
    k = kpi_like if isinstance(kpi_like, dict) else {}
    total_operations = _safe_int(_pick_first(k, ["total_operations", "total_ops", "operations_total"]), 0)
    late_ops_count = _safe_int(_pick_first(k, ["late_ops_count", "late_operations", "late_ops"]), 0)
    total_late_qty = _safe_int(_pick_first(k, ["total_late_qty", "late_qty"]), 0)
    total_unplanned_qty = _safe_int(_pick_first(k, ["total_unplanned_qty", "unplanned_qty"]), 0)
    total_tardiness_minutes = _safe_int(_pick_first(k, ["total_tardiness_minutes", "tardiness_minutes", "tardiness"]), 0)
    feasible = bool(_pick_first(k, ["feasible", "is_feasible"]))
    return {
        "total_operations": total_operations,
        "late_ops_count": late_ops_count,
        "total_late_qty": total_late_qty,
        "total_unplanned_qty": total_unplanned_qty,
        "total_tardiness_minutes": total_tardiness_minutes,
        "feasible": feasible,
    }


def _apply_due_date_only_mode_if_needed(
    suggestions: List[Dict[str, Any]],
    ai_plans: Dict[str, Any],
    *,
    kpi_like: Any,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not suggestions:
        return suggestions, ai_plans

    # Keep all proposals but enforce business priority order: 1 -> 2 -> 3 -> 4 -> 5 -> 6.
    def _tpl_no(s: Dict[str, Any]) -> int:
        try:
            return int((((s or {}).get("meta") or {}).get("template_no") or 99))
        except Exception:
            return 99

    indexed = list(enumerate(suggestions))
    indexed.sort(key=lambda p: (_tpl_no(p[1]), p[0]))
    sorted_suggestions = [x for _, x in indexed]

    # Re-key suggestions sequentially and keep ai_plans consistent.
    old_to_new: Dict[str, str] = {}
    for i, s in enumerate(sorted_suggestions, start=1):
        old_key = str((s or {}).get("key") or "")
        new_key = f"PA{i}"
        if isinstance(s, dict):
            s["key"] = new_key
        if old_key:
            old_to_new[old_key] = new_key

    remapped_ai_plans: Dict[str, Any] = {}
    for old_key, val in (ai_plans or {}).items():
        nk = old_to_new.get(str(old_key))
        if nk:
            remapped_ai_plans[nk] = val

    return sorted_suggestions, remapped_ai_plans


def _run_optimizer_and_map_raw(
    *,
    payload: Dict[str, Any],
    calendar: Dict[str, Any],
    user_rules: Optional[Dict[str, Any]],
    plan_id: int,
) -> Dict[str, Any]:
    related = payload.get("otherPlans") if isinstance(payload.get("otherPlans"), list) else []

    input_data: Dict[str, Any] = {
        "Rules": {
            "Top_n": _safe_int((user_rules or {}).get("top_n"), 3) or 3,
            "DeptExclusive": bool((user_rules or {}).get("deptExclusive", True)),
            "StrictDailyQty": bool((user_rules or {}).get("strict_daily_qty", False)),
        },
        "ProductionPlan": [_calendar_to_plan_like(calendar)],
        "RelatedProductionPlans": [_calendar_to_plan_like(c) for c in related if isinstance(c, dict)],
    }

    optimizer = ProductionPlanningOptimizerService(
        top_problem_rows=max(3, min(20, _safe_int((user_rules or {}).get("top_problem_rows"), 8))),
        top_conflicts=max(3, min(10, _safe_int((user_rules or {}).get("top_conflicts"), 5))),
        top_options=max(1, min(10, _safe_int((user_rules or {}).get("top_n"), 3))),
    )
    try:
        opt_result = optimizer.run(input_data)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    compact_payload = opt_result.llm_payload if isinstance(opt_result.llm_payload, dict) else {}
    full_result = opt_result.full_result if isinstance(getattr(opt_result, "full_result", None), dict) else {}
    source_rows_raw = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
    optimizer_plan_rows = _build_optimizer_plan_rows_from_solver(full_result, source_rows_raw)
    end_extension_needed = _optimizer_plan_needs_end_extension(optimizer_plan_rows, source_rows_raw)
    due_extension_needed = _optimizer_plan_needs_due_extension(optimizer_plan_rows, source_rows_raw)

    llm_error: Optional[str] = None
    llm_out: Optional[Dict[str, Any]] = None
    llm_analysis_text = ""

    try:
        top_n = max(1, min(10, _safe_int((user_rules or {}).get("top_n"), 3)))
        propose_timeout_s = _safe_int(os.getenv("AI_PROPOSE_TIMEOUT_S"), 150)
        disable_llm = str(os.getenv("AI_ANALYSIS_DISABLE_LLM") or "").strip().lower() in {"1", "yes", "true", "on"}
        if disable_llm or propose_timeout_s <= 0:
            llm_error = "LLM skipped by AI_ANALYSIS_DISABLE_LLM/AI_PROPOSE_TIMEOUT_S"
            raise RuntimeError(llm_error)

        llm_input_payload = build_payload(
            errors=compact_payload.get("errors") if isinstance(compact_payload.get("errors"), dict) else {},
            ortools_options=compact_payload.get("ortools_options") if isinstance(compact_payload.get("ortools_options"), dict) else {},
            context={
                "kpi_snapshot": compact_payload.get("kpi_snapshot") if isinstance(compact_payload.get("kpi_snapshot"), dict) else {},
                "hotspots": compact_payload.get("hotspots") if isinstance(compact_payload.get("hotspots"), dict) else {},
                "business": compact_payload.get("business") if isinstance(compact_payload.get("business"), dict) else {},
            },
        )
        llm_out = call_gpt(
            payload=llm_input_payload,
            system_prompt=build_system_prompt(),
            user_prompt=build_user_prompt(llm_input_payload, top_n=top_n),
            timeout_s=propose_timeout_s,
            max_retries=_safe_int(os.getenv("AI_PROPOSE_MAX_RETRIES"), 1),
        )
        llm_analysis_text = _analysis_dict_to_text(llm_out.get("analysis")) if isinstance(llm_out, dict) else ""
    except OpenAIClientError as e:
        llm_error = str(e)
    except Exception as e:
        llm_error = str(e)

    options = (
        compact_payload.get("ortools_options", {}).get("options", [])
        if isinstance(compact_payload.get("ortools_options"), dict)
        else []
    )

    suggestions: List[Dict[str, Any]] = []
    ai_plans: Dict[str, Any] = {}

    llm_proposals = llm_out.get("proposals") if isinstance(llm_out, dict) and isinstance(llm_out.get("proposals"), list) else []
    if llm_proposals:
        business_context = _build_business_context_from_calendar(calendar)
        btp_metrics_map = _build_btp_metrics_map(business_context)
        machine_name_map, machine_name_to_code = _build_machine_maps_from_calendar(calendar)
        due_fallback = _best_due_date_from_calendar(calendar)
        llm_proposals = _sanitize_compact_llm_proposals(
            llm_proposals,
            machine_name_map=machine_name_map,
            machine_name_to_code=machine_name_to_code,
            due_fallback=due_fallback,
            btp_metrics_map=btp_metrics_map,
            has_labor_resource=_calendar_has_labor_resource(calendar),
        )
        mapped_suggestions, mapped_ai_plans = _map_options_to_ui(
            llm_proposals,
            machine_name_map=machine_name_map,
            current_plan_code=str((calendar.get("Header") or {}).get("MaKeHoach") or (calendar.get("Header") or {}).get("KeHoachID") or "") or None,
            btp_metrics_map=btp_metrics_map,
        )
        suggestions = mapped_suggestions
        ai_plans = mapped_ai_plans
    else:
        for idx, opt in enumerate(options):
            if not isinstance(opt, dict):
                continue
            key = str(opt.get("id") or f"PA{idx + 1}")
            template_no = _template_no_from_compact_option(opt)
            expected = opt.get("expected_impact") if isinstance(opt.get("expected_impact"), dict) else {}
            content_lines = [
                str(opt.get("reason_summary") or "").strip(),
                _format_expected_impact_line(expected),
            ]
            suggestions.append(
                {
                    "key": key,
                    "title": str(opt.get("title") or "").strip() or f"Phương án {idx + 1}",
                    "description": str(opt.get("reason_summary") or "").strip(),
                    "meta": {
                        "priority": f"P{_safe_int(opt.get('rank'), idx + 1)}",
                        "template_no": template_no,
                        "can_apply": True,
                        "content_lines": [x for x in content_lines if x],
                    },
                }
            )
            ai_plans[key] = {"rows": list(optimizer_plan_rows)}

    if optimizer_plan_rows:
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            key = str(s.get("key") or "").strip()
            if not key:
                continue
            # Execution rows must come from the deterministic optimizer. LLM rows
            # are allowed to influence wording only; they are not trusted as an
            # executable schedule because they can drift outside date/capacity rules.
            ai_plans[key] = {"rows": list(optimizer_plan_rows)}

    analysis_fallback = _analysis_text_from_compact_payload(compact_payload)
    analysis_clean = _sanitize_analysis_quality(
        llm_analysis_text or analysis_fallback,
        fallback=analysis_fallback,
        calendar=calendar,
    )

    suggestions, ai_plans = _apply_due_date_only_mode_if_needed(
        suggestions,
        ai_plans,
        kpi_like=compact_payload.get("kpi_snapshot"),
    )
    if not end_extension_needed:
        kept_suggestions: List[Dict[str, Any]] = []
        kept_keys: set[str] = set()
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}
            try:
                template_no = int(meta.get("template_no")) if meta.get("template_no") is not None else None
            except Exception:
                template_no = None
            if template_no == 6 and not (end_extension_needed or due_extension_needed):
                continue
            key = str(s.get("key") or "").strip()
            if key:
                kept_keys.add(key)
            kept_suggestions.append(s)
        suggestions = kept_suggestions
        ai_plans = {k: v for k, v in (ai_plans or {}).items() if str(k) in kept_keys}

    if optimizer_plan_rows:
        optimizer_suggestion = _build_optimizer_schedule_suggestion(
            optimizer_plan_rows,
            source_rows_raw,
            plan_code=str(_pick_first(calendar.get("Header") if isinstance(calendar.get("Header"), dict) else {}, ["MaKeHoach", "KeHoachID"]) or plan_id),
        )
        suggestions = [optimizer_suggestion]
        ai_plans = {optimizer_suggestion["key"]: {"rows": list(optimizer_plan_rows)}}

    full_result = opt_result.full_result if isinstance(getattr(opt_result, "full_result", None), dict) else {}
    unplanned_branch = full_result.get("unplanned_branch") if isinstance(full_result.get("unplanned_branch"), dict) else {}
    detail_updates = unplanned_branch.get("detail_updates") if isinstance(unplanned_branch.get("detail_updates"), list) else []

    source_rows_raw = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
    source_rows = _merge_unplanned_detail_updates_into_source_rows(source_rows_raw, detail_updates)

    header = calendar.get("Header") if isinstance(calendar.get("Header"), dict) else {}
    plan_to = _pick_first(header, ["DenNgay"])
    if plan_to in (None, "") and unplanned_branch.get("plan_end_date_manufacturing") not in (None, ""):
        plan_to = str(unplanned_branch.get("plan_end_date_manufacturing"))

    return {
        "plan": {
            "APK": _pick_first(header, ["APK", "PlanAPK", "PlanApk", "KeHoachAPK"]),
            "code": _pick_first(header, ["MaKeHoach", "KeHoachID"]) or plan_id,
            "from": _pick_first(header, ["TuNgay"]),
            "to": plan_to,
            "status": _pick_first(header, ["TrangThai"]),
        },
        "baseline_kpi": compact_payload.get("kpi_snapshot"),
        "suggestions": suggestions,
        "ai_plans": ai_plans,
        "source_rows": source_rows,
        "original_source_rows": source_rows_raw,
        "recomputed_plan": {
            "detected": bool(unplanned_branch.get("detected")),
            "end_date_manufacturing": unplanned_branch.get("plan_end_date_manufacturing"),
            "earliest_due_date_if_missed": unplanned_branch.get("earliest_due_date_if_missed"),
            "detail_updates": detail_updates,
        },
        "analysis": analysis_clean,
        "llm_error": llm_error,
        "debug": {
            "rules": user_rules or {},
            "optimizer_mode": "production_optimizer_v1",
            "llm_payload_compact": compact_payload,
            "llm_called": True,
            "llm_proposals_count": len(llm_proposals),
            "llm_io": llm_out if isinstance(llm_out, dict) else None,
            "optimizer_unplanned_branch": unplanned_branch,
        },
    }


def _merge_unplanned_detail_updates_into_source_rows(
    source_rows: Sequence[Dict[str, Any]],
    detail_updates: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge recomputed detail fields into original calendar rows for FE consumption.

    Matching strategy (best-effort):
    - parse operation id: <plan_no>::<order_no>::<sequence>::<btp>
    - map by (order_no, sequence, btp_code)
    """

    out: List[Dict[str, Any]] = []
    if not isinstance(source_rows, (list, tuple)):
        return out

    upd_map: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for u in detail_updates:
        if not isinstance(u, dict):
            continue
        op_id = str(u.get("OperationID") or "")
        parts = op_id.split("::")
        if len(parts) < 4:
            continue
        order_no = str(parts[1] or "").strip()
        seq = _safe_int(parts[2], 0)
        btp = str(parts[3] or "").strip().upper()
        if not order_no or seq <= 0 or not btp:
            continue
        upd_map[(order_no, seq, btp)] = u

    for r in source_rows:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        order_no = str(_pick_first(row, ["SoDonHang", "DonHangID"]) or "").strip()
        seq = _safe_int(_pick_first(row, ["ThuTuSX"]), 0)
        btp = str(_pick_first(row, ["MaBTP"]) or "").strip().upper()
        upd = upd_map.get((order_no, seq, btp))
        if isinstance(upd, dict):
            row["EndDT"] = upd.get("EndDate")
            row["EndDate"] = upd.get("EndDate")
            row["ProductionCapacity"] = _safe_int(upd.get("ProductionCapacity"), 0)
            row["OvertimeCapacity"] = _safe_int(upd.get("OvertimeCapacity"), 0)
            row["DailyQty"] = upd.get("DailyQty") if isinstance(upd.get("DailyQty"), list) else []
            row["LateQty"] = _safe_int(upd.get("LateQty"), 0)
            row["UnplannedQty"] = _safe_int(upd.get("UnplannedQty"), 0)
            if str(upd.get("resource_mode") or "") == "labor" and upd.get("ResourceWorker") not in (None, ""):
                row["ResourceWorker"] = _safe_int(upd.get("ResourceWorker"), 0)
                row["NhanCong"] = _safe_int(upd.get("ResourceWorker"), 0)
            if upd.get("recommended_resource_worker") not in (None, ""):
                row["recommended_resource_worker"] = _safe_int(upd.get("recommended_resource_worker"), 0)
            if upd.get("max_resource_worker") not in (None, ""):
                row["max_resource_worker"] = _safe_int(upd.get("max_resource_worker"), 0)
        out.append(row)

    return out


def _analysis_items_to_text(items: Sequence[str]) -> str:
    it = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    # exactly 4 lines
    while len(it) < 4:
        it.append("không đủ dữ liệu.")
    it = it[:4]
    it2: List[str] = []
    for s in it:
        s2 = s.strip()
        if s2 and not s2.endswith("."):
            s2 += "."
        it2.append(s2)
    return "\n".join(
        [
            "Nội dung phân tích kế hoạch sản xuất:",
            f"1. {it2[0]}",
            f"2. {it2[1]}",
            f"3. {it2[2]}",
            f"4. {it2[3]}",
        ]
    )


def _analysis_dict_to_text(analysis: Any) -> str:
    if not isinstance(analysis, dict):
        return ""
    items = analysis.get("items")
    if not isinstance(items, list):
        return ""
    cleaned = [str(x).strip() for x in items if x is not None and str(x).strip()]
    if not cleaned:
        return ""
    return _analysis_items_to_text(cleaned)


def _extract_resource_ids(raw: Any) -> List[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        out: List[str] = []
        for it in raw:
            if isinstance(it, dict):
                rid = _pick_first(it, ["ResourceID", "resource_id", "MaNguonLuc"])
                if rid not in (None, ""):
                    out.append(str(rid).strip())
            else:
                s = str(it or "").strip()
                if s:
                    out.append(s)
        return [x for x in out if x]
    return [x.strip() for x in str(raw).split(",") if x and str(x).strip()]


def _resource_id_details_from_any(raw: Any) -> List[Dict[str, Any]]:
    if raw in (None, ""):
        return []

    details: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                rid = _pick_first(item, ["ResourceID", "resource_id", "MaNguonLuc", "MachineID"])
                if rid in (None, ""):
                    continue
                details.append(
                    {
                        "ResourceID": str(rid).strip(),
                        "TimeResource": _safe_int(_pick_first(item, ["TimeResource", "time_resource", "Minutes"]), 0),
                    }
                )
            else:
                for rid in [x.strip() for x in str(item or "").split(",") if x.strip()]:
                    details.append({"ResourceID": rid, "TimeResource": 0})
        return details

    if isinstance(raw, dict):
        rid = _pick_first(raw, ["ResourceID", "resource_id", "MaNguonLuc", "MachineID"])
        if rid in (None, ""):
            return []
        return [
            {
                "ResourceID": str(rid).strip(),
                "TimeResource": _safe_int(_pick_first(raw, ["TimeResource", "time_resource", "Minutes"]), 0),
            }
        ]

    for rid in [x.strip() for x in str(raw).split(",") if x.strip()]:
        details.append({"ResourceID": rid, "TimeResource": 0})
    return details


def _source_row_to_update_shape(src: Dict[str, Any]) -> Dict[str, Any]:
    daily_raw = src.get("DailyQty") if isinstance(src.get("DailyQty"), list) else src.get("DailyQuantity")
    daily_qty: List[Dict[str, Any]] = []
    if isinstance(daily_raw, list):
        for d in daily_raw:
            if not isinstance(d, dict):
                continue
            pdate = _pick_first(d, ["ProductionDate", "Date", "Ngay"])
            qty = _pick_first(d, ["Quantity", "Qty", "SoLuong"])
            dt = _parse_date_any(pdate)
            pdate_fmt = dt.strftime("%d/%m/%Y") if dt else str(pdate or "")
            daily_qty.append({"ProductionDate": pdate_fmt, "Quantity": _safe_int(qty, 0)})

    rid_details = _resource_id_details_from_any(_pick_first(src, ["ResourceIDs", "MaNguonLuc"]))

    return {
        "APK_MT2140": _pick_first(src, ["APK_MT2140", "APK_Master", "APKMaster", "PlanAPK", "KeHoachAPK", "APK"]),
        "APK_MT2141": _pick_first(src, ["APK_MT2141", "IdTP", "TP_DinhMucID"]),
        "APK_MT2142": _pick_first(src, ["APK_MT2142", "IdBTP", "BTP_DinhMucID"]),
        "OrderNo": _pick_first(src, ["OrderNo", "SoDonHang", "SoChungTu"]),
        "FinishedProductCode": _pick_first(src, ["FinishedProductCode", "MaTP"]),
        "SemiFinishedProductCode": _pick_first(src, ["SemiFinishedProductCode", "MaBTP"]),
        "FinishedProductName": _pick_first(src, ["FinishedProductName", "TenThanhPham"]),
        "SemiFinishedProductName": _pick_first(src, ["SemiFinishedProductName", "TenBanThanhPham"]),
        "TotalQuantity": _pick_first(src, ["TotalQuantity", "TotalQty", "SoLuongSX"]),
        "ResourceIDs": rid_details,
        "ResourceIDDetails": rid_details,
        "ResourceWorker": _pick_first(src, ["ResourceWorker", "SoNhanSuBoPhan"]),
        "ResourceMachine": _pick_first(src, ["ResourceManchine", "ResourceMachine", "SoLuongMayToiDa"]),
        "StartDate": _pick_first(src, ["StartDate", "Start", "StartDT"]),
        "EndDate": _pick_first(src, ["EndDate", "End", "EndDT"]),
        "DueDate": _pick_first(src, ["DueDate", "DueDT"]),
        "DailyQty": daily_qty,
    }


def _build_rule_based_suggestions(source_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(source_rows, list) or not source_rows:
        return []

    def _sum_daily(dq: Any) -> int:
        if not isinstance(dq, list):
            return 0
        return int(sum(_safe_float((x or {}).get("Quantity")) for x in dq if isinstance(x, dict)))

    def _parse_daily_date(d: Dict[str, Any]) -> Optional[date]:
        dt = _parse_date_any((d or {}).get("ProductionDate"))
        return dt.date() if dt else None

    def _fmt_d(d0: date) -> str:
        return d0.strftime("%d/%m/%Y")

    metrics: List[Dict[str, Any]] = []
    for r in source_rows:
        if not isinstance(r, dict):
            continue
        total_qty = _safe_int(_pick_first(r, ["TotalQuantity", "TotalQty", "SoLuongSX"]), 0)
        late_qty = _safe_int(_pick_first(r, ["LateQty"]), 0)
        unplanned_qty = _safe_int(_pick_first(r, ["UnplannedQty"]), 0)
        daily = r.get("DailyQty") if isinstance(r.get("DailyQty"), list) else []
        done_qty = _sum_daily(daily)
        cap_regular = _safe_float(_pick_first(r, ["ProductionCapacity", "NangLucSanXuat", "CapacityPerDay"]))
        cap_ot = _safe_float(_pick_first(r, ["OvertimeCapacity", "NangLucTangCa", "OvertimeCapacityPerDay"]))
        shortage = max(0, late_qty + unplanned_qty)
        if shortage <= 0 and total_qty > done_qty:
            shortage = max(0, total_qty - done_qty)
        metrics.append(
            {
                "row": r,
                "total_qty": total_qty,
                "done_qty": done_qty,
                "late_qty": late_qty,
                "unplanned_qty": unplanned_qty,
                "shortage": shortage,
                "cap_regular": cap_regular,
                "cap_ot": cap_ot,
            }
        )

    if not metrics:
        return []

    primary = sorted(metrics, key=lambda x: (x.get("shortage") or 0), reverse=True)[0]
    src = primary["row"]
    base_upd = _source_row_to_update_shape(src)

    order_no = str(base_upd.get("OrderNo") or "")
    btp_name = str(base_upd.get("SemiFinishedProductName") or "")
    tp_name = str(base_upd.get("FinishedProductName") or "")
    phase_name = str(_pick_first(src, ["PhaseName", "TenCongDoan"]) or "công đoạn")
    rid_list = _extract_resource_ids(_pick_first(src, ["ResourceIDs", "MaNguonLuc"]))
    is_labor_btp = any(str(x).upper() == "M000" for x in rid_list)

    shortage = int(primary["shortage"])
    cap_regular = float(primary["cap_regular"])
    cap_ot = float(primary["cap_ot"])
    total_qty = int(primary["total_qty"])
    done_qty = int(primary["done_qty"])
    late_qty = int(primary["late_qty"])
    unplanned_qty = int(primary["unplanned_qty"])

    # ---- Build option updates ----
    upd1 = dict(base_upd)
    dq1 = [dict(x) for x in (base_upd.get("DailyQty") or []) if isinstance(x, dict)]
    rem = max(0, shortage)
    if dq1 and rem > 0:
        for i in range(len(dq1) - 1, -1, -1):
            if rem <= 0:
                break
            add = min(rem, max(0, int(cap_ot)))
            dq1[i]["Quantity"] = _safe_int(dq1[i].get("Quantity"), 0) + int(add)
            rem -= int(add)
    upd1["DailyQty"] = dq1

    upd2 = dict(base_upd)
    upd2["ResourceWorker"] = _safe_int(base_upd.get("ResourceWorker"), 0) + max(1, int(ceil(max(1, shortage) / max(1.0, cap_regular))))

    upd3 = dict(base_upd)
    upd3["ResourceWorker"] = _safe_int(base_upd.get("ResourceWorker"), 0) + max(1, int(ceil(max(1, shortage) / max(1.0, cap_regular))))

    upd4 = dict(base_upd)
    due_dt = _parse_date_any(base_upd.get("DueDate"))
    start_dt = _parse_date_any(base_upd.get("StartDate"))
    ws = start_dt.date() if start_dt else (due_dt.date() if due_dt else date.today())
    we = due_dt.date() if due_dt else ws
    sunday = ws
    found_sun = False
    cur = ws
    while cur <= we:
        if cur.weekday() == 6:
            sunday = cur
            found_sun = True
            break
        cur += timedelta(days=1)
    if not found_sun:
        while sunday.weekday() != 6:
            sunday += timedelta(days=1)
    dq4 = [dict(x) for x in (base_upd.get("DailyQty") or []) if isinstance(x, dict)]
    dq4.append({"ProductionDate": _fmt_d(sunday), "Quantity": max(0, int(min(max(1.0, cap_regular), max(0, shortage))))})
    upd4["DailyQty"] = dq4
    upd4["EndDate"] = f"{sunday.isoformat()}T23:59:00"

    upd5 = dict(base_upd)
    eff = max(1.0, cap_regular + cap_ot)
    ext_days = int(max(1, ceil(max(1, shortage) / eff)))
    if due_dt:
        new_due = due_dt.date() + timedelta(days=ext_days)
        upd5["DueDate"] = f"{new_due.isoformat()}T23:59:00"

    rids_txt = ", ".join(rid_list) if rid_list else str(base_upd.get("ResourceIDs") or "")

    # ---- Build final 5 suggestions ----
    return [
        {
            "key": "PA1",
            "title": "Tăng ca để tăng sản lượng",
            "description": (
                f"+ BTP {btp_name} ({order_no}) áp dụng tăng ca theo năng lực tăng ca/ngày = {int(cap_ot)}; "
                f"năng lực giờ hành chính/ngày = {int(cap_regular)}."
            ),
            "priority": "P1",
            "template_no": 1,
            "can_apply": True,
            "content_lines": [
                f"+ Năng lực giờ hành chính tối đa/ngày: {int(cap_regular)} (ProductionCapacity).",
                f"+ Năng lực tăng ca tối đa/ngày: {int(cap_ot)} (OvertimeCapacity).",
                (
                    f"+ Đề xuất: {'tăng ca nhân sự' if is_labor_btp else 'bổ sung giờ chạy máy'} cho công đoạn {phase_name}, "
                    f"nguồn lực {rids_txt}."
                ),
            ],
            "updates": [upd1],
        },
        {
            "key": "PA2",
            "title": "Điều nhân sự từ công đoạn khác",
            "description": f"+ Điều chuyển nhân sự hỗ trợ công đoạn {phase_name} để xử lý thiếu hụt {shortage}.",
            "priority": "P2",
            "template_no": 2,
            "can_apply": False,
            "content_lines": [
                f"+ Tổng đã làm của kế hoạch: {done_qty} = tổng Quantity trong DailyQty.",
                f"+ Tổng bị trễ: {late_qty} (LateQty); chưa lên kế hoạch: {unplanned_qty} (UnplannedQty).",
                f"+ Điều chuyển thêm nhân sự để bù thiếu: {shortage}.",
            ],
            "updates": [upd2],
        },
        {
            "key": "PA3",
            "title": "Bổ sung nhân sự thuê ngoài",
            "description": f"+ Thuê ngoài bổ sung cho {phase_name} để hoàn thành phần thiếu {shortage}.",
            "priority": "P3",
            "template_no": 3,
            "can_apply": False,
            "content_lines": [
                f"+ Tổng cần làm: {total_qty}; tổng đã làm: {done_qty}; thiếu hụt cần bù: {shortage}.",
                "+ Bổ sung nhân sự thuê ngoài để tăng sản lượng giờ hành chính.",
            ],
            "updates": [upd3],
        },
        {
            "key": "PA4",
            "title": "Làm vào ngày nghỉ hàng tuần",
            "description": f"+ Mở thêm ngày Chủ nhật để sản xuất bù phần thiếu cho {btp_name}.",
            "priority": "P4",
            "template_no": 4,
            "can_apply": False,
            "content_lines": [
                f"+ Bổ sung sản xuất ngày nghỉ theo năng lực giờ hành chính/ngày: {int(cap_regular)}.",
                f"+ Ngày Chủ nhật đề xuất: {_fmt_d(sunday)}.",
            ],
            "updates": [upd4],
        },
        {
            "key": "PA5",
            "title": "Điều chỉnh lại ngày giao hàng",
            "description": "+ Điều chỉnh DueDate khi vẫn thiếu sản lượng sau các phương án tăng năng lực.",
            "priority": "P5",
            "template_no": 5,
            "can_apply": False,
            "content_lines": [
                f"+ Thiếu hụt hiện tại: {shortage} = LateQty ({late_qty}) + UnplannedQty ({unplanned_qty}).",
                f"+ Gia hạn DueDate dựa trên năng lực tối đa/ngày: {int(cap_regular + cap_ot)}.",
            ],
            "updates": [upd5],
        },
    ]


def _find_source_for_update(update: Dict[str, Any], source_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(update, dict):
        return {}

    u_order = str(_pick_first(update, ["OrderNo", "SoDonHang", "SoChungTu"]) or "").strip()
    u_btp = str(_pick_first(update, ["SemiFinishedProductCode", "MaBTP"]) or "").strip()
    u_tp = str(_pick_first(update, ["FinishedProductCode", "MaTP"]) or "").strip()
    u_apk1 = str(_pick_first(update, ["APK_MT2141", "IdTP", "TP_DinhMucID"]) or "").strip()
    u_apk2 = str(_pick_first(update, ["APK_MT2142", "IdBTP", "BTP_DinhMucID"]) or "").strip()

    best: Dict[str, Any] = {}
    best_score = -1
    for src in source_rows or []:
        if not isinstance(src, dict):
            continue
        score = 0
        s_order = str(_pick_first(src, ["OrderNo", "SoDonHang", "SoChungTu", "DonHangID"]) or "").strip()
        s_btp = str(_pick_first(src, ["SemiFinishedProductCode", "MaBTP"]) or "").strip()
        s_tp = str(_pick_first(src, ["FinishedProductCode", "MaTP"]) or "").strip()
        s_apk1 = str(_pick_first(src, ["APK_MT2141", "IdTP", "TP_DinhMucID"]) or "").strip()
        s_apk2 = str(_pick_first(src, ["APK_MT2142", "IdBTP", "BTP_DinhMucID"]) or "").strip()

        if u_order and s_order and u_order == s_order:
            score += 3
        if u_btp and s_btp and u_btp == s_btp:
            score += 5
        if u_tp and s_tp and u_tp == s_tp:
            score += 2
        if u_apk1 and s_apk1 and u_apk1 == s_apk1:
            score += 3
        if u_apk2 and s_apk2 and u_apk2 == s_apk2:
            score += 4

        if score > best_score:
            best = src
            best_score = score

    return best if best_score >= 5 else {}


def _find_source_for_operation_id(operation_id: Any, source_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    parts = str(operation_id or "").split("::")
    if len(parts) < 4:
        return {}
    order_no = str(parts[1] or "").strip()
    seq = _safe_int(parts[2], -1)
    btp_code = str(parts[3] or "").strip()

    for row in source_rows or []:
        if not isinstance(row, dict):
            continue
        row_order = str(_pick_first(row, ["OrderNo", "SoDonHang", "SoChungTu"]) or "").strip()
        row_seq = _safe_int(_pick_first(row, ["ProductionSequence", "ThuTuSX"]), -2)
        row_btp = str(_pick_first(row, ["SemiFinishedProductCode", "MaBTP", "IdBTP"]) or "").strip()
        if row_order == order_no and row_seq == seq and row_btp == btp_code:
            return row
    return {}


def _build_optimizer_plan_rows_from_solver(full_result: Dict[str, Any], source_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    solver = full_result.get("solver") if isinstance(full_result.get("solver"), dict) else {}
    scheduled = solver.get("scheduled_operations") if isinstance(solver.get("scheduled_operations"), list) else []
    rows: List[Dict[str, Any]] = []

    for so in scheduled:
        if not isinstance(so, dict):
            continue
        src = _find_source_for_operation_id(so.get("operation_id"), source_rows)
        if not src:
            continue

        product = _pick_first(src, ["SemiFinishedProductName", "FinishedProductName", "TenBanThanhPham", "TenThanhPham"])
        step = _pick_first(src, ["PhaseName", "TenCongDoan"])
        assigned = str(so.get("assigned_resource") or "").strip()
        resource = "M000" if assigned.upper() in ("", "LABOR") else assigned

        daily_qty = so.get("daily_qty") if isinstance(so.get("daily_qty"), list) else []
        if daily_qty:
            for d in daily_qty:
                if not isinstance(d, dict):
                    continue
                day = _pick_first(d, ["ProductionDate", "Date", "Ngay"])
                qty = _safe_int(_pick_first(d, ["Quantity", "Qty", "SoLuong"]), 0)
                if day and qty > 0:
                    rows.append(
                        {
                            "operation_id": so.get("operation_id"),
                            "day": str(day)[:10],
                            "product": product,
                            "step": step,
                            "qty": qty,
                            "resource": resource,
                        }
                    )
            continue

        st = _parse_date_any(so.get("start_dt"))
        if st is not None:
            rows.append(
                {
                    "day": st.date().isoformat(),
                    "operation_id": so.get("operation_id"),
                    "product": product,
                    "step": step,
                    "qty": _safe_int(_pick_first(src, ["TotalQuantity", "TotalQty", "SoLuongSX"]), 0),
                    "resource": resource,
                }
            )

    return rows


def _optimizer_plan_needs_due_extension(plan_rows: Sequence[Dict[str, Any]], source_rows: Sequence[Dict[str, Any]]) -> bool:
    if not isinstance(plan_rows, Sequence) or not plan_rows:
        return False

    fallback_due_dates = [
        _parse_date_any(_pick_first(src, ["DueDate", "DueDT"]))
        for src in (source_rows or [])
        if isinstance(src, dict)
    ]
    fallback_due_dates = [d for d in fallback_due_dates if d is not None]
    fallback_due = max(fallback_due_dates) if fallback_due_dates else None

    for row in plan_rows:
        if not isinstance(row, dict):
            continue
        day = _parse_date_any(row.get("day"))
        if day is None:
            continue

        src = _find_source_for_operation_id(row.get("operation_id"), source_rows)
        due = _parse_date_any(_pick_first(src, ["DueDate", "DueDT"])) if src else fallback_due
        if due is not None and day.date() > due.date():
            return True

    return False


def _optimizer_plan_needs_end_extension(plan_rows: Sequence[Dict[str, Any]], source_rows: Sequence[Dict[str, Any]]) -> bool:
    if not isinstance(plan_rows, Sequence) or not plan_rows:
        return False

    fallback_end_dates = [
        _parse_date_any(_pick_first(src, ["EndDate", "End", "EndDT"]))
        for src in (source_rows or [])
        if isinstance(src, dict)
    ]
    fallback_end_dates = [d for d in fallback_end_dates if d is not None]
    fallback_end = max(fallback_end_dates) if fallback_end_dates else None

    for row in plan_rows:
        if not isinstance(row, dict):
            continue
        day = _parse_date_any(row.get("day"))
        if day is None:
            continue

        src = _find_source_for_operation_id(row.get("operation_id"), source_rows)
        end_dt = _parse_date_any(_pick_first(src, ["EndDate", "End", "EndDT"])) if src else fallback_end
        if end_dt is not None and day.date() > end_dt.date():
            return True

    return False


def _build_optimizer_schedule_suggestion(
    plan_rows: Sequence[Dict[str, Any]],
    source_rows: Sequence[Dict[str, Any]],
    *,
    plan_code: str,
) -> Dict[str, Any]:
    days = sorted({str(r.get("day") or "")[:10] for r in (plan_rows or []) if isinstance(r, dict) and str(r.get("day") or "").strip()})
    min_day = days[0] if days else ""
    max_day = days[-1] if days else ""

    source_due_dates = [
        _parse_date_any(_pick_first(src, ["DueDate", "DueDT"]))
        for src in (source_rows or [])
        if isinstance(src, dict)
    ]
    source_due_dates = [d for d in source_due_dates if d is not None]
    due_dt = max(source_due_dates) if source_due_dates else None
    due_day = due_dt.date().isoformat() if due_dt is not None else ""

    source_end_dates = [
        _parse_date_any(_pick_first(src, ["EndDate", "End", "EndDT"]))
        for src in (source_rows or [])
        if isinstance(src, dict)
    ]
    source_end_dates = [d for d in source_end_dates if d is not None]
    source_end_dt = max(source_end_dates) if source_end_dates else None
    source_end_day = source_end_dt.date().isoformat() if source_end_dt is not None else ""
    max_dt = _parse_date_any(max_day)
    end_extension_needed = bool(max_dt is not None and source_end_dt is not None and max_dt.date() > source_end_dt.date())
    due_extension_needed = bool(max_dt is not None and due_dt is not None and max_dt.date() > due_dt.date())
    proposed_due_day = (max_dt.date() + timedelta(days=1)).isoformat() if due_extension_needed and max_dt is not None else due_day

    products = {
        str(r.get("product") or "").strip()
        for r in (plan_rows or [])
        if isinstance(r, dict) and str(r.get("product") or "").strip()
    }
    resources = {
        str(r.get("resource") or "").strip()
        for r in (plan_rows or [])
        if isinstance(r, dict) and str(r.get("resource") or "").strip()
    }
    template_no = 6 if (end_extension_needed or due_extension_needed) else 1
    key = "PA6" if template_no == 6 else "PA1"
    title_out = (
        "Dieu chinh ngay giao hang theo lich san xuat kha thi"
        if due_extension_needed and not end_extension_needed
        else "Dieu chinh EndDate ke hoach san xuat theo lich kha thi"
        if template_no == 6
        else "Xep lai lich san xuat theo rang buoc nguon luc va thu tu cong doan"
    )

    content_lines = [
        f"+ StartDate giu nguyen theo du lieu goc; EndDate hien tai {source_end_day}; EndDate kha thi {max_day}; DueDate de xuat {proposed_due_day}.",
        f"+ Xếp lại lịch thực thi cho kế hoạch {plan_code} theo ràng buộc thứ tự công đoạn, công đoạn lớn và nguồn lực.",
        f"+ Thời gian sản xuất đề xuất: từ ngày {min_day} đến ngày {max_day}.",
        f"+ Hạn giao hàng giữ nguyên: {due_day}; phương án không cần gia hạn ngày giao hàng.",
        f"+ Tổng số dòng sản xuất được bố trí lại: {len(products) or len(plan_rows or [])}.",
        f"+ Nguồn lực được dùng trong lịch: {', '.join(sorted(resources)[:12])}.",
    ]

    resource_text = ", ".join(sorted(resources)[:12])
    if template_no == 6:
        due_line = (
            f"+ Lịch mới vượt hạn giao hàng hiện tại {due_day}, nên cần điều chỉnh ngày giao hàng sang {proposed_due_day}."
            if due_extension_needed
            else f"+ Lịch mới vẫn hoàn thành trước hạn giao hàng {due_day}, nên chưa cần điều chỉnh ngày giao hàng."
        )
        content_lines = [
            "+ Phương án này sắp xếp lại lịch sản xuất khả thi sau khi tối ưu nguồn lực, thứ tự công đoạn và các kế hoạch liên quan.",
            f"+ Theo lịch khả thi, sản xuất dự kiến hoàn thành vào ngày {max_day}; kế hoạch hiện tại đang kết thúc vào ngày {source_end_day}.",
            due_line,
            f"+ Tổng số dòng sản xuất được sắp xếp lại: {len(products) or len(plan_rows or [])}.",
            f"+ Nguồn lực được sử dụng trong lịch: {resource_text}.",
        ]
    else:
        content_lines = [
            f"+ Phương án này sắp xếp lại lịch thực thi cho kế hoạch {plan_code} theo ràng buộc thứ tự công đoạn, công đoạn lớn và nguồn lực.",
            f"+ Lịch sản xuất khả thi được bố trí từ ngày {min_day} đến ngày {max_day}.",
            f"+ Lịch mới vẫn nằm trong thời gian kết thúc sản xuất hiện tại và không cần điều chỉnh ngày giao hàng {due_day}.",
            f"+ Tổng số dòng sản xuất được sắp xếp lại: {len(products) or len(plan_rows or [])}.",
            f"+ Nguồn lực được sử dụng trong lịch: {resource_text}.",
        ]

    return {
        "key": key,
        "title": "Xếp lại lịch sản xuất theo ràng buộc nguồn lực và thứ tự công đoạn",
        "title": title_out,
        "description": " ".join(content_lines),
        "meta": {
            "priority": "P1",
            "template_no": template_no,
            "can_apply": True,
            "content_lines": content_lines,
            "source": "optimizer_schedule",
            "source_end_date": source_end_day,
            "scheduled_start_date": min_day,
            "scheduled_end_date": max_day,
            "proposed_due_date": proposed_due_day,
        },
    }


def _daily_qty_normalized(daily_raw: Any) -> List[Tuple[str, int]]:
    out: List[Tuple[str, int]] = []
    if not isinstance(daily_raw, list):
        return out
    for item in daily_raw:
        if not isinstance(item, dict):
            continue
        dt = _parse_date_any(_pick_first(item, ["ProductionDate", "Date", "Ngay"]))
        qty = _safe_int(_pick_first(item, ["Quantity", "Qty", "SoLuong"]), 0)
        key = dt.date().isoformat() if dt is not None else str(_pick_first(item, ["ProductionDate", "Date", "Ngay"]) or "").strip()
        out.append((key, qty))
    return sorted(out)


def _resources_are_labor_only(raw: Any) -> bool:
    codes = [str(x or "").strip().upper() for x in _extract_resource_ids(raw)]
    return bool(codes) and all(x == "M000" for x in codes)


def _resources_have_machine(raw: Any) -> bool:
    codes = [str(x or "").strip().upper() for x in _extract_resource_ids(raw)]
    return any(x and x != "M000" for x in codes)


def _resource_items_for_capacity(primary: Any, fallback: Any = None) -> List[Dict[str, Any]]:
    raw = primary if primary not in (None, "") else fallback
    out: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                rid = _pick_first(item, ["ResourceID", "resource_id", "MaNguonLuc"])
                if rid in (None, ""):
                    continue
                out.append(
                    {
                        "resource_id": str(rid).strip(),
                        "setup_minutes": _safe_int(_pick_first(item, ["TimeResource", "setup_minutes"]), 0),
                    }
                )
            elif item not in (None, ""):
                out.append({"resource_id": str(item).strip(), "setup_minutes": 0})
    elif raw not in (None, ""):
        for rid in [x.strip() for x in str(raw).split(",") if x.strip()]:
            out.append({"resource_id": rid, "setup_minutes": 0})
    return out


def _recompute_daily_capacity_for_update(update: Dict[str, Any], source: Dict[str, Any]) -> int:
    time_limit = _safe_float(_pick_first(update, ["TimeLimit", "DinhMucPhutMoiSP"]), 0.0)
    if time_limit <= 0 and source:
        time_limit = _safe_float(_pick_first(source, ["TimeLimit", "DinhMucPhutMoiSP"]), 0.0)
    if time_limit <= 0:
        return 0

    worker = _safe_int(_pick_first(update, ["ResourceWorker", "SoNhanSuBoPhan"]), 0)
    if worker <= 0 and source:
        worker = _safe_int(_pick_first(source, ["ResourceWorker", "SoNhanSuBoPhan"]), 0)

    resources = _resource_items_for_capacity(update.get("ResourceIDs"), _pick_first(source, ["ResourceIDs", "MaNguonLuc"]) if source else None)
    machine_resources = [r for r in resources if str(r.get("resource_id") or "").strip().upper() != "M000"]

    if machine_resources:
        regular_total = sum(max(0, 8 * 60 - max(0, _safe_int(r.get("setup_minutes"), 0))) for r in machine_resources)
        overtime_total = sum(4 * 60 for _ in machine_resources)
        return int(regular_total / time_limit) + int(overtime_total / time_limit)

    if worker > 0:
        return int((worker * 8 * 60) / time_limit) + int((worker * 4 * 60) / time_limit)

    return 0


def _update_changed_from_source(update: Dict[str, Any], source: Dict[str, Any]) -> bool:
    if not isinstance(update, dict) or not isinstance(source, dict) or not source:
        return True

    scalar_pairs = [
        ("StartDate", ["StartDate", "Start", "StartDT"]),
        ("EndDate", ["EndDate", "End", "EndDT"]),
        ("DueDate", ["DueDate", "DueDT"]),
        ("ResourceWorker", ["ResourceWorker", "SoNhanSuBoPhan"]),
        ("ResourceMachine", ["ResourceManchine", "ResourceMachine", "SoLuongMayToiDa"]),
        ("TotalQuantity", ["TotalQuantity", "TotalQty", "SoLuongSX"]),
    ]
    for u_key, s_keys in scalar_pairs:
        if u_key == "ResourceMachine":
            uv = _pick_first(update, ["ResourceMachine", "ResourceManchine"])
        else:
            uv = update.get(u_key)
        sv = _pick_first(source, s_keys)
        if u_key in ("StartDate", "EndDate", "DueDate"):
            ud = _parse_date_any(uv)
            sd = _parse_date_any(sv)
            if ud or sd:
                if (ud.isoformat() if ud else "") != (sd.isoformat() if sd else ""):
                    return True
                continue
        if str(uv or "").strip() != str(sv or "").strip():
            return True

    if sorted([x.upper() for x in _extract_resource_ids(update.get("ResourceIDs"))]) != sorted(
        [x.upper() for x in _extract_resource_ids(_pick_first(source, ["ResourceIDs", "MaNguonLuc"]))]
    ):
        return True

    src_daily = source.get("DailyQty") if isinstance(source.get("DailyQty"), list) else source.get("DailyQuantity")
    return _daily_qty_normalized(update.get("DailyQty")) != _daily_qty_normalized(src_daily)


def _validate_updates_for_auto_apply(
    updates: List[Dict[str, Any]],
    source_rows: List[Dict[str, Any]],
    *,
    template_no: Any = None,
) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    changed_count = 0

    if not isinstance(updates, list) or not updates:
        return {"ok": False, "changed_count": 0, "errors": ["NO_UPDATE_ROWS"], "warnings": [], "template_no": template_no}

    machine_intervals: List[Tuple[str, datetime, datetime, str]] = []
    try:
        template_no_int = int(template_no) if template_no is not None else None
    except Exception:
        template_no_int = None
    end_extension_needed = False
    due_extension_needed = False
    for idx, upd in enumerate(updates):
        if not isinstance(upd, dict):
            errors.append(f"UPDATE_{idx}_NOT_OBJECT")
            continue

        src = _find_source_for_update(upd, source_rows)
        if _update_changed_from_source(upd, src):
            changed_count += 1

        st = _parse_date_any(upd.get("StartDate"))
        en = _parse_date_any(upd.get("EndDate"))
        due = _parse_date_any(upd.get("DueDate"))
        if st is None:
            errors.append(f"UPDATE_{idx}_MISSING_START")
        if en is None:
            errors.append(f"UPDATE_{idx}_MISSING_END")
        if st is not None and en is not None and en < st:
            errors.append(f"UPDATE_{idx}_END_BEFORE_START")

        daily = upd.get("DailyQty") if isinstance(upd.get("DailyQty"), list) else []
        total_qty = _safe_int(upd.get("TotalQuantity"), 0)
        daily_capacity = _recompute_daily_capacity_for_update(upd, src)
        if daily_capacity <= 0:
            daily_capacity = _safe_int(_pick_first(upd, ["ProductionCapacity", "NangLucSanXuat"]), 0) + _safe_int(
                _pick_first(upd, ["OvertimeCapacity", "NangLucTangCa"]),
                0,
            )
        if daily_capacity <= 0 and src:
            daily_capacity = _safe_int(_pick_first(src, ["ProductionCapacity", "NangLucSanXuat"]), 0) + _safe_int(
                _pick_first(src, ["OvertimeCapacity", "NangLucTangCa"]),
                0,
            )
        daily_sum = 0
        for d_idx, d in enumerate(daily):
            if not isinstance(d, dict):
                errors.append(f"UPDATE_{idx}_DAILY_{d_idx}_NOT_OBJECT")
                continue
            q = _safe_int(_pick_first(d, ["Quantity", "Qty", "SoLuong"]), 0)
            if q < 0:
                errors.append(f"UPDATE_{idx}_DAILY_{d_idx}_NEGATIVE_QTY")
            if daily_capacity > 0 and q > daily_capacity:
                errors.append(f"UPDATE_{idx}_DAILY_{d_idx}_EXCEEDS_DAILY_CAPACITY")
            daily_sum += max(0, q)
            pdt = _parse_date_any(_pick_first(d, ["ProductionDate", "Date", "Ngay"]))
            if pdt is None:
                errors.append(f"UPDATE_{idx}_DAILY_{d_idx}_BAD_DATE")
            elif st is not None and en is not None and not (st.date() <= pdt.date() <= en.date()):
                errors.append(f"UPDATE_{idx}_DAILY_{d_idx}_OUTSIDE_WINDOW")
        if total_qty > 0 and daily_sum > total_qty:
            errors.append(f"UPDATE_{idx}_DAILY_EXCEEDS_TOTAL")

        update_resource = upd.get("ResourceIDs")
        source_resource = _pick_first(src, ["ResourceIDs", "MaNguonLuc"]) if src else None
        if src and _resources_are_labor_only(source_resource) and _resources_have_machine(update_resource):
            errors.append(f"UPDATE_{idx}_LABOR_TO_MACHINE_RESOURCE_MISMATCH")
        if src and _resources_have_machine(source_resource) and _resources_are_labor_only(update_resource):
            errors.append(f"UPDATE_{idx}_MACHINE_TO_LABOR_RESOURCE_MISMATCH")

        if st is not None and en is not None:
            row_label = str(_pick_first(upd, ["SemiFinishedProductCode", "OrderNo"]) or idx)
            row_intervals: List[Tuple[datetime, datetime]] = []
            for d in daily:
                if not isinstance(d, dict):
                    continue
                pdt = _parse_date_any(_pick_first(d, ["ProductionDate", "Date", "Ngay"]))
                if pdt is None:
                    continue
                day_start = datetime.combine(pdt.date(), datetime.min.time())
                day_end = datetime.combine(pdt.date(), datetime.max.time())
                row_intervals.append((day_start, day_end))
            if not row_intervals:
                row_intervals = [(st, en)]
            for rid in _extract_resource_ids(update_resource):
                rid_u = str(rid or "").strip().upper()
                if not rid_u or rid_u == "M000":
                    continue
                for row_st, row_en in row_intervals:
                    for old_rid, old_st, old_en, old_label in machine_intervals:
                        if rid_u != old_rid:
                            continue
                        if row_st <= old_en and row_en >= old_st:
                            errors.append(f"MACHINE_OVERLAP_{rid_u}_{old_label}_{row_label}")
                            break
                    machine_intervals.append((rid_u, row_st, row_en, row_label))

        if due is not None and en is not None and en > due:
            errors.append(f"UPDATE_{idx}_ENDS_AFTER_DUE")
        source_end = _parse_date_any(_pick_first(src, ["EndDate", "End", "EndDT"])) if src else None
        if template_no_int == 6 and source_end is not None and en is not None and en.date() > source_end.date():
            end_extension_needed = True
        source_due = _parse_date_any(_pick_first(src, ["DueDate", "DueDT"])) if src else None
        if template_no_int == 6 and source_due is not None and en is not None and en.date() > source_due.date():
            due_extension_needed = True
            expected_due = en.date() + timedelta(days=1)
            if due is None or due.date() != expected_due:
                errors.append(f"UPDATE_{idx}_DUE_DATE_MUST_BE_END_PLUS_ONE")

    if changed_count <= 0:
        errors.append("NO_EFFECTIVE_CHANGE")
    if template_no_int == 6 and not (end_extension_needed or due_extension_needed):
        errors.append("DATE_EXTENSION_NOT_NEEDED")

    return {
        "ok": not errors,
        "changed_count": changed_count,
        "errors": errors,
        "warnings": warnings,
        "template_no": template_no,
    }


def _to_client_ai_analysis_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "overall_analysis": {"plan": None, "analysis": ""},
            "suggestions": [],
        }

    plan_info = raw.get("plan") if isinstance(raw.get("plan"), dict) else None
    analysis_text = str(raw.get("analysis") or "").strip()

    suggestions_raw = raw.get("suggestions") if isinstance(raw.get("suggestions"), list) else []
    ai_plans_raw = raw.get("ai_plans") if isinstance(raw.get("ai_plans"), dict) else {}
    source_rows = raw.get("source_rows") if isinstance(raw.get("source_rows"), list) else []
    validation_source_rows = raw.get("original_source_rows") if isinstance(raw.get("original_source_rows"), list) else source_rows

    suggestions: List[Dict[str, Any]] = []

    for s in suggestions_raw:
        if not isinstance(s, dict):
            continue

        key = str(s.get("key") or "").strip()
        title = str(s.get("title") or "").strip()
        description = str(s.get("description") or "").strip()
        meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}

        pa_obj = ai_plans_raw.get(key) if isinstance(ai_plans_raw.get(key), dict) else {}
        pa_rows = pa_obj.get("rows") if isinstance(pa_obj.get("rows"), list) else []
        updates = _build_updates_for_option(pa_rows, source_rows, meta=meta, title=title, description=description)
        apk_master = None
        for u in updates:
            if isinstance(u, dict) and u.get("APK_MT2140") not in (None, ""):
                apk_master = u.get("APK_MT2140")
                break
        if apk_master in (None, ""):
            for src in source_rows:
                if isinstance(src, dict):
                    apk_master = _pick_first(src, ["APK_MT2140", "APK_Master", "APKMaster", "PlanAPK", "KeHoachAPK", "APK"])
                    if apk_master not in (None, ""):
                        break
        apply_validation = _validate_updates_for_auto_apply(
            updates,
            validation_source_rows,
            template_no=meta.get("template_no"),
        )
        can_apply = bool(meta.get("can_apply")) and bool(apply_validation.get("ok"))

        suggestions.append(
            {
                "key": key,
                "APK_MT2140": apk_master,
                "title": title,
                "description": description,
                "priority": meta.get("priority"),
                "template_no": meta.get("template_no"),
                "can_apply": can_apply,
                "content_lines": meta.get("content_lines") if isinstance(meta.get("content_lines"), list) else [],
                "updates": updates,
            }
        )

    if isinstance(plan_info, dict):
        plan_summary = {
            "APK": _pick_first(plan_info, ["APK", "PlanAPK", "PlanApk", "KeHoachAPK"]),
            "code": plan_info.get("code"),
            "from": plan_info.get("from"),
            "to": plan_info.get("to"),
            "status": plan_info.get("status"),
            "suggestions": suggestions,
        }
    else:
        plan_summary = None

    return {
        "overall_analysis": {
            "plan": {
                "count": 1 if plan_summary else 0,
                "plans": [plan_summary] if plan_summary else [],
            },
            "analysis": analysis_text,
        },
    }


def _build_updates_for_option(
    plan_rows: List[Dict[str, Any]],
    source_rows: List[Dict[str, Any]],
    *,
    meta: Optional[Dict[str, Any]] = None,
    title: str = "",
    description: str = "",
) -> List[Dict[str, Any]]:
    """Build per-option update rows in client ProductionPlan.Detail-like shape."""

    def _extract_order_no_from_operation_id(operation_id: Any) -> Optional[str]:
        op = str(operation_id or "")
        if not op:
            return None

        for part in op.split("::"):
            if part.startswith("DH"):
                token = part[2:]
                if token.startswith("S"):
                    token = unquote(token[1:])
                return token or None
            if part.startswith("LK"):
                line_key = unquote(part[2:])
                if line_key:
                    chunks = line_key.split("::")
                    if chunks and chunks[0].strip():
                        return chunks[0].strip()

        return None

    def _fmt_output_date(v: Any) -> Optional[str]:
        dt = _parse_date_any(v)
        if dt is None:
            return None
        return dt.date().isoformat()

    def _normalize_source_row_to_update(src: Dict[str, Any]) -> Dict[str, Any]:
        daily_raw = src.get("DailyQty") if isinstance(src.get("DailyQty"), list) else src.get("DailyQuantity")
        daily_qty: List[Dict[str, Any]] = []
        if isinstance(daily_raw, list):
            # Case A: DailyQty is already a list of objects {ProductionDate, Quantity}
            has_any_dict = any(isinstance(x, dict) for x in daily_raw)
            if has_any_dict:
                for d in daily_raw:
                    if not isinstance(d, dict):
                        continue
                    pdate = _pick_first(d, ["ProductionDate", "Date", "Ngay"])
                    qty = _pick_first(d, ["Quantity", "Qty", "SoLuong"])
                    daily_qty.append(
                        {
                            "ProductionDate": _fmt_output_date(pdate) or str(pdate or ""),
                            "Quantity": _safe_int(qty, 0),
                        }
                    )
            else:
                # Case B: systemData.Rows often carries DailyQty as a numeric array aligned to a date window.
                st0 = _parse_date_any(_pick_first(src, ["PlanWindowStart", "StartDate", "Start", "StartDT", "TuNgay"]))
                if st0 is not None:
                    base_day = st0.date()
                    for idx, q in enumerate(daily_raw):
                        qi = _safe_int(q, 0)
                        if qi <= 0:
                            continue
                        pdate = (base_day + timedelta(days=int(idx))).isoformat()
                        daily_qty.append({"ProductionDate": pdate, "Quantity": int(qi)})

        # Fallback when DailyQty is missing: derive minimal daily allocation from date window and total quantity.
        if not daily_qty:
            st = _parse_date_any(_pick_first(src, ["StartDate", "Start", "StartDT", "PlanWindowStart", "TuNgay"]))
            en = _parse_date_any(_pick_first(src, ["EndDate", "End", "EndDT", "PlanWindowEnd", "DenNgay", "DueDate", "DueDT"]))
            total_guess = _safe_int(_pick_first(src, ["PlannedTotal", "TotalDoneQty", "TotalQuantity", "TotalQty", "SoLuongSX"]), 0)
            if st is not None and en is not None and total_guess > 0:
                d0 = st.date()
                d1 = en.date()
                if d1 < d0:
                    d1 = d0
                days_count = (d1 - d0).days + 1
                days_count = max(1, int(days_count))
                base = int(total_guess) // days_count
                rem = int(total_guess) - base * days_count
                cur = d0
                for i in range(days_count):
                    q = base + (1 if i < rem else 0)
                    if int(q) <= 0:
                        cur = cur + timedelta(days=1)
                        continue
                    daily_qty.append({"ProductionDate": cur.isoformat(), "Quantity": int(q)})
                    cur = cur + timedelta(days=1)

        resource_id = _pick_first(src, ["ResourceIDs", "MaNguonLuc"])
        resource_id_s = str(resource_id or "").strip().upper()
        is_machine = bool(re.match(r"^M\d{3,4}$", resource_id_s)) and resource_id_s != "M000"
        machine_qty = _pick_first(src, ["ResourceManchine", "ResourceMachine", "SoLuongMayToiDa"])
        if machine_qty in (None, "") and is_machine:
            machine_qty = _pick_first(src, ["SoLuongNguonLuc"])  # often 1 for machine rows

        return {
            "APK_MT2140": _pick_first(src, ["APK_MT2140", "APK_Master", "APKMaster", "PlanAPK", "KeHoachAPK", "APK"]),
            "APK_MT2141": _pick_first(src, ["APK_MT2141", "IdTP", "TP_DinhMucID"]),
            "APK_MT2142": _pick_first(src, ["APK_MT2142", "IdBTP", "BTP_DinhMucID"]),
            "OrderNo": _pick_first(src, ["OrderNo", "SoDonHang", "SoChungTu"]),
            "FinishedProductCode": _pick_first(src, ["FinishedProductCode", "MaTP"]),
            "SemiFinishedProductCode": _pick_first(src, ["SemiFinishedProductCode", "MaBTP"]),
            "FinishedProductName": _pick_first(src, ["FinishedProductName", "TenThanhPham"]),
            "SemiFinishedProductName": _pick_first(src, ["SemiFinishedProductName", "TenBanThanhPham"]),
            "TotalQuantity": _pick_first(src, ["TotalQuantity", "TotalQty", "SoLuongSX"]),
            "ResourceIDs": _pick_first(src, ["ResourceIDs", "MaNguonLuc"]),
            "ResourceWorker": _pick_first(src, ["ResourceWorker", "SoNhanSuBoPhan"]),
            "ResourceMachine": machine_qty,
            "StartDate": _pick_first(src, ["StartDate", "Start", "StartDT"]),
            "EndDate": _pick_first(src, ["EndDate", "End", "EndDT"]),
            "DueDate": _pick_first(src, ["DueDate", "DueDT"]),
            "DailyQty": daily_qty,
        }

    def _apply_option_adjustments(update: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(update, dict):
            return update

        out_u = dict(update)
        tno = None
        try:
            tno = int((meta or {}).get("template_no")) if (meta or {}).get("template_no") is not None else None
        except Exception:
            tno = None

        text_blob = "\n".join(
            [
                str(title or ""),
                str(description or ""),
                "\n".join([str(x) for x in ((meta or {}).get("content_lines") or []) if str(x).strip()]),
            ]
        )

        machines = re.findall(r"\bM\d{3}\b", text_blob, flags=re.IGNORECASE)
        machine_codes = []
        seen_m: set[str] = set()
        for m in machines:
            mm = str(m).upper()
            if mm in seen_m:
                continue
            seen_m.add(mm)
            machine_codes.append(mm)

        if tno == 2 and machine_codes:
            original_resource_ids = [str(x or "").strip().upper() for x in _extract_resource_ids(out_u.get("ResourceIDs"))]
            original_is_machine = any(x and x != "M000" for x in original_resource_ids)
            if not original_is_machine:
                return out_u
            existing_details = _resource_id_details_from_any(out_u.get("ResourceIDDetails") or out_u.get("ResourceIDs"))
            replaced_details: List[Dict[str, Any]] = []
            for idx, code in enumerate(machine_codes):
                time_resource = 0
                if idx < len(existing_details):
                    time_resource = _safe_int(existing_details[idx].get("TimeResource"), 0)
                elif existing_details:
                    time_resource = _safe_int(existing_details[0].get("TimeResource"), 0)
                replaced_details.append({"ResourceID": code, "TimeResource": time_resource})
            out_u["ResourceIDs"] = replaced_details
            out_u["ResourceIDDetails"] = replaced_details
            out_u["ResourceMachine"] = max(1, len(machine_codes))
            out_u.pop("ResourceManchine", None)

        if tno == 6:
            proposed_due = (meta or {}).get("proposed_due_date")
            proposed_due_dt = _parse_date_any(proposed_due)
            if proposed_due_dt is not None:
                out_u["DueDate"] = f"{proposed_due_dt.date().isoformat()}T23:59:00"

        if tno == 4:
            m_staff = re.search(r"(?i)số\s+lượng\s+nhân\s+sự[^\d]*([0-9]+)", text_blob)
            if m_staff:
                add_staff = _safe_int(m_staff.group(1), 0)
                cur_staff = _safe_int(out_u.get("ResourceWorker"), 0)
                if add_staff > 0 and cur_staff >= 0:
                    out_u["ResourceWorker"] = int(cur_staff + add_staff)

        m_window = re.search(r"(?i)từ\s+ngày\s+(\d{4}-\d{2}-\d{2})\s+đến\s+ngày\s+(\d{4}-\d{2}-\d{2})", text_blob)
        has_executable_daily = bool(out_u.get("DailyQty") if isinstance(out_u.get("DailyQty"), list) else [])
        if m_window and not has_executable_daily:
            out_u["StartDate"] = f"{m_window.group(1)}T00:00:00"
            out_u["EndDate"] = f"{m_window.group(2)}T23:59:00"

        return out_u

    if (not isinstance(plan_rows, list) or not plan_rows) and isinstance(source_rows, list):
        return [_apply_option_adjustments(_normalize_source_row_to_update(sr)) for sr in source_rows if isinstance(sr, dict)]

    def _norm_text(v: Any) -> str:
        return str(v or "").strip().lower()

    def _pick_src(product: Any, step: Any, operation_id: Any = None) -> Dict[str, Any]:
        by_operation = _find_source_for_operation_id(operation_id, source_rows)
        if by_operation:
            return by_operation
        p = _norm_text(product)
        s = _norm_text(step)
        for sr in source_rows:
            if not isinstance(sr, dict):
                continue
            p1 = _norm_text(_pick_first(sr, ["SemiFinishedProductName", "FinishedProductName", "TenBanThanhPham", "TenThanhPham"]))
            s1 = _norm_text(_pick_first(sr, ["PhaseName", "TenCongDoan"]))
            if p and p1 and p == p1 and (not s or s == s1):
                return sr
        for sr in source_rows:
            if not isinstance(sr, dict):
                continue
            p1 = _norm_text(_pick_first(sr, ["SemiFinishedProductName", "FinishedProductName", "TenBanThanhPham", "TenThanhPham"]))
            if p and p1 and p == p1:
                return sr
        return {}

    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for pr in plan_rows:
        if not isinstance(pr, dict):
            continue
        day = str(pr.get("day") or "").strip()
        operation_id = str(pr.get("operation_id") or "").strip()
        product = str(pr.get("product") or "").strip()
        step = str(pr.get("step") or "").strip()
        if not day or not product:
            continue

        k = (operation_id, product, step)
        g = grouped.setdefault(
            k,
            {
                "operation_id": operation_id,
                "product": product,
                "step": step,
                "daily": {},
                "resources": set(),
            },
        )
        try:
            qty = float(pr.get("qty") or 0.0)
        except Exception:
            qty = 0.0
        g["daily"][day] = float(g["daily"].get(day) or 0.0) + qty

        res = str(pr.get("resource") or "").strip()
        if res:
            g["resources"].add(res)

    out: List[Dict[str, Any]] = []
    for _, g in grouped.items():
        product = g.get("product")
        step = g.get("step")
        src = _pick_src(product, step, g.get("operation_id"))

        daily_map = g.get("daily") if isinstance(g.get("daily"), dict) else {}
        day_keys = sorted([str(d) for d in daily_map.keys()])
        daily_qty = []
        total_qty_from_daily = 0.0
        for d in day_keys:
            qf = float(daily_map.get(d) or 0.0)
            q = _safe_int(qf, 0)
            total_qty_from_daily += qf
            d_fmt = _fmt_output_date(d) or str(d)
            daily_qty.append({"ProductionDate": d_fmt, "Quantity": q})

        start_dt = None
        end_dt = None
        source_start_dt = _pick_first(src, ["StartDate", "Start", "StartDT"])
        if day_keys:
            start_dt = source_start_dt or (day_keys[0] + "T00:00:00")
            end_dt = (day_keys[-1] + "T23:59:00")

        if not start_dt:
            start_dt = source_start_dt
        if not end_dt:
            end_dt = _pick_first(src, ["EndDate", "End", "EndDT"])

        due_dt = _pick_first(src, ["DueDate", "DueDT"])
        resource_ids = _resource_id_details_from_any(sorted([str(x).strip() for x in (g.get("resources") or set()) if str(x).strip()]))
        if not resource_ids:
            resource_ids = _resource_id_details_from_any(_pick_first(src, ["ResourceIDs", "MaNguonLuc"]))

        total_qty = _pick_first(src, ["TotalQuantity", "TotalQty", "SoLuongSX"])
        if total_qty in (None, ""):
            total_qty = _safe_int(total_qty_from_daily, 0)

        order_no = _pick_first(src, ["OrderNo", "SoDonHang", "SoChungTu"])
        if order_no in (None, ""):
            order_no = _extract_order_no_from_operation_id(g.get("operation_id"))

        out.append(
            {
                "APK_MT2140": _pick_first(src, ["APK_MT2140", "APK_Master", "APKMaster", "PlanAPK", "KeHoachAPK", "APK"]),
                "APK_MT2141": _pick_first(src, ["APK_MT2141", "IdTP", "TP_DinhMucID"]),
                "APK_MT2142": _pick_first(src, ["APK_MT2142", "IdBTP", "BTP_DinhMucID"]),
                "OrderNo": order_no,
                "FinishedProductCode": _pick_first(src, ["FinishedProductCode", "MaTP"]),
                "SemiFinishedProductCode": _pick_first(src, ["SemiFinishedProductCode", "MaBTP"]),
                "FinishedProductName": _pick_first(src, ["FinishedProductName", "TenThanhPham"]),
                "SemiFinishedProductName": _pick_first(src, ["SemiFinishedProductName", "TenBanThanhPham"]),
                "TotalQuantity": total_qty,
                "ResourceIDs": resource_ids,
                "ResourceWorker": _pick_first(src, ["ResourceWorker", "SoNhanSuBoPhan"]),
                "ResourceMachine": _pick_first(src, ["ResourceManchine", "ResourceMachine", "SoLuongMayToiDa"]),
                "StartDate": start_dt,
                "EndDate": end_dt,
                "DueDate": due_dt,
                "DailyQty": daily_qty,
            }
        )

    if out:
        return [_apply_option_adjustments(u) for u in out]
    if isinstance(source_rows, list):
        return [_apply_option_adjustments(_normalize_source_row_to_update(sr)) for sr in source_rows if isinstance(sr, dict)]
    return []


def _compact_llm_io_for_client(llm_io: Any, *, include_raw: bool = False) -> Any:
    if include_raw:
        return llm_io
    if not isinstance(llm_io, dict):
        return llm_io

    parsed = llm_io.get("parsed") if isinstance(llm_io.get("parsed"), dict) else {}
    analysis_items = parsed.get("analysis", {}).get("items") if isinstance(parsed.get("analysis"), dict) else []
    proposals = parsed.get("proposals") if isinstance(parsed.get("proposals"), list) else []

    return {
        "model": llm_io.get("model"),
        "parsed_summary": {
            "analysis_items_count": len(analysis_items) if isinstance(analysis_items, list) else 0,
            "proposal_count": len(proposals),
            "proposal_ids": [str((p or {}).get("id")) for p in proposals if isinstance(p, dict) and (p or {}).get("id")],
        },
    }


def _sanitize_analysis_text(text: str) -> str:
    # Remove obvious technical phrases that should not be shown to business users.
    try:
        s = str(text or "")
        s = re.sub(r"\bOR-?Tools\b", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\bCP-?SAT\b", "", s, flags=re.IGNORECASE)

        # Remove internal validator/constraint codes that sometimes leak from LLM.
        # Keep this allow-list explicit to avoid accidentally stripping business codes like BP003.
        codes = [
            "MACHINE_OVERLAP",
            "MACHINE_BUSY_CONFLICT",
            "DEPT_OVERLAP",
            "DEPT_BUSY_CONFLICT",
            "PRECEDENCE_VIOLATION",
            "RESOURCE_OVERLAP",
            "OUTSIDE_HORIZON",
            "DUE_DATE_VIOLATION",
            "INSUFFICIENT_QTY_BEFORE_DUE",
        ]
        code_pat = r"\b(?:" + "|".join([re.escape(c) for c in codes]) + r")\b"
        s = re.sub(code_pat, "", s)

        # Remove common technical words
        s = re.sub(r"\bvalidator\b", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\bconstraint(s)?\b", "", s, flags=re.IGNORECASE)

        # Preserve newlines (the business format is line-based)
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
        s = "\n".join([ln for ln in lines if ln])

        s = s.replace(" .", ".").replace(" ,", ",")
        s = s.replace("\n ", "\n")
        return s.strip()
    except Exception:
        return text


def _build_machine_name_map(db: Session, business_context: Any) -> Dict[str, str]:
    """Build a stable map: machine code -> machine name.

    Do NOT rely on positional pairing between comma-separated MachineCodes/MachineText
    from SQL DISTINCT lists (ordering can differ). Instead, query DM_NguonLuc by code.
    """

    out: Dict[str, str] = {}

    codes: List[str] = []
    try:
        prs = (business_context or {}).get("problem_rows") if isinstance(business_context, dict) else []
        if isinstance(prs, list):
            for pr in prs:
                if not isinstance(pr, dict):
                    continue
                res = pr.get("resource") or {}
                if not isinstance(res, dict):
                    continue
                for c in _split_csv(res.get("code")):
                    c2 = str(c or "").strip().upper()
                    if not c2 or c2 == "M000":
                        continue
                    codes.append(c2)
    except Exception:
        codes = []

    codes = sorted(set(codes))
    if not codes:
        return out

    try:
        rows = db.query(DM_NguonLuc).filter(DM_NguonLuc.MaNguonLuc.in_(codes)).all()
        for r in rows:
            try:
                code = str(r.MaNguonLuc or "").strip().upper()
                name = str(r.TenNguonLuc or "").strip()
                if code and name:
                    out[code] = name
            except Exception:
                continue
    except Exception:
        # Fallback to best-effort parsing from business_context (may be imperfect)
        try:
            prs = (business_context or {}).get("problem_rows") if isinstance(business_context, dict) else []
            for pr in prs if isinstance(prs, list) else []:
                if not isinstance(pr, dict):
                    continue
                res = pr.get("resource") or {}
                if not isinstance(res, dict):
                    continue
                # If a single code has a single name, accept it.
                c_list = _split_csv(res.get("code"))
                n_list = _split_csv(res.get("name"))
                if len(c_list) == 1 and len(n_list) == 1:
                    c = str(c_list[0] or "").strip().upper()
                    n = str(n_list[0] or "").strip()
                    if c and n and c != "M000":
                        out.setdefault(c, n)
        except Exception:
            pass

    return out


def _build_btp_metrics_map(business_context: Any) -> Dict[str, Dict[str, Any]]:
    """Build a lookup map for deterministic fill-ins from problem_rows.

    Keys include BTP code and BTP name (uppercased) when available.
    """

    out: Dict[str, Dict[str, Any]] = {}
    try:
        prs = (business_context or {}).get("problem_rows") if isinstance(business_context, dict) else []
        if not isinstance(prs, list):
            return out
        for pr in prs:
            if not isinstance(pr, dict):
                continue

            so_don_hang = str(pr.get("so_don_hang") or "").strip()
            khach_hang = str(pr.get("khach_hang") or "").strip()
            order_ref = None
            # Display format required by business UI: (SO_CT - KHACH_HANG)
            if so_don_hang and khach_hang:
                order_ref = f"({so_don_hang} - {khach_hang})"
            elif so_don_hang:
                order_ref = f"({so_don_hang})"

            btp = pr.get("btp") or {}
            if not isinstance(btp, dict):
                btp = {}
            code = str(btp.get("code") or "").strip()
            name = str(btp.get("name") or "").strip()
            cap = pr.get("capacity") or {}
            wf = pr.get("workforce") or {}
            dates = pr.get("dates") or {}
            qty = pr.get("qty") or {}

            norm_min = cap.get("norm_time_min")
            try:
                norm_min_f = float(norm_min) if norm_min not in (None, "") else None
            except Exception:
                norm_min_f = None

            metrics = {
                "btp_code": code,
                "btp_name": name,
                "norm_time_min": norm_min_f,
                "ot_qty": _safe_float(qty.get("overtime")),
                "order_refs": ([order_ref] if order_ref else []),
                "dept_name": str((pr.get("dept") or {}).get("name") or "").strip() or None,
                "ot_staff_recommended": int(_safe_float((wf or {}).get("ot_staff_recommended"), 0.0)),
                "window_start": str((dates or {}).get("window_start") or "").strip() or None,
                "window_end": str((dates or {}).get("window_end") or "").strip() or None,
                "due": str((dates or {}).get("due") or "").strip() or None,
            }

            def _merge_into(key: str) -> None:
                if not key:
                    return
                k = key.upper()
                existing = out.get(k)
                if not isinstance(existing, dict):
                    out[k] = metrics
                    return
                # Preserve existing metrics (date window, norms) and only merge order refs.
                ex_refs = existing.get("order_refs") if isinstance(existing.get("order_refs"), list) else []
                new_refs = metrics.get("order_refs") if isinstance(metrics.get("order_refs"), list) else []
                merged = []
                seen: set[str] = set()
                for rr in [*ex_refs, *new_refs]:
                    rrs = str(rr or "").strip()
                    if not rrs or rrs in seen:
                        continue
                    seen.add(rrs)
                    merged.append(rrs)
                    if len(merged) >= 6:
                        break
                existing["order_refs"] = merged
                out[k] = existing

            if code:
                _merge_into(code)
            if name:
                _merge_into(name)
    except Exception:
        return out
    return out


def _inject_order_clause_into_btp_header(line: str, clause: str) -> str:
    """Inject order clause into '+ Bán thành phẩm ...' line at a business-friendly position.

    Target format (example):
      "+ Bán thành phẩm ... của thành phẩm ... của đơn hàng (...), cần ..."
    """

    try:
        s = str(line or "")
        c = str(clause or "").strip()
        if not s.strip() or not c:
            return s
        if re.search(r"(?i)\bđơn\s*hàng\b", s):
            return s

        # Prefer inserting right before ", cần ..." if present.
        m = re.search(r"(?i),\s*cần\b", s)
        if m:
            return s[: m.start()].rstrip() + " " + c + s[m.start() :]

        # Fallback: insert right before " cần ..." and add comma.
        m2 = re.search(r"(?i)\s+cần\b", s)
        if m2:
            return s[: m2.start()].rstrip() + " " + c + "," + s[m2.start() :]

        # Last resort: append.
        return s.rstrip() + " " + c
    except Exception:
        return str(line or "")


def _patch_lines_with_order_refs(content_lines: List[str], *, btp_metrics_map: Dict[str, Dict[str, Any]]) -> List[str]:
    """Best-effort: add 'của đơn hàng ...' into each BTP header line.

    We only patch when we can extract a BTP token (BTPT.../BTP...) and we have order_refs for that token.
    """

    if not content_lines or not btp_metrics_map:
        return content_lines

    out: List[str] = []
    cur_btp_key: str | None = None
    for raw in content_lines:
        ln = str(raw or "").strip()
        if not ln:
            continue

        if re.match(r"(?i)^\+\s*bán\s+thành\s+phẩm\b", ln):
            tok = _extract_btp_token(ln)
            cur_btp_key = tok or cur_btp_key
            metrics = btp_metrics_map.get((cur_btp_key or "").upper()) if cur_btp_key else None
            refs = (metrics or {}).get("order_refs") if isinstance(metrics, dict) else None
            refs_list = refs if isinstance(refs, list) else []
            refs_list = [str(x).strip() for x in refs_list if str(x or "").strip()]
            clause = ""
            if len(refs_list) == 1:
                clause = f"của đơn hàng {refs_list[0]}"
            elif len(refs_list) >= 2:
                shown = refs_list[:3]
                tail = "" if len(refs_list) <= 3 else "..."
                clause = f"của các đơn hàng: {', '.join(shown)}{tail}"

            out.append(_inject_order_clause_into_btp_header(ln, clause))
            continue

        out.append(ln)

    return out


def _fmt_hours(x: float) -> str:
    try:
        if x is None:
            return "không đủ dữ liệu"
        v = float(x)
        if abs(v - round(v)) < 1e-9:
            return str(int(round(v)))
        return str(round(v, 2))
    except Exception:
        return "không đủ dữ liệu"


def _parse_iso_date(s: Any) -> date | None:
    try:
        if not s:
            return None
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _extract_btp_token(text: str) -> str | None:
    try:
        s = str(text or "")
        m = re.search(r"\b(BTPT\w+|BTP\w+)\b", s, flags=re.IGNORECASE)
        if not m:
            return None
        return str(m.group(1)).upper()
    except Exception:
        return None


def _extract_first_int(text: str) -> int | None:
    try:
        m = re.search(r"(-?\d+)", str(text or ""))
        if not m:
            return None
        return int(m.group(1))
    except Exception:
        return None


def _strip_parenthetical_note(s: str) -> str:
    try:
        # Remove trailing parenthetical notes e.g. "26 (...)"
        return re.sub(r"\s*\([^\)]*\)\s*", " ", str(s or "")).strip()
    except Exception:
        return str(s or "")


def _sanitize_template1_machine_line(line: str) -> str:
    """Ensure template 1 machine line contains machine list or a safe fallback."""

    try:
        s = str(line or "").strip()
        if not re.match(r"(?i)^\+\s*số\s+lượng\s+máy\s+cần\s+cho\s+tăng\s+ca\s*:", s): 
            return s

        sl = s.lower()
        if "máy" in sl or "không đủ dữ liệu" in sl or "không áp dụng" in sl:
            return s

        # Replace nonsensical content (e.g. 'Nhân công') with safe fallback.
        return "+ Số lượng máy cần cho tăng ca: không đủ dữ liệu"
    except Exception:
        return str(line or "")


def _optimize_template1_lines(
    content_lines: List[str],
    *,
    btp_metrics_map: Dict[str, Dict[str, Any]],
    daily_ot_hours_per_staff: float = 4.0,
) -> List[str]:
    """Deterministically fill OT hours and OT date window for Template 1.

    Rules:
    - OT hours = Qty * norm_time_min / 60
    - Remove explanatory notes in parentheses
    - Estimate days = ceil(ot_hours / (staff * 4))
    - Use end date = due if available else window_end
    - start date = max(window_start, end - days + 1)
    """

    if not content_lines:
        return content_lines

    cur_btp_key: str | None = None
    cur_qty: int | None = None
    cur_staff: int | None = None

    out: List[str] = []
    for raw in content_lines:
        ln = str(raw or "").strip()
        if not ln:
            continue

        # Track BTP for the block
        if re.match(r"(?i)^\+\s*bán\s+thành\s+phẩm\b", ln):
            tok = _extract_btp_token(ln)
            cur_btp_key = tok or cur_btp_key
            cur_qty = None
            cur_staff = None
            out.append(ln)
            continue

        if re.match(r"(?i)^\+\s*số\s+lượng\s+cần\s+sản\s+xuất(\s+tăng\s+ca)?\s*:", ln):
            v = _extract_first_int(ln)
            if v is not None and v >= 0:
                cur_qty = v
                out.append(f"+ Số lượng cần sản xuất: {cur_qty}")
                continue

        if re.match(r"(?i)^\+\s*số\s+lượng\s+nhân\s+sự\b", ln):
            v = _extract_first_int(ln)
            if v is not None and v >= 0:
                cur_staff = v
                dept_name = None
                metrics = btp_metrics_map.get((cur_btp_key or "").upper()) if cur_btp_key else None
                if isinstance(metrics, dict):
                    dept_name = metrics.get("dept_name")
                if dept_name:
                    out.append(f"+ Số lượng nhân sự {dept_name} cần tăng ca: {cur_staff}")
                else:
                    out.append(f"+ Số lượng nhân sự cần tăng ca: {cur_staff}")
                continue

        # OT hours line
        if re.match(r"(?i)^\+\s*(số\s+giờ\s+cần\s+tăng\s+ca|tổng\s+số\s+giờ\s+phải\s+tăng\s+ca|tổng\s+số\s+giờ\s+cần\s+tăng\s+ca)\s*:", ln):
            base = _strip_parenthetical_note(ln)

            metrics = btp_metrics_map.get((cur_btp_key or "").upper()) if cur_btp_key else None
            norm_min = None
            if isinstance(metrics, dict):
                norm_min = metrics.get("norm_time_min")

            if cur_qty is not None and norm_min not in (None, ""):
                try:
                    ot_hours = float(cur_qty) * float(norm_min) / 60.0
                    out.append(f"+ Tổng số giờ cần tăng ca: {_fmt_hours(ot_hours)}")
                    continue
                except Exception:
                    pass

            # fallback: keep the cleaned line but remove "chưa xác định..." tail if present
            base2 = re.sub(r"(?i)chưa\s+xác\s+định\s+rõ.*$", "không đủ dữ liệu", base)
            out.append(base2)
            continue

        # OT window line
        if re.match(r"(?i)^\+\s*bắt\s+đầu\s+tăng\s+ca\s*:", ln):
            metrics = btp_metrics_map.get((cur_btp_key or "").upper()) if cur_btp_key else None
            norm_min = metrics.get("norm_time_min") if isinstance(metrics, dict) else None
            ws = _parse_iso_date(metrics.get("window_start")) if isinstance(metrics, dict) else None
            we = _parse_iso_date(metrics.get("window_end")) if isinstance(metrics, dict) else None
            due = _parse_iso_date(metrics.get("due")) if isinstance(metrics, dict) else None
            end_d = due or we

            if end_d and cur_qty is not None and norm_min not in (None, ""):
                try:
                    ot_hours = float(cur_qty) * float(norm_min) / 60.0
                    denom = float(max(1, int(cur_staff or 0))) * float(daily_ot_hours_per_staff)
                    days_needed = int(ceil(float(ot_hours) / denom)) if denom > 0 else 1
                    days_needed = max(1, days_needed)
                    start_d = end_d - timedelta(days=days_needed - 1)
                    if ws and start_d < ws:
                        start_d = ws
                    out.append(f"+ Bắt đầu tăng ca: từ ngày {start_d.isoformat()} đến ngày {end_d.isoformat()}")
                    continue
                except Exception:
                    pass

            out.append(_strip_parenthetical_note(ln))
            continue

        out.append(_sanitize_template1_machine_line(_strip_parenthetical_note(ln)))

    # Remove exact duplicates while preserving order (LLM sometimes repeats staff lines)
    seen: set[str] = set()
    uniq: List[str] = []
    for ln in out:
        key = str(ln)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ln)
    return uniq


def _patch_machine_codes(text: str, machine_name_map: Dict[str, str]) -> str:
    """Best-effort normalize M### occurrences to [M###]-[Name] when name known.

    - Keeps existing bracketed forms intact.
    - Converts 'Máy M011-CNC 1' -> 'Máy [M011]-[CNC 1]'.
    - Converts bare 'M011' -> '[M011]-[CNC 1]'.
    """

    if not text:
        return text
    if not machine_name_map:
        return text

    s = str(text)

    def _strip_may_prefix(name: str) -> str:
        n = str(name or "").strip()
        n = re.sub(r"(?i)^máy\s+", "", n).strip()
        return n

    def _repl_code_hyphen_may(m: re.Match) -> str:
        code = str(m.group(1) or "").upper()
        raw_name = _strip_may_prefix(str(m.group(2) or ""))
        name = machine_name_map.get(code) or raw_name or "không đủ dữ liệu"
        return f"[{code}]-[{name}]"

    def _repl_code_hyphen_name(m: re.Match) -> str:
        code = str(m.group(1) or "").upper()
        raw_name = _strip_may_prefix(str(m.group(2) or ""))
        name = machine_name_map.get(code) or raw_name or "không đủ dữ liệu"
        return f"[{code}]-[{name}]"

    # M011-Máy CNC 1
    s = re.sub(r"\b(M\d{3})\s*-\s*Máy\s*([^,;\]\n]+)", _repl_code_hyphen_may, s, flags=re.IGNORECASE)

    # M011-CNC 1 (generic hyphen form)
    s = re.sub(r"\b(M\d{3})\s*-\s*([^,;\]\n]+)", _repl_code_hyphen_name, s)

    def _repl_hyphen(m: re.Match) -> str:
        code = str(m.group(1) or "").upper()
        name = _strip_may_prefix(str(m.group(2) or ""))
        if not name:
            name = machine_name_map.get(code) or "không đủ dữ liệu"
        return f"Máy [{code}]-[{name}]"

    # Máy M011-CNC 1
    s = re.sub(r"(?i)\bMáy\s+(M\d{3})\s*-\s*([^,;\]\n]+)", _repl_hyphen, s)

    def _repl_bare(m: re.Match) -> str:
        code = str(m.group(0) or "").upper()
        name = machine_name_map.get(code)
        if not name:
            return code
        return f"[{code}]-[{name}]"

    # Bare M### not already in brackets
    s = re.sub(r"(?<!\[)\bM\d{3}\b(?!\s*[-\]])", _repl_bare, s)
    return s


def _remove_redundant_machine_parens(text: str) -> str:
    """Remove redundant machine-name parentheses.

    Example: 'Máy [M013]-[CNC 3] (CNC 3).' -> 'Máy [M013]-[CNC 3].'
    """

    try:
        s = str(text or "")
        # [M013]-[CNC 3] (CNC 3)
        s = re.sub(r"(\[M\d{3}\]-\[(?P<n>[^\]]+)\])\s*\(\s*(?P=n)\s*\)", r"\1", s)
        return s
    except Exception:
        return str(text or "")


def _sanitize_jargon(text: str) -> str:
    """Remove/translate technical jargon for business UI."""

    try:
        s = str(text or "")

        # Remove explicit English jargon; keep meaning in Vietnamese.
        s = re.sub(r"\(\s*bottleneck\s*\)", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\bbottleneck\b", "quá tải", s, flags=re.IGNORECASE)

        # Dependency jargon
        s = re.sub(r"\(\s*dependency\s*\)", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\bdependency\b", "phụ thuộc công đoạn", s, flags=re.IGNORECASE)
        s = re.sub(r"\bblocked_by\b", "bị chặn bởi công đoạn trước", s, flags=re.IGNORECASE)

        # Planning terms that are too technical for business UI.
        s = re.sub(r"\bbaseline\b", "kế hoạch hiện tại", s, flags=re.IGNORECASE)
        s = re.sub(r"\bphase\s*group\b", "bộ phận", s, flags=re.IGNORECASE)

        # Hide technical operation-id strings in business text.
        s = re.sub(r"(?i)\boperation\s+KHSX/[^\s,;]+", "công đoạn liên quan", s)
        s = re.sub(r"\bKHSX/\d{2}/\d{4}/\d+::[^\s,;]+", "công đoạn liên quan", s, flags=re.IGNORECASE)
        s = re.sub(r"\boperation\b", "công đoạn", s, flags=re.IGNORECASE)

        # Planning jargon
        s = re.sub(r"\bLateQty\b", "sản lượng trễ tiến độ", s, flags=re.IGNORECASE)
        s = re.sub(r"\bUnplannedQty\b", "sản lượng chưa lên kế hoạch", s, flags=re.IGNORECASE)
        s = re.sub(r"\bunplanned\b", "chưa lên kế hoạch", s, flags=re.IGNORECASE)
        s = re.sub(r"\bplanned\b", "đã lên kế hoạch", s, flags=re.IGNORECASE)
        s = re.sub(r"\bovertime\b", "tăng ca", s, flags=re.IGNORECASE)
        # OT as standalone token
        s = re.sub(r"\bOT\b", "tăng ca", s)

        # A bit more user-friendly wording
        s = re.sub(r"\bnghẽn\s*nút\b", "quá tải", s, flags=re.IGNORECASE)

        # Replace technical delta-format with business sentence when leaked from model output.
        m = re.search(
            r"(?i)tác\s+động\s+dự\s+kiến\s*:\s*late_ops_delta\s*=\s*(-?\d+)\s*,\s*tardiness_minutes_delta\s*=\s*(-?\d+)",
            s,
        )
        if m:
            try:
                s = re.sub(
                    r"(?i)tác\s+động\s+dự\s+kiến\s*:\s*late_ops_delta\s*=\s*(-?\d+)\s*,\s*tardiness_minutes_delta\s*=\s*(-?\d+)\.?",
                    _format_expected_impact_line(
                        {
                            "late_ops_delta": int(m.group(1)),
                            "tardiness_minutes_delta": int(m.group(2)),
                        }
                    ),
                    s,
                )
            except Exception:
                pass

        # Normalize spaces around punctuation
        s = re.sub(r"\s{2,}", " ", s)
        s = s.replace(" ,", ",").replace(" .", ".")
        return s.strip()
    except Exception:
        return str(text or "")


def _calendar_has_labor_resource(calendar: Dict[str, Any]) -> bool:
    rows = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
    for r in rows:
        if not isinstance(r, dict):
            continue
        resource_code = str(_pick_first(r, ["MaNguonLuc", "ResourceID", "ResourceCode"]) or "").upper()
        if "M000" in resource_code:
            return True
        resource_name = str(_pick_first(r, ["NguonLucText", "TenNguonLuc", "ResourceNames"]) or "").lower()
        if "nhân công" in resource_name or "nhan cong" in resource_name or "labor" in resource_name:
            return True
    return False


def _pick_most_impacted_blocked_row(problem_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any] | None:
    best: Tuple[float, Dict[str, Any]] | None = None
    for r in problem_rows or []:
        if not isinstance(r, dict):
            continue
        dep = r.get("dependency") if isinstance(r.get("dependency"), dict) else {}
        if not bool(dep.get("is_blocked_by_predecessor")):
            continue
        q = r.get("qty") if isinstance(r.get("qty"), dict) else {}
        score = _safe_float(q.get("unplanned")) * 10.0 + _safe_float(q.get("late")) * 8.0 + _safe_float(q.get("overtime"))
        if best is None or score > best[0]:
            best = (score, r)
    return best[1] if best else None


def _find_blocker_row(problem_rows: Sequence[Dict[str, Any]], blocked_by: Dict[str, Any], *, tp_id: Any, don_hang_id: Any, line_key: Any) -> Dict[str, Any] | None:
    try:
        bb_tts = int(blocked_by.get("thu_tu_sx") or 0)
        bb_btp = int(blocked_by.get("btp_dinh_muc_id") or 0)
        bb_cd = str(blocked_by.get("ma_cong_doan") or "")
        if not (bb_tts and bb_btp and bb_cd):
            return None
    except Exception:
        return None

    for r in problem_rows or []:
        if not isinstance(r, dict):
            continue
        op = r.get("op") if isinstance(r.get("op"), dict) else {}
        try:
            if int(op.get("tp_dinh_muc_id") or 0) != int(tp_id or 0):
                continue
            if int(op.get("don_hang_id") or 0) != int(don_hang_id or 0):
                continue
            if str(op.get("line_key") or "") != str(line_key or ""):
                continue
            if int(op.get("btp_dinh_muc_id") or 0) != bb_btp:
                continue
            if int(op.get("thu_tu_sx") or 0) != bb_tts:
                continue
            if str(op.get("ma_cong_doan") or "") != bb_cd:
                continue
            return r
        except Exception:
            continue
    return None


def _list_blocked_successor_steps(problem_rows: Sequence[Dict[str, Any]], blocked_by: Dict[str, Any], *, tp_id: Any, don_hang_id: Any, line_key: Any) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for r in problem_rows or []:
        if not isinstance(r, dict):
            continue
        dep = r.get("dependency") if isinstance(r.get("dependency"), dict) else {}
        if not bool(dep.get("is_blocked_by_predecessor")):
            continue
        if not isinstance(dep.get("blocked_by"), dict):
            continue
        if dep.get("blocked_by") != blocked_by:
            continue

        op = r.get("op") if isinstance(r.get("op"), dict) else {}
        try:
            if int(op.get("tp_dinh_muc_id") or 0) != int(tp_id or 0):
                continue
            if int(op.get("don_hang_id") or 0) != int(don_hang_id or 0):
                continue
            if str(op.get("line_key") or "") != str(line_key or ""):
                continue
        except Exception:
            continue

        step = r.get("step") if isinstance(r.get("step"), dict) else {}
        label = _fmt_code_name(step.get("code"), step.get("name"))
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= 6:
            break
    return out


def _patch_analysis_with_dependency_summary(
    analysis_text: str,
    *,
    business_context: Dict[str, Any],
    baseline_validation_summary: Any,
) -> str:
    """Ensure the 4-line analysis mentions dependency root-cause when relevant."""

    try:
        prs = business_context.get("problem_rows") if isinstance(business_context, dict) else []
        if not isinstance(prs, list) or not prs:
            return analysis_text

        blocked_primary = _pick_most_impacted_blocked_row(prs)
        if not blocked_primary:
            return analysis_text

        dep = blocked_primary.get("dependency") if isinstance(blocked_primary.get("dependency"), dict) else {}
        blocked_by = dep.get("blocked_by") if isinstance(dep.get("blocked_by"), dict) else None
        if not isinstance(blocked_by, dict) or not blocked_by:
            return analysis_text

        op = blocked_primary.get("op") if isinstance(blocked_primary.get("op"), dict) else {}
        tp_id = op.get("tp_dinh_muc_id")
        dh_id = op.get("don_hang_id")
        lk = op.get("line_key")

        blocker_row = _find_blocker_row(prs, blocked_by, tp_id=tp_id, don_hang_id=dh_id, line_key=lk)
        blocker_step = (blocker_row or {}).get("step") if isinstance((blocker_row or {}).get("step"), dict) else {}
        blocker_label = _fmt_code_name(blocker_step.get("code") or blocked_by.get("ma_cong_doan"), blocker_step.get("name") or "không đủ dữ liệu")

        blocked_steps = _list_blocked_successor_steps(prs, blocked_by, tp_id=tp_id, don_hang_id=dh_id, line_key=lk)
        blocked_steps_txt = ", ".join(blocked_steps) if blocked_steps else "không đủ dữ liệu"

        # conflicts summary
        summary = baseline_validation_summary if isinstance(baseline_validation_summary, dict) else {}
        has_any_err = any(int(v or 0) > 0 for v in summary.values()) if summary else False
        conflict_prefix = "Có phát sinh ràng buộc/xung đột theo kiểm tra tự động" if has_any_err else "Không ghi nhận xung đột nguồn lực theo kiểm tra tự động"

        # Keep 4 lines; replace line2/line3 with deterministic dependency-aware summary.
        lines = [ln.strip() for ln in str(analysis_text or "").splitlines() if ln.strip()]
        while len(lines) < 4:
            lines.append("không đủ dữ liệu.")
        lines = lines[:4]

        lines[1] = (
            f"{conflict_prefix}. Nguyên nhân chính là phụ thuộc công đoạn: công đoạn {blocker_label} chưa hoàn thành "
            f"nên các công đoạn tiếp theo bị chặn ({blocked_steps_txt})."
        )
        lines[2] = (
            "Kế hoạch sản xuất chưa tối ưu do phụ thuộc công đoạn chưa được xử lý trước, khiến các bước sau khó phân bổ trong khung thời gian hợp lệ. "
            f"Cần ưu tiên giải quyết công đoạn gây chặn {blocker_label} (tăng ca/bổ sung nguồn lực/đổi máy nếu có) rồi mới tối ưu các công đoạn sau."
        )

        return "\n".join([_sanitize_jargon(x) for x in lines])
    except Exception:
        return analysis_text


def _soften_conflict_claims_if_no_conflicts(text: str, *, baseline_validation_summary: Any) -> str:
    """If baseline validation reports no conflicts, avoid hard claims like 'đang xung đột'."""

    try:
        summary = baseline_validation_summary if isinstance(baseline_validation_summary, dict) else {}
        has_any_error = any(int(v or 0) > 0 for v in summary.values()) if summary else False
        if has_any_error:
            return str(text or "")

        s = str(text or "")
        # Replace the strongest claims but keep the sentence readable.
        s = re.sub(
            r"(?i)đang\s+bị\s+sử\s+dụng\s+xung\s+đột",
            "chưa ghi nhận xung đột theo kiểm tra tự động (cần kiểm tra tải/năng lực)",
            s,
        )
        s = re.sub(
            r"(?i)xung\s+đột\s+nguồn\s+lực",
            "ràng buộc tải/năng lực",
            s,
        )
        return s
    except Exception:
        return str(text or "")


def _should_drop_impact_line(line: str, *, current_plan_code: str | None) -> bool:
    """Drop generic 'impact to plan' sentence unless it clearly refers to another plan."""

    s = str(line or "").strip()
    if not s:
        return False

    # Only target the specific sentence family.
    if not re.match(r"(?i)^\+\s*việc\s+điều\s+chỉnh.*ảnh\s+hưởng.*kế\s+hoạch", s):
        return False

    sl = s.lower()
    if "kế hoạch khác" in sl or "ke hoach khac" in sl:
        return False

    plans = re.findall(r"\bKHSX/\d{2}/\d{4}/\d+\b", s, flags=re.IGNORECASE)
    plans_u = [p.upper() for p in plans]
    cur = (current_plan_code or "").strip().upper()

    # If the line doesn't name any other plan, it's speculative -> drop.
    if not plans_u:
        return True

    # If it only references the current plan, it's redundant -> drop.
    if cur and all(p == cur for p in plans_u):
        return True

    # If current plan code is unknown, keep conservative: hide to avoid misleading.
    if not cur:
        return True

    # Keep only if it references a different plan.
    return not any(p != cur for p in plans_u)


def _map_options_to_ui(
    options: Sequence[Dict[str, Any]],
    *,
    machine_name_map: Optional[Dict[str, str]] = None,
    current_plan_code: Optional[str] = None,
    btp_metrics_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Map PlanAIService options to FE response shape.

    FE expects:
      - suggestions: [{key,title,description,meta:{template_no,priority,can_apply,content_lines,moves,...}}]
      - ai_plans: {PA1:{rows,note}, ...}
    """

    suggestions: List[Dict[str, Any]] = []
    plans_by_pa: Dict[str, Any] = {}

    idx = 0
    for opt in list(options or [])[:20]:
        if not isinstance(opt, dict):
            continue
        idx += 1
        key = f"PA{idx}"

        title = str(opt.get("goal") or opt.get("title") or f"Phương án {idx}").strip() or f"Phương án {idx}"
        priority = str(opt.get("priority") or "").strip() or f"P{int(opt.get('template_no') or 2)}"
        template_no = opt.get("template_no")
        try:
            template_no = int(template_no) if template_no is not None else None
        except Exception:
            template_no = None

        raw_lines = opt.get("content_lines")
        if isinstance(raw_lines, list):
            lines = [str(x).strip() for x in raw_lines if x is not None and str(x).strip()]
        else:
            lines = []

        # Normalize '+ ' prefix and cap
        content_lines: List[str] = []
        for ln in lines:
            s = str(ln or "").strip()
            if not s:
                continue
            if not s.startswith("+"):
                s = "+ " + s.lstrip("-•* ")
            if not s.startswith("+ "):
                s = "+ " + s[1:].lstrip()
            s = " ".join(s.split())
            if machine_name_map:
                s = _patch_machine_codes(s, machine_name_map)
            s = _remove_redundant_machine_parens(s)
            s = _sanitize_jargon(s)
            if _should_drop_impact_line(s, current_plan_code=current_plan_code):
                continue
            content_lines.append(s)

        # Remove exact duplicate lines while preserving order
        if content_lines:
            uniq_lines: List[str] = []
            seen_lines: set[str] = set()
            for ln in content_lines:
                key_ln = str(ln or "").strip()
                if not key_ln or key_ln in seen_lines:
                    continue
                seen_lines.add(key_ln)
                uniq_lines.append(key_ln)
            content_lines = uniq_lines

        # Template 1: deterministically compute OT hours/date window and remove notes.
        if template_no == 1 and btp_metrics_map:
            content_lines = _optimize_template1_lines(content_lines, btp_metrics_map=btp_metrics_map)

        # Add order references into BTP header lines (best-effort), for multi-order plans.
        if btp_metrics_map:
            content_lines = _patch_lines_with_order_refs(content_lines, btp_metrics_map=btp_metrics_map)

        content_lines = content_lines[:24]

        moves = opt.get("moves") if isinstance(opt.get("moves"), list) else []
        llm_can_apply = bool(opt.get("can_apply")) if opt.get("can_apply") is not None else None
        can_apply = True

        desc = "; ".join([x for x in content_lines if x])
        if not desc:
            notes = str(opt.get("notes") or "").strip()
            desc = "; ".join([p.strip() for p in notes.splitlines() if p.strip()]) if notes else ""

        kpi_before = opt.get("kpi_before")
        kpi_after = opt.get("kpi_after")
        kpi_before_out = kpi_before
        kpi_after_out = kpi_after
        delta_out = opt.get("delta")
        try:
            if kpi_before == kpi_after:
                kpi_before_out = None
                kpi_after_out = None
                if not any(v not in (None, 0, 0.0) for v in ((delta_out or {}).values() if isinstance(delta_out, dict) else [])):
                    delta_out = None
        except Exception:
            pass

        validation_explain_out = opt.get("validation_explain")
        try:
            ve = validation_explain_out
            if isinstance(ve, list) and not ve:
                validation_explain_out = None
        except Exception:
            pass

        proposed_due_date = None
        if template_no == 6:
            due_text = "\n".join(
                [
                    str(title or ""),
                    "\n".join([str(x) for x in content_lines if str(x).strip()]),
                    json.dumps(moves, ensure_ascii=False, default=str),
                ]
            )
            due_matches = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", due_text)
            if due_matches:
                proposed_due_date = sorted(due_matches)[-1]

        if machine_name_map:
            title = _patch_machine_codes(title, machine_name_map)
            desc = _patch_machine_codes(desc, machine_name_map)
        title = _sanitize_jargon(_remove_redundant_machine_parens(title))
        desc = _sanitize_jargon(_remove_redundant_machine_parens(desc))

        suggestions.append(
            {
                "key": key,
                "title": title,
                "description": desc,
                "meta": {
                    "priority": priority,
                    "template_no": template_no,
                    "can_apply": can_apply,
                    "llm_can_apply_hint": llm_can_apply,
                    "content_lines": content_lines,
                    "moves": moves,
                    "validation_errors": opt.get("validation_errors"),
                    "validation_summary": opt.get("validation_summary"),
                    "validation_explain": validation_explain_out,
                    "kpi_before": kpi_before_out,
                    "kpi_after": kpi_after_out,
                    "delta": delta_out,
                    "proposed_due_date": proposed_due_date,
                },
            }
        )

        plan_rows = opt.get("plan_rows") if isinstance(opt.get("plan_rows"), list) else []
        note_raw = str(opt.get("notes") or "").strip()
        note_norm = "\n".join([str(x).strip() for x in note_raw.splitlines() if str(x).strip()]) if note_raw else ""
        desc_norm = "; ".join([str(x).strip() for x in content_lines if str(x).strip()])
        if note_norm and (note_norm.replace("\n", "; ").strip() == desc_norm.strip()):
            note_norm = ""
        plans_by_pa[key] = {
            "rows": plan_rows,
            "note": note_norm,
        }

    return suggestions, plans_by_pa


def _fmt_code_name(code: Any, name: Any) -> str:
    c = str(code).strip() if code not in (None, "") else "không đủ dữ liệu"
    n = str(name).strip() if name not in (None, "") else "không đủ dữ liệu"
    return f"[{c}]-[{n}]"


def _split_csv(x: Any) -> List[str]:
    if x in (None, ""):
        return []
    return [p.strip() for p in str(x).split(",") if p and str(p).strip()]


def _build_machine_maps_from_calendar(calendar: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str]]:
    code_to_name: Dict[str, str] = {}
    name_to_code: Dict[str, str] = {}
    rows = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
    for r in rows:
        if not isinstance(r, dict):
            continue
        codes = [str(x).strip().upper() for x in _split_csv(_pick_first(r, ["MaNguonLuc"])) if str(x).strip()]
        names = [str(x).strip() for x in _split_csv(_pick_first(r, ["NguonLucText", "TenNguonLuc"])) if str(x).strip()]
        if not codes:
            continue
        for idx, code in enumerate(codes):
            if not code or code == "M000":
                continue
            name = names[idx] if idx < len(names) else (names[0] if len(names) == 1 else "")
            if not name:
                continue
            code_to_name.setdefault(code, name)
            name_to_code.setdefault(name.upper(), code)
            name_no_prefix = re.sub(r"(?i)^máy\s+", "", name).strip().upper()
            if name_no_prefix:
                name_to_code.setdefault(name_no_prefix, code)
    return code_to_name, name_to_code


def _best_due_date_from_calendar(calendar: Dict[str, Any]) -> Optional[str]:
    header = calendar.get("Header") if isinstance(calendar.get("Header"), dict) else {}
    due = _pick_first(header, ["DenNgay", "EndDateManufactering", "EndDate"])
    if due not in (None, ""):
        return str(due)[:10]
    rows = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = _pick_first(r, ["DueDT", "DueDate", "DenNgay"])
        if d not in (None, ""):
            return str(d)[:10]
    return None


def _patch_machine_list_with_codes(line: str, *, machine_name_map: Dict[str, str], machine_name_to_code: Dict[str, str]) -> str:
    s = str(line or "").strip()
    if not re.match(r"(?i)^\+\s*(số\s+lượng|số)\s+máy\s+cần\s+cho\s+tăng\s+ca\s*:", s):
        return s
    head, tail = s.split(":", 1)
    raw = tail.strip()
    if not raw:
        return s
    if re.search(r"(?i)\b(LABOR|M000)\b", raw):
        return f"{head}: không áp dụng (nguồn lực nhân công)"

    normalized: List[str] = []
    tokens = [t.strip().strip(".") for t in raw.split(",") if str(t).strip()]
    for tok in tokens:
        m = re.search(r"\b(M\d{3})\b", tok, flags=re.IGNORECASE)
        if m:
            code = str(m.group(1) or "").upper()
            name = machine_name_map.get(code)
            if name:
                normalized.append(f"[{code}]-[{name}]")
            else:
                normalized.append(tok)
            continue
        key = re.sub(r"(?i)^máy\s+", "", tok).strip().strip(".").upper()
        key = re.sub(r"\s+", " ", key)
        code = machine_name_to_code.get(key)
        if not code:
            for name_key, c in machine_name_to_code.items():
                if not name_key:
                    continue
                if key == name_key or key in name_key or name_key in key:
                    code = c
                    break
        if code:
            name = machine_name_map.get(code) or re.sub(r"(?i)^máy\s+", "", tok).strip()
            normalized.append(f"[{code}]-[{name}]")
        else:
            normalized.append(tok)

    if not normalized:
        return s
    return f"{head}: Máy {', '.join(normalized)}"


def _sanitize_compact_llm_proposals(
    proposals: Sequence[Dict[str, Any]],
    *,
    machine_name_map: Dict[str, str],
    machine_name_to_code: Dict[str, str],
    due_fallback: Optional[str],
    btp_metrics_map: Optional[Dict[str, Dict[str, Any]]] = None,
    has_labor_resource: bool = True,
) -> List[Dict[str, Any]]:
    btp_metrics_map = btp_metrics_map or {}
    out: List[Dict[str, Any]] = []
    for p in proposals or []:
        if not isinstance(p, dict):
            continue
        p2 = dict(p)
        lines_in = p2.get("content_lines") if isinstance(p2.get("content_lines"), list) else []
        lines_out: List[str] = []
        for ln in lines_in:
            s = str(ln or "").strip()
            if not s:
                continue
            if due_fallback:
                s = re.sub(
                    r"(?i)lịch\s+giao\s*(hàng)?\s+không\s+đủ\s+dữ\s+liệu",
                    f"lịch giao hàng {due_fallback}",
                    s,
                )
                s = re.sub(
                    r"(?i)(đến\s+khi\s+hết\s+tồn\s+đọng\s+trễ|ngay\s+lập\s+tức\s+đến\s+khi\s+hết\s+tồn\s+đọng\s+trễ)",
                    f"đến ngày {due_fallback}",
                    s,
                )

            m_btp = re.search(r"\b(BTP\w+)\s*-\s*không\s+đủ\s+dữ\s+liệu\b", s, flags=re.IGNORECASE)
            if m_btp:
                btp_code = str(m_btp.group(1) or "").upper()
                m_info = btp_metrics_map.get(btp_code) if isinstance(btp_metrics_map, dict) else None
                btp_name = str((m_info or {}).get("btp_name") or "").strip() if isinstance(m_info, dict) else ""
                if btp_name:
                    s = re.sub(
                        r"\b" + re.escape(m_btp.group(1)) + r"\s*-\s*không\s+đủ\s+dữ\s+liệu\b",
                        f"{btp_code}-{btp_name}",
                        s,
                        flags=re.IGNORECASE,
                    )
            s = re.sub(r"(?i)Máy\s+LABOR\s*-\s*không\s+đủ\s+dữ\s+liệu", "nguồn lực nhân công", s)
            s = re.sub(r"(?i)\bLABOR\b", "nhân công", s)
            if not has_labor_resource:
                s = re.sub(r"(?i)nguồn\s+lực\s+nhân\s+công", "nguồn lực", s)
                s = re.sub(r"(?i)nhân\s+công\s+hiện\s+tại", "nguồn lực hiện tại", s)
            s = _patch_machine_codes(s, machine_name_map)
            s = _patch_machine_list_with_codes(s, machine_name_map=machine_name_map, machine_name_to_code=machine_name_to_code)
            s = _sanitize_jargon(s)
            lines_out.append(s)
        p2["content_lines"] = lines_out
        out.append(p2)
    return out


def _sanitize_analysis_quality(text: str, *, fallback: str, calendar: Dict[str, Any]) -> str:
    s = _sanitize_jargon(str(text or "").strip())
    if not s:
        return _sanitize_jargon(str(fallback or "").strip())

    # Only fallback when output clearly unusable for business UI.
    sl = s.lower()
    if "không đủ dữ liệu" in sl:
        s = _sanitize_jargon(str(fallback or "").strip())
    line_count = len([ln for ln in s.splitlines() if str(ln).strip()])
    if line_count < 3:
        s = _sanitize_jargon(str(fallback or "").strip())

    # If input has no labor resource, suppress LABOR/nhân công wording to avoid misleading users.
    if not _calendar_has_labor_resource(calendar):
        s = re.sub(r"\bLABOR\b", "", s, flags=re.IGNORECASE)
        s = re.sub(r"(?i)nguồn\s+lực\s+nhân\s+công", "nguồn lực", s)
        s = re.sub(r"(?i)nhân\s+sự\s+thuê\s+ngoài", "nguồn lực bổ sung", s)
        s = re.sub(r"\(\s*\)", "", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x in (None, ""):
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _pick_best_row(problem_rows: Sequence[Dict[str, Any]], *, prefer_type: str | None = None) -> Dict[str, Any] | None:
    best: Tuple[float, Dict[str, Any]] | None = None
    for r in problem_rows or []:
        if not isinstance(r, dict):
            continue
        if prefer_type and str(((r.get("resource") or {}).get("type") or "")).lower() != prefer_type.lower():
            continue
        q = r.get("qty") or {}
        score = (
            _safe_float(q.get("late")) * 10.0
            + _safe_float(q.get("unplanned")) * 8.0
            + _safe_float(q.get("overtime")) * 3.0
            + _safe_float(q.get("over_capacity"))
        )

        # If a step is blocked by predecessor, proposing actions on it is usually invalid.
        # Prefer unblocked/blocker rows when available.
        try:
            dep = r.get("dependency") if isinstance(r.get("dependency"), dict) else {}
            if bool(dep.get("is_blocked_by_predecessor")):
                score = score * 0.1
        except Exception:
            pass
        if best is None or score > best[0]:
            best = (score, r)
    return best[1] if best else None


def _extract_conflict_plan_tokens(errors: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for e in errors or []:
        ctx = getattr(e, "context", None) or {}
        if not isinstance(ctx, dict):
            continue
        busy = ctx.get("busy") if isinstance(ctx.get("busy"), dict) else {}
        ma = busy.get("ma_ke_hoach")
        kid = busy.get("ke_hoach_id")
        if ma:
            out.append(str(ma))
        elif kid:
            out.append(str(kid))
    # unique, stable
    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:5]


def _fmt_machine_list(resource: Dict[str, Any]) -> str:
    codes = _split_csv(resource.get("code"))
    names = _split_csv(resource.get("name"))
    if not codes and not names:
        return "không đủ dữ liệu"

    pairs: List[str] = []
    for i, c in enumerate(codes[:6]):
        n = names[i] if i < len(names) else "không đủ dữ liệu"
        pairs.append(f"Máy {_fmt_code_name(c, n)}")
    if not pairs and names:
        # no codes, but names exist
        pairs = [f"Máy [_]-[{n}]" for n in names[:3]]
    return ", ".join(pairs) if pairs else "không đủ dữ liệu"


def _pick_dates(r: Dict[str, Any]) -> Tuple[str, str, str]:
    d = r.get("dates") or {}
    ws = str(d.get("window_start") or "không đủ dữ liệu")
    we = str(d.get("window_end") or "không đủ dữ liệu")
    due = str(d.get("due") or "")
    return ws, we, due


def _next_sunday_within(ws: str, we: str) -> str:
    try:
        start = date.fromisoformat(ws[:10])
        end = date.fromisoformat(we[:10])
    except Exception:
        return "không đủ dữ liệu"

    cur = start
    for _ in range(0, 21):
        if cur.weekday() == 6 and cur <= end:
            return cur.isoformat()
        cur = cur + timedelta(days=1)
    return "không đủ dữ liệu"


def _build_overall_analysis_items(
    problem_rows: Sequence[Dict[str, Any]],
    errors: Sequence[Any],
    hotspots: Dict[str, Any],
) -> List[str]:
    primary = _pick_best_row(problem_rows) or {}

    tp = primary.get("tp") or {}
    btp = primary.get("btp") or {}
    dept = primary.get("dept") or {}
    step = primary.get("step") or {}
    res = primary.get("resource") or {}
    qty = primary.get("qty") or {}
    ws, we, due = _pick_dates(primary)

    late = _safe_float(qty.get("late"))
    unplanned = _safe_float(qty.get("unplanned"))
    overtime = _safe_float(qty.get("overtime"))
    shortage = max(0.0, late + unplanned)

    tp_label = _fmt_code_name(tp.get("code"), tp.get("name"))
    btp_label = _fmt_code_name(btp.get("code"), btp.get("name"))
    dept_label = _fmt_code_name(dept.get("code"), dept.get("name"))
    step_label = _fmt_code_name(step.get("code"), step.get("name"))

    # 1) Late/unplanned summary
    due_txt = f" (hạn giao {due})" if due else ""
    item1 = (
        f"Bán thành phẩm {btp_label} của thành phẩm {tp_label} đang có nguy cơ trễ tiến độ{due_txt}; "
        f"khối lượng thiếu/ngoài kế hoạch ước tính {int(round(shortage))} (trễ {int(round(late))}, phát sinh {int(round(unplanned))})."
    )

    # 2) Conflict summary
    conflict_tokens = _extract_conflict_plan_tokens(errors)
    conflict_part = (
        f"; có xung đột sử dụng nguồn lực {(_fmt_code_name(res.get('code'), res.get('name')))}"
        if (res.get("code") or res.get("name"))
        else ""
    )
    if conflict_tokens:
        conflict_part += f" với kế hoạch {', '.join(conflict_tokens)}"
    item2 = (
        f"Nguồn lực/công đoạn {dept_label} / {step_label} cần được kiểm tra ràng buộc không chồng lấn theo giờ "
        f"(khung kế hoạch {ws} → {we}){conflict_part}."
    )

    # 3) Bottleneck summary
    bn = hotspots.get("bottleneck_machines") if isinstance(hotspots, dict) else []
    if not isinstance(bn, list):
        bn = []
    bn_txt = ", ".join([str(x) for x in bn[:3] if x]) or "không đủ dữ liệu"
    item3 = (
        "Kế hoạch hiện tại chưa tối ưu ở việc phân bổ tải trên nguồn lực/công đoạn; "
        f"điểm nghẽn đang tập trung ở các máy/nguồn lực: {bn_txt}, cần ưu tiên giải quyết trước hạn giao."
    )

    # 4) Conclusion
    item4 = (
        "Đề xuất áp dụng các phương án theo thứ tự 1→6 (tăng ca, bổ sung/đổi máy, điều chuyển/thuê nhân sự, làm Chủ nhật, dời due date) "
        "để đảm bảo tiến độ và không phát sinh xung đột với các kế hoạch khác."
    )

    return [item1, item2, item3, item4]


def _build_suggestions_1_to_6(
    problem_rows: Sequence[Dict[str, Any]],
    errors: Sequence[Any],
    hotspots: Dict[str, Any],
) -> List[Dict[str, Any]]:
    # Choose representative rows per template
    row_any = _pick_best_row(problem_rows) or {}
    row_machine = _pick_best_row(problem_rows, prefer_type="machine") or row_any
    row_labor = _pick_best_row(problem_rows, prefer_type="labor") or row_any

    conflict_tokens = _extract_conflict_plan_tokens(errors)
    conflict_txt = ", ".join(conflict_tokens) if conflict_tokens else "không đủ dữ liệu"

    def _tpl_common_labels(r: Dict[str, Any]) -> Tuple[str, str, str]:
        tp = (r.get("tp") or {})
        btp = (r.get("btp") or {})
        dept = (r.get("dept") or {})
        return _fmt_code_name(btp.get("code"), btp.get("name")), _fmt_code_name(tp.get("code"), tp.get("name")), str(dept.get("name") or "không đủ dữ liệu")

    def _qty_need_regular(r: Dict[str, Any]) -> int:
        q = r.get("qty") or {}
        return int(round(max(0.0, _safe_float(q.get("late")) + _safe_float(q.get("unplanned")))))

    def _qty_need_ot(r: Dict[str, Any]) -> int:
        q = r.get("qty") or {}
        ot = _safe_float(q.get("overtime"))
        if ot > 0:
            return int(round(ot))
        return _qty_need_regular(r)

    def _ot_hours(r: Dict[str, Any]) -> float:
        cap = r.get("capacity") or {}
        h = cap.get("ot_hours_needed")
        try:
            return round(float(h), 2) if h not in (None, "") else 0.0
        except Exception:
            return 0.0

    def _staff_ot(r: Dict[str, Any]) -> int:
        wf = r.get("workforce") or {}
        return int(max(0, int(wf.get("ot_staff_recommended") or 0)))

    def _staff_outsource_regular(r: Dict[str, Any]) -> int:
        wf = r.get("workforce") or {}
        v = int(max(0, int(wf.get("outsource_staff_recommended") or 0)))
        return min(OUTSOURCE_STAFF_MAX, v)

    def _staff_dept(r: Dict[str, Any]) -> int:
        wf = r.get("workforce") or {}
        return int(max(0, int(wf.get("dept_staff") or 0)))

    def _staff_transfer(r: Dict[str, Any]) -> int:
        # conservative: transfer a small chunk, never exceed dept staff
        ds = _staff_dept(r)
        if ds <= 0:
            return 1
        return min(ds, max(1, int(ceil(ds * 0.2))))

    def _dates_for_action(r: Dict[str, Any]) -> Tuple[str, str]:
        ws, we, due = _pick_dates(r)
        start = ws
        end = due or we
        return start, end

    suggestions: List[Dict[str, Any]] = []

    # 1) OT
    btp_label, tp_label, dept_name = _tpl_common_labels(row_any)
    ot_qty = _qty_need_ot(row_any)
    ot_staff = _staff_ot(row_any)

    # Deterministic overtime hours: OT hours = OT qty * định mức phút / 60
    ot_h = _ot_hours(row_any)
    try:
        norm_min = _safe_float(((row_any.get("capacity") or {}).get("norm_time_min")))
    except Exception:
        norm_min = 0.0
    if norm_min > 0 and ot_qty > 0:
        ot_h = round((float(ot_qty) * norm_min) / 60.0, 2)

    ot_start, ot_end = _dates_for_action(row_any)

    # More accurate OT window (assume 4 OT hours/day, end at due date)
    try:
        ws, _we, due = _pick_dates(row_any)
        if due and ot_h > 0 and ot_staff > 0 and ws and ws != "không đủ dữ liệu":
            end_d = date.fromisoformat(str(due)[:10])
            window_start_d = date.fromisoformat(str(ws)[:10])
            days_needed = int(ceil(ot_h / (float(ot_staff) * 4.0)))
            days_needed = max(1, days_needed)
            calc_start = end_d - timedelta(days=days_needed - 1)
            if calc_start < window_start_d:
                calc_start = window_start_d
            ot_start, ot_end = calc_start.isoformat(), end_d.isoformat()
    except Exception:
        pass
    machines_txt = _fmt_machine_list((row_any.get("resource") or {}))
    content_1 = [
        f"+ Bán thành phẩm {btp_label} của thành phẩm {tp_label}, cần sản xuất tăng ca để đáp ứng thời gian giao hàng, với thông tin cụ thể sau:",
        f"+ Số lượng cần sản xuất: {ot_qty}.",
        f"+ Tổng số giờ cần tăng ca: {ot_h}.",
        f"+ Số lượng nhân sự [{dept_name}] cần tăng ca: {ot_staff} người.",
        f"+ Số lượng máy cần cho tăng ca: {machines_txt}.",
        f"+ Bắt đầu tăng ca: từ ngày {ot_start} đến ngày {ot_end}.",
    ]
    suggestions.append(
        {
            "key": "PA1",
            "title": "Tăng ca để kịp hạn giao",
            "description": "; ".join(content_1),
            "meta": {
                "priority": "P1",
                "template_no": 1,
                "can_apply": False,
                "content_lines": content_1,
                "moves": [],
            },
        }
    )

    # 2) Add/switch machine
    btp_label, tp_label, _dept_name = _tpl_common_labels(row_machine)
    machines_txt_2 = _fmt_machine_list((row_machine.get("resource") or {}))
    content_2 = [
        f"+ Bán thành phẩm {btp_label} của thành phẩm {tp_label}, cần bổ sung thêm máy để sản xuất kịp tiến độ giao hàng với thông tin máy cụ thể như sau: {machines_txt_2}.",
        f"+ Việc điều chỉnh này sẽ ảnh hưởng (NẾU CÓ) đến các kế hoạch: {conflict_txt}, nên sẽ điều chỉnh các kế hoạch bị ảnh hưởng này như sau: sắp xếp lại thứ tự/khung giờ chạy máy để tránh chồng lấn.",
    ]
    suggestions.append(
        {
            "key": "PA2",
            "title": "Bổ sung/đổi máy để kịp tiến độ",
            "description": "; ".join(content_2),
            "meta": {
                "priority": "P2",
                "template_no": 2,
                "can_apply": False,
                "content_lines": content_2,
                "moves": [],
            },
        }
    )

    # 3) Transfer staff
    btp_label, tp_label, dept_name_3 = _tpl_common_labels(row_labor)
    transfer_staff = _staff_transfer(row_labor)
    content_3 = [
        f"+ Bán thành phẩm {btp_label} của thành phẩm {tp_label}, cần chuyển {transfer_staff} nhân sự thuộc [{dept_name_3}] từ kế hoạch [{conflict_txt}] để sản xuất kịp tiến độ giao hàng.",
        f"+ Việc điều chỉnh này sẽ ảnh hưởng đến các kế hoạch [{conflict_txt}] nên cần điều chỉnh như sau: bù lại ca/giờ làm cho kế hoạch bị điều chuyển để không phát sinh trễ mới.",
    ]
    suggestions.append(
        {
            "key": "PA3",
            "title": "Điều chuyển nhân sự giữa công đoạn",
            "description": "; ".join(content_3),
            "meta": {
                "priority": "P3",
                "template_no": 3,
                "can_apply": False,
                "content_lines": content_3,
                "moves": [],
            },
        }
    )

    # 4) Hire outsource regular + OT if needed
    btp_label, tp_label, dept_name_4 = _tpl_common_labels(row_any)
    qty_regular = _qty_need_regular(row_any)
    outsource_staff = _staff_outsource_regular(row_any)
    work_start, work_end = _dates_for_action(row_any)
    ot_qty_4 = _qty_need_ot(row_any)
    ot_h_4 = _ot_hours(row_any)
    ot_staff_4 = _staff_ot(row_any)
    machines_txt_4 = _fmt_machine_list((row_any.get("resource") or {}))
    content_4 = [
        f"+ Bán thành phẩm {btp_label} của thành phẩm {tp_label} cần thuê thêm nhân sự thời vụ làm việc giờ hành chính để đáp ứng thời gian giao hàng, với thông tin cụ thể sau:",
        f"+ Số lượng cần sản xuất: {qty_regular}.",
        f"+ Số lượng nhân sự [{dept_name_4}] cần thuê: {outsource_staff} người.",
        f"+ Bắt đầu làm việc: từ ngày {work_start} đến ngày {work_end}.",
        f"+ Số lượng máy cần: {machines_txt_4}.",
        f"+ Và tăng ca (nếu có):",
        f"+ Số lượng cần sản xuất tăng ca: {ot_qty_4}.",
        f"+ Tổng số giờ cần tăng ca: {ot_h_4}.",
        f"+ Số lượng nhân sự [{dept_name_4}] cần tăng ca: {ot_staff_4} người.",
        f"+ Số lượng máy cần cho tăng ca: {machines_txt_4}.",
        f"+ Bắt đầu tăng ca: từ ngày {work_start} đến ngày {work_end}.",
    ]
    suggestions.append(
        {
            "key": "PA4",
            "title": "Thuê nhân sự thời vụ (giờ hành chính) + tăng ca nếu cần",
            "description": "; ".join(content_4),
            "meta": {
                "priority": "P4",
                "template_no": 4,
                "can_apply": False,
                "content_lines": content_4,
                "moves": [],
            },
        }
    )

    # 5) Sunday
    btp_label, tp_label, dept_name_5 = _tpl_common_labels(row_any)
    dept_staff = _staff_dept(row_any)
    step_name = str(((row_any.get("step") or {}).get("name") or "không đủ dữ liệu"))
    ws, we, _due = _pick_dates(row_any)
    sunday = _next_sunday_within(ws, we)
    qty_sun = _qty_need_regular(row_any)
    content_5 = [
        f"+ Bán thành phẩm {btp_label} của thành phẩm {tp_label}, cần làm việc vào ngày nghỉ hàng tuần để sản xuất kịp tiến độ giao hàng, với thông tin cụ thể sau:",
        f"+ Số lượng nhân sự [{dept_name_5}]: {dept_staff} người.",
        f"+ Công đoạn: {step_name}.",
        f"+ Ngày cần làm việc (Chủ nhật): {sunday}.",
        f"+ Số lượng cần sản xuất: {qty_sun}.",
    ]
    suggestions.append(
        {
            "key": "PA5",
            "title": "Làm vào ngày nghỉ (Chủ nhật)",
            "description": "; ".join(content_5),
            "meta": {
                "priority": "P5",
                "template_no": 5,
                "can_apply": False,
                "content_lines": content_5,
                "moves": [],
            },
        }
    )

    # 6) Due date adjustment (ONLY when plan still cannot meet due)
    due_sug = _build_due_date_extension_suggestion(problem_rows)
    if due_sug:
        due_sug["key"] = "PA6"
        suggestions.append(due_sug)

    # Ensure '+ ' prefix and cap length
    for sug in suggestions:
        meta = sug.get("meta") if isinstance(sug.get("meta"), dict) else {}
        lines = meta.get("content_lines") if isinstance(meta.get("content_lines"), list) else []
        cleaned: List[str] = []
        for ln in lines:
            s = str(ln or "").strip()
            if not s:
                continue
            if not s.startswith("+"):
                s = "+ " + s.lstrip("-•* ")
            if not s.startswith("+ "):
                s = "+ " + s[1:].lstrip()
            cleaned.append(" ".join(s.split()))
        meta["content_lines"] = cleaned[:24]
        sug["meta"] = meta
        # keep description coherent
        sug["description"] = "; ".join(meta["content_lines"])

    return suggestions


def _is_need_due_date_extension(problem_rows: Sequence[Dict[str, Any]]) -> bool:
    try:
        for r in problem_rows or []:
            if not isinstance(r, dict):
                continue
            q = r.get("qty") if isinstance(r.get("qty"), dict) else {}
            if _safe_float(q.get("unplanned")) > 0 or _safe_float(q.get("late")) > 0:
                return True
        return False
    except Exception:
        return False


def _add_days_skip_sunday(d0: date, days: int) -> date:
    cur = d0
    n = int(max(0, days))
    while n > 0:
        cur = cur + timedelta(days=1)
        if cur.weekday() == 6:
            continue
        n -= 1
    return cur


def _pick_row_with_max_shortage(problem_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any] | None:
    best: Tuple[float, Dict[str, Any]] | None = None
    for r in problem_rows or []:
        if not isinstance(r, dict):
            continue
        q = r.get("qty") if isinstance(r.get("qty"), dict) else {}
        shortage = max(0.0, _safe_float(q.get("late")) + _safe_float(q.get("unplanned")))
        if best is None or shortage > best[0]:
            best = (shortage, r)
    return best[1] if best else None


def _estimate_new_due_for_row(r: Dict[str, Any]) -> str:
    """Estimate a feasible due date by spreading shortage across effective daily capacity."""

    try:
        ws, we, due = _pick_dates(r)
        due_d = date.fromisoformat(str(due)[:10]) if due and due != "không đủ dữ liệu" else None
        we_d = date.fromisoformat(str(we)[:10]) if we and we != "không đủ dữ liệu" else None
        base = due_d or we_d
        if base is None:
            return "không đủ dữ liệu"

        q = r.get("qty") if isinstance(r.get("qty"), dict) else {}
        shortage = max(0.0, _safe_float(q.get("late")) + _safe_float(q.get("unplanned")))
        if shortage <= 0:
            return base.isoformat()

        cap = r.get("capacity") if isinstance(r.get("capacity"), dict) else {}
        cap_norm = int(max(0, int(cap.get("normal_per_day") or 0)))
        cap_ot = int(max(0, int(cap.get("ot_per_day") or 0)))
        eff = cap_norm + cap_ot
        if eff <= 0:
            # fallback: at least suggest a small extension, but avoid arbitrary large numbers
            return _add_days_skip_sunday(base, 2).isoformat()

        days_needed = int(ceil(float(shortage) / float(eff)))
        days_needed = max(1, days_needed)
        return _add_days_skip_sunday(base, days_needed).isoformat()
    except Exception:
        return "không đủ dữ liệu"


def _estimate_optimal_due_from_rows(problem_rows: Sequence[Dict[str, Any]]) -> str:
    candidates: List[date] = []
    for r in problem_rows or []:
        if not isinstance(r, dict):
            continue
        q = r.get("qty") if isinstance(r.get("qty"), dict) else {}
        shortage = max(0.0, _safe_float(q.get("late")) + _safe_float(q.get("unplanned")))
        if shortage <= 0:
            continue

        _ws, we, due = _pick_dates(r)
        due_d = date.fromisoformat(str(due)[:10]) if due and due != "không đủ dữ liệu" else None
        we_d = date.fromisoformat(str(we)[:10]) if we and we != "không đủ dữ liệu" else None
        base = max([x for x in (due_d, we_d) if x is not None], default=None)
        if base is None:
            continue

        cap = r.get("capacity") if isinstance(r.get("capacity"), dict) else {}
        cap_norm = int(max(0, int(cap.get("normal_per_day") or 0)))
        cap_ot = int(max(0, int(cap.get("ot_per_day") or 0)))
        eff = cap_norm + cap_ot
        if eff <= 0:
            candidates.append(base)
            continue

        extra_days = max(0, int(ceil(float(shortage) / float(eff))) - 1)
        candidates.append(_add_days_skip_sunday(base, extra_days))

    if candidates:
        return max(candidates).isoformat()

    primary = _pick_row_with_max_shortage(problem_rows) or _pick_best_row(problem_rows) or {}
    return _estimate_new_due_for_row(primary) if primary else "không đủ dữ liệu"


def _build_due_date_extension_suggestion(problem_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Build Template 6 suggestion when late/unplanned remain in the plan."""

    try:
        if not _is_need_due_date_extension(problem_rows):
            return None

        primary = _pick_row_with_max_shortage(problem_rows) or _pick_best_row(problem_rows) or {}
        if not isinstance(primary, dict) or not primary:
            return None

        tp = primary.get("tp") if isinstance(primary.get("tp"), dict) else {}
        tp_label = _fmt_code_name(tp.get("code"), tp.get("name"))
        _ws, _we, due = _pick_dates(primary)
        new_due = _estimate_optimal_due_from_rows(problem_rows)

        q = primary.get("qty") if isinstance(primary.get("qty"), dict) else {}
        shortage = int(round(max(0.0, _safe_float(q.get("late")) + _safe_float(q.get("unplanned")))))

        # If blocked by predecessor, mention the blocker step to make this actionable.
        blocker_txt = ""
        try:
            dep = primary.get("dependency") if isinstance(primary.get("dependency"), dict) else {}
            if bool(dep.get("is_blocked_by_predecessor")) and isinstance(dep.get("blocked_by"), dict):
                bb = dep.get("blocked_by") or {}
                bb_cd = bb.get("ma_cong_doan")
                if bb_cd:
                    blocker_txt = f" (ưu tiên xử lý công đoạn gây chặn {str(bb_cd)})"
        except Exception:
            blocker_txt = ""

        content_6 = [
            f"+ Thành phẩm {tp_label}, đã cân nhắc các biện pháp (tăng ca/bổ sung nguồn lực/điều nhân sự/làm Chủ nhật) nhưng vẫn còn thiếu {shortage} chi tiết trước ngày giao {due or '[Ngày giao hàng]'}."
            f"{blocker_txt}"
        ]
        content_6.append(
            f"+ Đề xuất điều chỉnh lịch giao hàng sang {new_due} để có đủ thời gian hoàn tất sản xuất và tránh phát sinh trễ dây chuyền."
        )

        return {
            "title": "Điều chỉnh lại lịch giao hàng",
            "description": "; ".join([str(x) for x in content_6 if str(x).strip()]),
            "meta": {
                "priority": "P6",
                "template_no": 6,
                "can_apply": False,
                "content_lines": content_6,
                "moves": [
                    {
                        "type": "REQUEST_DUE_DATE_EXTENSION",
                        "reason": "Không đủ khung thời gian/năng lực để đáp ứng hạn giao hiện tại",
                        "expected_impact": {"late_ops_delta": 0, "tardiness_minutes_delta": 0, "setup_minutes_delta": 0},
                    }
                ],
            },
        }
    except Exception:
        return None


def _build_business_context_from_calendar(calendar: Dict[str, Any]) -> Dict[str, Any]:
    """Extract compact business rows for deterministic analysis."""

    try:
        header = calendar.get("Header") or {}
        rows = calendar.get("Rows") or []
        days = calendar.get("Days") or []
        plan_code = header.get("MaKeHoach") or header.get("KeHoachID")

        def _row_key(r: Dict[str, Any]) -> tuple:
            return (
                _id_token(r.get("DonHangID"), "0"),
                str(r.get("LineKey") or ""),
                _id_token(r.get("TP_DinhMucID"), "0"),
                _id_token(r.get("BTP_DinhMucID"), "0"),
                _id_token(r.get("ThuTuSX"), "0"),
                str(r.get("MaCongDoan") or ""),
            )

        def _build_op_id(
            *,
            plan_id: Any,
            don_hang_id: Any,
            line_key: Any,
            tp_id: Any,
            btp_id: Any,
            ma_cd: Any,
            thu_tu_sx: Any,
            machine: Any | None = None,
        ) -> str:
            # Must match KeHoachPlanBuilder op_id format (machine token optional).
            lk_enc = quote(str(line_key or ""), safe="")
            base = (
                f"KH{_id_token(plan_id, '0')}::DH{_id_token(don_hang_id, '0')}::LK{lk_enc}"
                f"::TP{_id_token(tp_id, '0')}::BTP{_id_token(btp_id, '0')}::CD{str(ma_cd or '')}"
                f"::T{_id_token(thu_tu_sx, '0')}"
            )
            if machine not in (None, ""):
                return base + f"::M{str(machine)}"
            return base

        row_map: Dict[tuple, Dict[str, Any]] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            row_map[_row_key(r)] = r

        prob: List[Tuple[float, Dict[str, Any]]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            late_qty = _safe_float(r.get("LateQty"))
            unplanned = _safe_float(r.get("UnplannedQty"))
            overtime = _safe_float(r.get("OvertimeQty"))
            overcap = _safe_float(r.get("OverCapacityQty"))
            if late_qty > 0 or unplanned > 0 or overtime > 0 or overcap > 0:
                score = late_qty * 10 + unplanned * 8 + overtime * 3 + overcap
                prob.append((score, r))

                # If this row is blocked by predecessor, also include its blocker step for root-cause suggestions.
                try:
                    if bool(r.get("IsBlockedByPredecessor")) and isinstance(r.get("BlockedBy"), dict):
                        bb = r.get("BlockedBy") or {}
                        bb_key = (
                            _id_token(r.get("DonHangID"), "0"),
                            str(r.get("LineKey") or ""),
                            _id_token(r.get("TP_DinhMucID"), "0"),
                            _id_token(bb.get("btp_dinh_muc_id"), "0"),
                            _id_token(bb.get("thu_tu_sx"), "0"),
                            str(bb.get("ma_cong_doan") or ""),
                        )
                        bb_row = row_map.get(bb_key)
                        if isinstance(bb_row, dict):
                            prob.append((score + 0.5, bb_row))
                except Exception:
                    pass

        if not prob:
            for r in rows:
                if not isinstance(r, dict):
                    continue
                has_business_keys = bool(r.get("MaCongDoanLon") or r.get("MaCongDoan") or r.get("MaTP") or r.get("MaBTP"))
                if not has_business_keys:
                    continue
                score = _safe_float(r.get("TotalQty"))
                prob.append((score, r))

        prob.sort(key=lambda x: x[0], reverse=True)
        top_rows = [r for _, r in prob[:12]]

        summarized: List[Dict[str, Any]] = []
        for r in top_rows:
            cap = _safe_float(r.get("ProductionCapacity") or r.get("CapacityPerDay") or r.get("NangLucSanXuat"))
            ot_cap = _safe_float(r.get("OvertimeCapacity") or r.get("OvertimeCapacityPerDay") or r.get("NangLucTangCa"))
            overtime_qty = _safe_float(r.get("OvertimeQty"))
            late_qty = _safe_float(r.get("LateQty"))
            unplanned_qty = _safe_float(r.get("UnplannedQty"))
            total_qty = _safe_float(r.get("TotalQty"))

            planned_total = _safe_float(r.get("PlannedTotal"))
            if planned_total <= 0 and isinstance(r.get("DailyQty"), list):
                planned_total = sum(_safe_float((x or {}).get("Quantity")) for x in (r.get("DailyQty") or []) if isinstance(x, dict))

            # Respect explicit LateQty/UnplannedQty from input; only fallback when missing/zero and plan is incomplete.
            if late_qty <= 0 and unplanned_qty <= 0 and total_qty > 0 and planned_total > 0 and planned_total < total_qty:
                unplanned_qty = max(0.0, total_qty - planned_total)

            dinh_muc_min = _safe_float(r.get("DinhMucThoiGian"), 0.0)

            ot_hours = None
            if overtime_qty > 0 and ot_cap > 0:
                ot_hours = round(4.0 * overtime_qty / float(ot_cap), 2)
            elif (late_qty + unplanned_qty) > 0 and ot_cap > 0:
                ot_hours = round(4.0 * (late_qty + unplanned_qty) / float(ot_cap), 2)

            dept_staff = None
            try:
                dept_staff_raw = r.get("SoNhanSuBoPhan")
                if dept_staff_raw in (None, ""):
                    dept_staff_raw = r.get("SoLuongNguonLuc")
                if dept_staff_raw not in (None, ""):
                    dept_staff = max(0, int(float(dept_staff_raw)))
            except Exception:
                dept_staff = None

            ot_staff_max = dept_staff
            ot_staff_recommended = 0
            try:
                if overtime_qty > 0 and dinh_muc_min > 0:
                    ot_staff_recommended = max(0, int(ceil((overtime_qty * dinh_muc_min) / (4.0 * 60.0))))
                    if dept_staff is not None:
                        ot_staff_recommended = min(int(dept_staff), int(ot_staff_recommended))
                elif overtime_qty > 0 and dept_staff is not None:
                    ot_staff_recommended = min(int(dept_staff), 1)
                else:
                    ot_staff_recommended = 0
            except Exception:
                ot_staff_recommended = 0

            outsource_staff_recommended = 0
            outsource_ot_staff_recommended = 0
            try:
                supplement_qty = max(0.0, late_qty + unplanned_qty)
                if supplement_qty > 0 and dinh_muc_min > 0:
                    outsource_staff_recommended = min(
                        OUTSOURCE_STAFF_MAX,
                        max(0, int(ceil((supplement_qty * dinh_muc_min) / (8.0 * 60.0)))),
                    )
                elif supplement_qty > 0:
                    outsource_staff_recommended = 1
                else:
                    outsource_staff_recommended = 0

                if overtime_qty > 0 and dinh_muc_min > 0:
                    outsource_ot_staff_recommended = min(
                        OUTSOURCE_STAFF_MAX,
                        max(0, int(ceil((overtime_qty * dinh_muc_min) / (4.0 * 60.0)))),
                    )
                elif overtime_qty > 0:
                    outsource_ot_staff_recommended = 1
                else:
                    outsource_ot_staff_recommended = 0
            except Exception:
                outsource_staff_recommended = 0
                outsource_ot_staff_recommended = 0

            qty_need = max(0.0, late_qty + unplanned_qty)
            if qty_need <= 0:
                qty_need = max(unplanned_qty, late_qty, overtime_qty, total_qty, planned_total)

            due = r.get("DueDT")
            due_date = None
            try:
                due_date = due.date().isoformat() if hasattr(due, "date") else str(due)[:10]
            except Exception:
                due_date = str(due)[:10] if due else None

            plan_ws = r.get("PlanWindowStart")
            plan_we = r.get("PlanWindowEnd")
            ws = str(plan_ws)[:10] if plan_ws is not None else (str(header.get("TuNgay") or "")[:10] or None)
            we = str(plan_we)[:10] if plan_we is not None else (str(header.get("DenNgay") or "")[:10] or None)

            resource_code_raw = r.get("MaNguonLuc")
            resource_name_raw = r.get("NguonLucText") or r.get("TenNguonLuc")
            code_list = _split_csv(resource_code_raw)
            name_list = _split_csv(resource_name_raw)

            resource_type = "unknown"
            if code_list and all(str(c).upper() == "M000" for c in code_list):
                resource_type = "labor"
            elif code_list:
                resource_type = "machine"
            elif str(resource_code_raw or "").upper() == "M000":
                resource_type = "labor"
            elif resource_name_raw:
                resource_type = "machine"

            summarized.append(
                {
                    "key": str(_row_key(r)),
                    "plan_code": plan_code,
                    "don_hang_id": r.get("DonHangID"),
                    "so_don_hang": r.get("SoChungTu") or r.get("SoDonHang"),
                    "khach_hang": r.get("KhachHang"),
                    "op": {
                        "plan_id": header.get("KeHoachID"),
                        "don_hang_id": r.get("DonHangID"),
                        "line_key": r.get("LineKey"),
                        "tp_dinh_muc_id": r.get("TP_DinhMucID"),
                        "btp_dinh_muc_id": r.get("BTP_DinhMucID"),
                        "thu_tu_sx": r.get("ThuTuSX"),
                        "ma_cong_doan": r.get("MaCongDoan"),
                        "op_id": None,
                    },
                    "tp": {
                        "id": r.get("TP_DinhMucID"),
                        "code": r.get("MaTP") or r.get("TP_DinhMucID"),
                        "name": r.get("TenThanhPham"),
                    },
                    "btp": {
                        "id": r.get("BTP_DinhMucID"),
                        "code": r.get("MaBTP") or r.get("BTP_DinhMucID"),
                        "name": r.get("TenBanThanhPham"),
                    },
                    "dept": {"code": r.get("MaCongDoanLon"), "name": r.get("TenBoPhan")},
                    "step": {"code": r.get("MaCongDoan"), "name": r.get("TenCongDoan")},
                    "resource": {
                        "count": r.get("SoLuongNguonLuc"),
                        "code": ", ".join(code_list) if code_list else resource_code_raw,
                        "name": ", ".join(name_list) if name_list else resource_name_raw,
                        "type": resource_type,
                    },
                    "dependency": {
                        "is_blocked_by_predecessor": bool(r.get("IsBlockedByPredecessor")),
                        "blocked_by": r.get("BlockedBy"),
                        "blocked_by_op_id": None,
                        "blocked_by_op_ids": [],
                        "reason": r.get("BlockedReason"),
                    },
                    "workforce": {
                        "dept_staff": dept_staff,
                        "regular_staff_required": dept_staff,
                        "ot_staff_max": ot_staff_max,
                        "ot_staff_recommended": ot_staff_recommended,
                        "outsource_staff_max": OUTSOURCE_STAFF_MAX,
                        "outsource_staff_recommended": outsource_staff_recommended,
                        "outsource_ot_staff_recommended": outsource_ot_staff_recommended,
                        "rules": {
                            "ot_staff_max_equals_dept_staff": True,
                            "outsource_staff_max": OUTSOURCE_STAFF_MAX,
                        },
                    },
                    "qty": {
                        "need": qty_need,
                        "total": r.get("TotalQty"),
                        "planned_total": planned_total,
                        "done_total": planned_total,
                        "late": r.get("LateQty"),
                        "unplanned": r.get("UnplannedQty"),
                        "overtime": r.get("OvertimeQty"),
                        "over_capacity": r.get("OverCapacityQty"),
                    },
                    "capacity": {
                        "normal_per_day": int(cap) if cap else 0,
                        "ot_per_day": int(ot_cap) if ot_cap else 0,
                        "ot_hours_needed": ot_hours,
                        "norm_time_min": float(r.get("DinhMucThoiGian") or 0)
                        if r.get("DinhMucThoiGian") not in (None, "")
                        else None,
                    },
                    "dates": {
                        "window_start": ws,
                        "window_end": we,
                        "due": due_date,
                    },
                }
            )

            # Fill op_id and blocked_by_op_id(s) best-effort.
            try:
                op_d = summarized[-1].get("op") if isinstance(summarized[-1].get("op"), dict) else {}
                pid_raw = header.get("KeHoachID") if header.get("KeHoachID") not in (None, "") else plan_code
                dh_raw = r.get("DonHangID")
                tp_raw = r.get("TP_DinhMucID")
                btp_raw = r.get("BTP_DinhMucID")
                tts_raw = r.get("ThuTuSX")
                cd = str(r.get("MaCongDoan") or "")
                lk = str(r.get("LineKey") or "")

                if lk and cd:
                    op_d["op_id"] = _build_op_id(
                        plan_id=pid_raw,
                        don_hang_id=dh_raw,
                        line_key=lk,
                        tp_id=tp_raw,
                        btp_id=btp_raw,
                        ma_cd=cd,
                        thu_tu_sx=tts_raw,
                        machine=None,
                    )

                dep_d = summarized[-1].get("dependency") if isinstance(summarized[-1].get("dependency"), dict) else {}
                bb = r.get("BlockedBy") if isinstance(r.get("BlockedBy"), dict) else None
                if lk and isinstance(bb, dict):
                    bb_tts = bb.get("thu_tu_sx")
                    bb_btp_id = bb.get("btp_dinh_muc_id")
                    bb_cd = str(bb.get("ma_cong_doan") or "")
                    if bb_tts not in (None, "") and bb_btp_id not in (None, "") and bb_cd:
                        dep_d["blocked_by_op_id"] = _build_op_id(
                            plan_id=pid_raw,
                            don_hang_id=r.get("DonHangID"),
                            line_key=lk,
                            tp_id=r.get("TP_DinhMucID"),
                            btp_id=bb_btp_id,
                            ma_cd=bb_cd,
                            thu_tu_sx=bb_tts,
                            machine=None,
                        )

                        # If blocker step exists across multiple machines in calendar, surface all candidates.
                        op_ids: List[str] = []
                        for k, rr in row_map.items():
                            try:
                                if (
                                    _id_token(rr.get("DonHangID"), "0") == _id_token(r.get("DonHangID"), "0")
                                    and str(rr.get("LineKey") or "") == lk
                                    and _id_token(rr.get("TP_DinhMucID"), "0") == _id_token(r.get("TP_DinhMucID"), "0")
                                    and _id_token(rr.get("BTP_DinhMucID"), "0") == _id_token(bb_btp_id, "0")
                                    and _id_token(rr.get("ThuTuSX"), "0") == _id_token(bb_tts, "0")
                                    and str(rr.get("MaCongDoan") or "") == bb_cd
                                ):
                                    op_ids.append(
                                        _build_op_id(
                                            plan_id=pid_raw,
                                            don_hang_id=rr.get("DonHangID"),
                                            line_key=lk,
                                            tp_id=rr.get("TP_DinhMucID"),
                                            btp_id=bb_btp_id,
                                            ma_cd=bb_cd,
                                            thu_tu_sx=bb_tts,
                                            machine=rr.get("MaNguonLuc"),
                                        )
                                    )
                            except Exception:
                                continue

                        # Unique + cap to keep payload small
                        seen = set()
                        uniq = []
                        for oid in op_ids:
                            if oid in seen:
                                continue
                            seen.add(oid)
                            uniq.append(oid)
                        dep_d["blocked_by_op_ids"] = uniq[:6]
            except Exception:
                pass

        return {
            "plan": {
                "id": header.get("KeHoachID"),
                "code": plan_code,
                "from": str(header.get("TuNgay") or "")[:10] or None,
                "to": str(header.get("DenNgay") or "")[:10] or None,
                "days": [str(d)[:10] for d in days[:31]] if isinstance(days, list) else [],
            },
            "problem_rows": summarized,
            "template_priority": [1, 2, 3, 4, 5, 6],
        }
    except Exception:
        return {"problem_rows": [], "template_priority": [1, 2, 3, 4, 5, 6]}


def _enrich_calendar_department_staff(db: Session, calendar: Dict[str, Any]) -> None:
    """Backfill SoNhanSuBoPhan from master data when calendar rows miss it."""

    try:
        rows = (calendar or {}).get("Rows") or []
        if not isinstance(rows, list) or not rows:
            return

        dept_codes: List[str] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            code = r.get("MaCongDoanLon")
            if not code:
                continue
            if r.get("SoNhanSuBoPhan") in (None, ""):
                dept_codes.append(str(code))

        dept_codes = sorted(set(dept_codes))
        if not dept_codes:
            return

        staff_map: Dict[str, int] = {}
        q = db.query(DM_CongDoanLon).filter(DM_CongDoanLon.MaCongDoanLon.in_(dept_codes)).all()
        for d in q:
            try:
                staff_map[str(d.MaCongDoanLon)] = int(float(d.SoNhanSu or 0))
            except Exception:
                staff_map[str(d.MaCongDoanLon)] = 0

        for r in rows:
            if not isinstance(r, dict):
                continue
            code = r.get("MaCongDoanLon")
            if not code:
                continue
            if r.get("SoNhanSuBoPhan") in (None, ""):
                r["SoNhanSuBoPhan"] = staff_map.get(str(code))
    except Exception:
        return


def _parse_op_id_for_db(op_id: str) -> Dict[str, Any] | None:
    """Parse op_id from KeHoachPlanBuilder back to DB key fields."""

    try:
        if not op_id or "::" not in op_id:
            return None
        parts = str(op_id).split("::")
        out: Dict[str, Any] = {}
        for p in parts:
            if p.startswith("DH"):
                out["DonHangID"] = int(p[2:]) if p[2:] else None
            elif p.startswith("LK"):
                out["LineKey"] = unquote(p[2:])
            elif p.startswith("TP"):
                out["TP_DinhMucID"] = int(p[2:]) if p[2:] else None
            elif p.startswith("BTP"):
                out["BTP_DinhMucID"] = int(p[3:]) if p[3:] else None
            elif p.startswith("CD"):
                out["MaCongDoan"] = p[2:]
            elif p.startswith("T") and not p.startswith("TP"):
                try:
                    out["ThuTuSX"] = int(p[1:]) if p[1:] else None
                except Exception:
                    pass
        return out if out else None
    except Exception:
        return None

