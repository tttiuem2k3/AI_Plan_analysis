from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional
import time

# add for debug save
import json
import os

from sqlalchemy.orm import Session

from .dtos import KPIResult, Move, Plan, PlanOption
from .kpi import compute_kpi, find_hotspots
from .moves import apply_moves
from .openai_client import OpenAIClientError, call_gpt
from .prompt_builder import build_payload, build_system_prompt, build_user_prompt
from .rescheduler import local_reschedule
from .validator import validate_plan
from .calendarizer import calendarize_plan
from .cp_sat_scheduler import cp_sat_reschedule
from ..utils.ai_flow_log import append_ai_flow_log

logger = logging.getLogger(__name__)


# Moves that `apply_moves` can actually mutate/affect in the plan.
EXECUTABLE_MOVE_TYPES: set[str] = {
    "MOVE_TO_ALTERNATE_MACHINE",
    "SWAP_ORDER_ON_MACHINE",
    "BATCH_SAME_SETUP_FAMILY",
    "PULL_FORWARD_CRITICAL_OP",
    "SHIFT_TO_FILL_IDLE_WINDOW",
}


def _coerce_template_no(
    raw_template_no: Any,
    *,
    moves_raw: list[dict],
    goal: str,
    content_lines: list[str],
) -> Optional[int]:
    """Best-effort: map LLM output to strict business templates 1..6.

    We do this so UI always shows correct 1→6 buckets even if the model mislabels.
    """

    try:
        t = int(raw_template_no) if raw_template_no is not None else None
    except Exception:
        t = None

    # Preserve explicit template number from LLM when valid.
    # Heuristic keyword mapping is only for missing/invalid template_no.
    if t is not None and 1 <= t <= 6:
        return t

    text = " ".join([goal or ""] + (content_lines or []))
    text_l = text.lower()

    move_types: set[str] = set()
    try:
        for m in moves_raw or []:
            if isinstance(m, dict) and m.get("type"):
                move_types.add(str(m.get("type")))
    except Exception:
        move_types = set()

    # Hard signals from move types
    if "REQUEST_DUE_DATE_EXTENSION" in move_types:
        return 6

    # Sunday / day-off work
    if any(k in text_l for k in ("chủ nhật", "chu nhat", "ngày nghỉ", "ngay nghi")):
        return 5

    # Machine-related automation (swap/move machine)
    if any(mt in move_types for mt in ("MOVE_TO_ALTERNATE_MACHINE", "SWAP_ORDER_ON_MACHINE")):
        return 2

    # Overtime/add shift
    if "INCREASE_OT_OR_ADD_SHIFT" in move_types or any(k in text_l for k in ("tăng ca", "tang ca", "thêm ca", "them ca")):
        return 1

    # Staffing / outsource / temp labor
    if "HIRE_OUTSOURCE_LABOR" in move_types or any(k in text_l for k in ("thuê", "thue", "thời vụ", "thoi vu", "điều chuyển", "dieu chuyen", "outsource", "thuê ngoài", "thue ngoai")):
        # Try distinguish 3 vs 4 by keyword.
        if any(k in text_l for k in ("điều chuyển", "dieu chuyen")):
            return 3
        return 4

    return None


def _env_flag(name: str, default: bool = False) -> bool:
    v = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _active_llm_provider() -> str:
    v = str(os.getenv("LLM_PROVIDER") or "openai").strip().lower()
    return v if v in ("openai", "gemini", "openrouter") else "openai"


def _active_llm_model_name() -> str:
    provider = _active_llm_provider()
    if provider == "gemini":
        return str(os.getenv("GEMINI_MODEL") or "gemini-2.5-flash")
    if provider == "openrouter":
        return str(os.getenv("OPENROUTER_MODEL") or "qwen/qwen3.6-plus")
    return str(os.getenv("OPENAI_MODEL") or "gpt-4.1")


def _active_llm_max_retries(default_openai: int = 2, default_gemini: int = 2) -> int:
    provider = _active_llm_provider()
    if provider == "gemini":
        try:
            return int(os.getenv("GEMINI_MAX_RETRIES") or default_gemini)
        except Exception:
            return int(default_gemini)
    if provider == "openrouter":
        try:
            return int(os.getenv("OPENROUTER_MAX_RETRIES") or default_openai)
        except Exception:
            return int(default_openai)
    try:
        return int(os.getenv("OPENAI_MAX_RETRIES") or default_openai)
    except Exception:
        return int(default_openai)


def _limit_validation_errors(errors: list[dict], max_items: int) -> list[dict]:
    try:
        if not isinstance(errors, list):
            return []
        return errors[: max(0, int(max_items or 0))]
    except Exception:
        return []


def _compact_validation_error_item(err: dict) -> dict:
    """Keep only business-readable fields from a validation error for LLM prompts."""

    if not isinstance(err, dict):
        return {}

    out: Dict[str, Any] = {
        "code": err.get("code"),
        "message": err.get("message"),
    }

    ctx = err.get("context")
    if not isinstance(ctx, dict):
        return out

    compact_ctx: Dict[str, Any] = {}
    for k in ("ma_cong_doan_lon", "ten_bo_phan", "ma_cong_doan", "ten_cong_doan"):
        if ctx.get(k) not in (None, ""):
            compact_ctx[k] = ctx.get(k)

    def _pick_side(side: Any) -> Dict[str, Any]:
        if not isinstance(side, dict):
            return {}
        picked: Dict[str, Any] = {}
        for kk in (
            "op_id",
            "so_don_hang",
            "ten_thanh_pham",
            "ten_ban_thanh_pham",
            "ma_cong_doan",
            "ten_cong_doan",
            "ma_cong_doan_lon",
            "ten_bo_phan",
            "machine",
            "start",
            "end",
        ):
            if side.get(kk) not in (None, ""):
                picked[kk] = side.get(kk)
        return picked

    prev_c = _pick_side(ctx.get("prev"))
    cur_c = _pick_side(ctx.get("cur"))
    if prev_c:
        compact_ctx["prev"] = prev_c
    if cur_c:
        compact_ctx["cur"] = cur_c

    if compact_ctx:
        out["context"] = compact_ctx
    return out


def _compact_validation_errors_for_llm(errors: list[dict], max_items: int) -> list[dict]:
    """Slice + compact validation errors to reduce prompt tokens."""

    compacted = []
    for e in _limit_validation_errors(errors, max_items):
        compacted.append(_compact_validation_error_item(e))
    return compacted


def _build_validation_explain_prompt(plan_id: int, option_id: str, errors: list[dict]) -> str:
    compact_errors = _compact_validation_errors_for_llm(
        errors if isinstance(errors, list) else [],
        int(os.getenv("AI_EXPLAIN_VALIDATION_MAX_ERRORS", "80") or "80"),
    )
    return (
        "Bạn là trợ lý giải thích vấn đề của kế hoạch sản xuất cho người dùng nghiệp vụ.\n"
        "Bạn sẽ nhận danh sách lỗi (JSON) từ hệ thống kiểm tra lịch.\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "- TUYỆT ĐỐI KHÔNG dùng mã lỗi kỹ thuật (ví dụ: DEPT_BUSY_CONFLICT, DEPT_OVERLAP, PRECEDENCE_VIOLATION).\n"
        "- Không nhắc đến 'validator', 'constraint'.\n"
        "- KHÔNG đưa ra gợi ý theo mã máy dạng M### (ví dụ M011, M012). Nếu cần nhắc, hãy nói theo 'nguồn lực/máy' chung chung hoặc theo TÊN bộ phận/công đoạn.\n"
        "- PHẢI ưu tiên hiển thị TÊN rõ ràng nếu có trong context: ten_bo_phan/tenCongDoanLon/TenBoPhan, ten_cong_doan/TenCongDoan, ten_ban_thanh_pham/TenBanThanhPham, ten_thanh_pham/TenThanhPham, so_don_hang/SoDonHang.\n"
        "- Khi nhắc đến 'công đoạn', hãy dùng TÊN công đoạn (TenCongDoan/ten_cong_doan). KHÔNG hiển thị mã như CD010/CD077 trừ khi KHÔNG có tên; nếu thiếu tên thì viết 'Công đoạn <mã> (chưa có tên)'.\n"
        "- Khi nhắc đến 'kế hoạch khác', hãy dùng MaKeHoach nếu có (meta.busy.ma_ke_hoach hoặc MaKeHoach). Nếu không có thì mới dùng ke_hoach_id.\n"
        "- Nếu là xung đột với kế hoạch khác thì phải ghi rõ 'đang bận theo kế hoạch <MaKeHoach>'.\n"
        "- Mỗi issue phải nêu rõ: bộ phận nào, công đoạn nào, bán thành phẩm/thành phẩm nào (nếu có), đơn hàng nào (nếu có), khung thời gian, và nguyên nhân dễ hiểu.\n"
        "- Chỉ dựa trên dữ liệu có trong input. Thiếu trường thì ghi 'không đủ dữ liệu'.\n\n"
        "Output JSON thuần theo schema (đúng key):\n"
        "{\n"
        '  \"summary\":\"...\",\n'
        '  \"issues\":[\n'
        '    {\"title\":\"...\",\"detail\":\"...\",\"where\":[{\"bo_phan\":\"...\",\"cong_doan\":\"...\",\"ban_thanh_pham\":\"...\",\"thanh_pham\":\"...\",\"don_hang\":\"...\",\"ke_hoach_lien_quan\":\"...\",\"thoi_gian\":\"...\"}]}\n'
        "  ],\n"
        '  \"recommendations\":[{\"action\":\"...\",\"why\":\"...\",\"expected_effect\":\"...\"}]\n'
        "}\n\n"
        f"PlanId={int(plan_id)}; Option={option_id}.\n"
        "Validation errors (JSON):\n"
        + json.dumps(compact_errors, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _group_validation_errors_for_ui(errs: list[dict], max_per_code: int = 10) -> dict:
    out: dict = {}
    if not isinstance(errs, list):
        return out
    for e in errs:
        if not isinstance(e, dict):
            continue
        code = str(e.get("code") or "UNKNOWN")
        out.setdefault(code, [])
        if len(out[code]) >= max_per_code:
            continue
        out[code].append(e)
    return out


def _compact_hotspots_for_llm(hotspots: Any) -> Any:
    """Keep only top hotspots to reduce tokens.

    `find_hotspots` can return fairly verbose structures. We keep a small subset.
    """

    try:
        if not isinstance(hotspots, dict):
            return hotspots

        keep_n = int(os.getenv("AI_HOTSPOTS_MAX", "12") or "12")
        keep_n = max(5, min(50, keep_n))

        out: dict[str, Any] = {}
        for k, v in hotspots.items():
            if k == "bottleneck_ops" and isinstance(v, list):
                compact_ops = []
                for op in v[:keep_n]:
                    if not isinstance(op, dict):
                        continue
                    compact_ops.append(
                        {
                            "op_id": op.get("op_id"),
                            "machine": op.get("machine"),
                            "dept": op.get("dept") or op.get("ten_bo_phan"),
                            "ten_cong_doan": op.get("ten_cong_doan"),
                            "ten_thanh_pham": op.get("ten_thanh_pham"),
                            "ten_ban_thanh_pham": op.get("ten_ban_thanh_pham"),
                            "qty": op.get("qty"),
                        }
                    )
                out[k] = compact_ops
                continue

            if isinstance(v, list):
                out[k] = v[:keep_n]
            else:
                out[k] = v
        return out
    except Exception:
        return hotspots


def _compact_kpi_for_llm(kpi_snapshot: Any) -> Any:
    """Keep only KPI fields model needs for decision making."""

    try:
        if not isinstance(kpi_snapshot, dict):
            return kpi_snapshot

        allow = {
            "late_ops",
            "total_tardiness_minutes",
            "total_setup_minutes",
            "makespan_minutes",
            "utilization",
        }
        return {k: kpi_snapshot.get(k) for k in allow if k in kpi_snapshot}
    except Exception:
        return kpi_snapshot


def _compact_payload_for_llm(payload: dict) -> dict:
    """Reduce payload size to avoid LLM timeouts.

    Biggest offender is usually constraints.calendar.busy_intervals.
    We'll keep only a small, representative sample.
    """

    try:
        if not isinstance(payload, dict):
            return payload

        p = json.loads(json.dumps(payload, ensure_ascii=False, default=str))

        # ---- compact kpi/hotspots first ----
        if "kpi_snapshot" in p:
            p["kpi_snapshot"] = _compact_kpi_for_llm(p.get("kpi_snapshot"))
        if "hotspots" in p:
            p["hotspots"] = _compact_hotspots_for_llm(p.get("hotspots"))

        constraints = (p.get("constraints") or {})
        cal_wrap = constraints.get("calendar")
        # payload currently is {constraints: {calendar: {calendar: ... , capacity: ...}}, ...}
        # Be defensive on shapes.
        if isinstance(cal_wrap, dict) and "calendar" in cal_wrap:
            cal = cal_wrap.get("calendar")
        else:
            cal = cal_wrap
        if isinstance(cal, dict):
            bi = cal.get("busy_intervals")
            # Compact earlier (busy intervals are very token-heavy)
            if isinstance(bi, list) and len(bi) > 80:
                keep = int(os.getenv("AI_BUSY_INTERVALS_MAX", "120") or "120")
                keep = max(20, min(600, keep))
                cal["busy_intervals"] = bi[:keep]

        return p
    except Exception:
        return payload


def _safe_first_segment_start(op: Any) -> Optional[datetime]:
    try:
        segs = getattr(op, "segments", None) or []
        starts = [getattr(s, "start", None) for s in segs if getattr(s, "start", None) is not None]
        return min(starts) if starts else None
    except Exception:
        return None


def _derive_swap_moves_for_machines(plan: Plan, machines: List[str], *, max_ops_per_machine: int = 18) -> List[Dict[str, Any]]:
    """Derive compact SWAP_ORDER_ON_MACHINE suggestions from a scheduled plan.

    We only include a prefix of op_ids to keep the move compact; CP-SAT rescheduler
    interprets machine_sequences as a soft/partial intent (filtered on known ops).
    """

    out: List[Dict[str, Any]] = []
    try:
        max_ops_per_machine = int(max_ops_per_machine)
    except Exception:
        max_ops_per_machine = 18
    max_ops_per_machine = max(8, min(60, max_ops_per_machine))

    for m in machines:
        ops = [op for op in (plan.operations or []) if str(getattr(op, "machine", "") or "") == str(m)]
        ops2 = []
        for op in ops:
            st = _safe_first_segment_start(op)
            if st is None:
                continue
            ops2.append((st, str(op.op_id)))
        ops2.sort(key=lambda x: x[0])
        seq = [op_id for _, op_id in ops2][:max_ops_per_machine]
        if len(seq) < 2:
            continue
        out.append(
            {
                "type": "SWAP_ORDER_ON_MACHINE",
                "machine": str(m),
                "op_ids_after": seq,
                "reason": "Gợi ý thứ tự chạy trên nguồn lực bị nghẽn từ CP-SAT (tham khảo để chọn ít thay đổi nhưng giảm nghẽn).",
            }
        )
    return out


def _build_ortools_options_for_llm(plan: Plan, hotspots: Any) -> Dict[str, Any]:
    """Best-effort: run CP-SAT on bottleneck machines and expose compact suggestions to LLM."""

    import os

    machines: List[str] = []
    try:
        if isinstance(hotspots, dict):
            machines = list(hotspots.get("bottleneck_machines") or [])
    except Exception:
        machines = []

    try:
        tl = float(os.getenv("AI_ORTOOLS_SUGGEST_TIME_LIMIT_S", "0.8") or "0.8")
    except Exception:
        tl = 0.8
    tl = max(0.2, min(5.0, tl))

    def _to_ortools_input_log_payload(p: Plan) -> Dict[str, Any]:
        plan_apk = None
        try:
            for op0 in (p.operations or []):
                meta0 = getattr(op0, "meta", None)
                if isinstance(meta0, dict) and meta0.get("plan_apk") not in (None, ""):
                    plan_apk = meta0.get("plan_apk")
                    break
        except Exception:
            plan_apk = None

        due_dates = getattr(p, "due_dates", None) if isinstance(getattr(p, "due_dates", None), dict) else {}
        constraints = getattr(p, "constraints", None) if isinstance(getattr(p, "constraints", None), dict) else {}

        ops_out: List[Dict[str, Any]] = []
        api_detail_rows: List[Dict[str, Any]] = []
        for op in (p.operations or []):
            meta = getattr(op, "meta", None) if isinstance(getattr(op, "meta", None), dict) else {}
            op_id = getattr(op, "op_id", None)
            due_dt = due_dates.get(op_id)
            machine = getattr(op, "machine", None)
            machine_candidates = (meta or {}).get("machines") if isinstance((meta or {}).get("machines"), list) else None
            if machine_candidates:
                resource_ids = ",".join([str(x) for x in machine_candidates if x not in (None, "")])
            else:
                resource_ids = str(machine) if machine not in (None, "") else None

            op_dict: Dict[str, Any] = {
                "op_id": op_id,
                "plan_id": plan_apk if plan_apk not in (None, "") else getattr(op, "plan_id", None),
                "so_don_hang": getattr(op, "so_don_hang", None),
                "CustomerName": (meta or {}).get("customer_name"),
                "tp_id": getattr(op, "tp_id", None),
                "ten_thanh_pham": getattr(op, "ten_thanh_pham", None),
                "btp_id": getattr(op, "btp_id", None),
                "ten_ban_thanh_pham": getattr(op, "ten_ban_thanh_pham", None),
                "thu_tu_sx": getattr(op, "thu_tu_sx", None),
                "ma_cong_doan": getattr(op, "ma_cong_doan", None),
                "ten_cong_doan": getattr(op, "ten_cong_doan", None),
                "ma_cong_doan_lon": getattr(op, "ma_cong_doan_lon", None),
                "ten_bo_phan": getattr(op, "ten_bo_phan", None),
                "machine": getattr(op, "machine", None),
                "loai_nguon_luc": getattr(op, "loai_nguon_luc", None),
                "total_qty": getattr(op, "total_qty", None),
                "dinh_muc_phut_moi_sp": getattr(op, "dinh_muc_phut_moi_sp", None),
                "segments": [asdict(s) for s in (getattr(op, "segments", None) or [])],
                "meta": meta,
            }
            ops_out.append(op_dict)

            api_detail_rows.append(
                {
                    "OrderNo": getattr(op, "so_don_hang", None),
                    "CustomerName": (meta or {}).get("customer_name"),
                    "LineKey": (meta or {}).get("line_key"),
                    "ProductionSequence": getattr(op, "thu_tu_sx", None),
                    "PhaseID": getattr(op, "ma_cong_doan", None),
                    "PhaseName": getattr(op, "ten_cong_doan", None),
                    "PhaseGroupName": getattr(op, "ten_bo_phan", None),
                    "ResourceIDs": resource_ids,
                    "APK_MT2141": getattr(op, "tp_id", None),
                    "FinishedProductName": getattr(op, "ten_thanh_pham", None),
                    "APK_MT2142": getattr(op, "btp_id", None),
                    "SemiFinishedProductName": getattr(op, "ten_ban_thanh_pham", None),
                    "TotalQuantity": getattr(op, "total_qty", None),
                    "TimeLimit": getattr(op, "dinh_muc_phut_moi_sp", None),
                    "StartDate": _safe_first_segment_start(op),
                    "EndDate": max(
                        [getattr(s, "end", None) for s in (getattr(op, "segments", None) or []) if getattr(s, "end", None) is not None],
                        default=None,
                    ),
                    "DueDate": due_dt,
                }
            )

        api_rules: Dict[str, Any] = {
            "DeptExclusive": bool(constraints.get("enforce_dept_no_overlap")),
        }

        work_hours = constraints.get("work_hours") if isinstance(constraints.get("work_hours"), dict) else {}
        if work_hours:
            api_rules["RegularHours"] = work_hours.get("regular_hours")
            api_rules["OtHoursMax"] = work_hours.get("ot_hours_max")
            api_rules["OutsourcePeopleMaxPerDay"] = work_hours.get("outsource_people_max_per_day")

        related_production_plans = constraints.get("related_production_plans")
        if not isinstance(related_production_plans, list):
            related_production_plans = []

        horizon = constraints.get("horizon") if isinstance(constraints.get("horizon"), dict) else {}
        master_apk = plan_apk if plan_apk not in (None, "") else p.plan_id

        return {
            "Rules": api_rules,
            "ProductionPlan": {
                "Master": {
                    "APK": master_apk,
                    "StartManufactering": horizon.get("start", getattr(p, "horizon_start", None)),
                    "EndDateManufactering": horizon.get("end", getattr(p, "horizon_end", None)),
                },
                "Detail": api_detail_rows,
            },
            "RelatedProductionPlans": related_production_plans,
            "SolverInput": {
                "plan": {
                    "plan_id": master_apk,
                    "horizon_start": getattr(p, "horizon_start", None),
                    "horizon_end": getattr(p, "horizon_end", None),
                    "operations": ops_out,
                    "constraints": constraints,
                    "capacity": getattr(p, "capacity", None),
                    "due_dates": due_dates,
                },
                "affected_scope": {"machines": machines},
                "time_limit_s": tl,
                "hotspots": hotspots,
            },
        }

    append_ai_flow_log(
        "input_ortools.txt",
        _to_ortools_input_log_payload(plan),
        title="ortools_input_pre_llm",
    )

    if not machines:
        out = {
            "enabled": True,
            "status": "SKIPPED",
            "reason": "Không có bottleneck_machines để chạy CP-SAT gợi ý.",
            "time_limit_s": tl,
            "suggested_moves": [],
        }
        append_ai_flow_log("output_ortools.txt", out, title="ortools_output_pre_llm")
        return out

    try:
        patched, result = cp_sat_reschedule(plan, {"machines": machines}, time_limit_s=tl)
        if not getattr(result, "ok", False):
            out = {
                "enabled": True,
                "status": "FAILED",
                "reason": str(getattr(result, "reason", "")) or "CP-SAT không trả về phương án khả thi.",
                "time_limit_s": tl,
                "wall_time_s": float(getattr(result, "wall_time_s", 0.0) or 0.0),
                "scheduled_ops": int(getattr(result, "scheduled_ops", 0) or 0),
                "suggested_moves": [],
            }
            append_ai_flow_log("output_ortools.txt", out, title="ortools_output_pre_llm")
            return out

        max_ops = int(os.getenv("AI_ORTOOLS_SUGGEST_MAX_OPS_PER_MACHINE", "18") or "18")
        suggested = _derive_swap_moves_for_machines(patched, machines, max_ops_per_machine=max_ops)
        out = {
            "enabled": True,
            "status": "OK",
            "reason": str(getattr(result, "reason", "ok")) or "ok",
            "time_limit_s": tl,
            "wall_time_s": float(getattr(result, "wall_time_s", 0.0) or 0.0),
            "scheduled_ops": int(getattr(result, "scheduled_ops", 0) or 0),
            "machines": machines,
            "suggested_moves": suggested,
        }
        append_ai_flow_log("output_ortools.txt", out, title="ortools_output_pre_llm")
        return out
    except Exception as e:
        out = {
            "enabled": True,
            "status": "ERROR",
            "reason": str(e),
            "time_limit_s": tl,
            "suggested_moves": [],
        }
        append_ai_flow_log("output_ortools.txt", out, title="ortools_output_pre_llm")
        return out


class PlanBuilderPort:
    """Port (interface) to adapt your existing repositories/services.

    You need to implement this adapter using current KeHoachRepo/calendar builder.
    """

    def get_plan(self, db: Session, plan_id: int, horizon_start: datetime, horizon_end: datetime) -> Plan:
        raise NotImplementedError

    def get_constraints(self, db: Session, horizon_start: datetime, horizon_end: datetime) -> Dict[str, Any]:
        raise NotImplementedError

    def get_capacity(self, db: Session, horizon_start: datetime, horizon_end: datetime) -> Dict[str, Any]:
        raise NotImplementedError

    def get_due_dates(self, db: Session, plan_id: int) -> Dict[str, datetime]:
        raise NotImplementedError


class PlanAIService:
    def __init__(self, plan_builder: PlanBuilderPort):
        self._plan_builder = plan_builder

    def analyze_and_propose(
        self,
        db: Session,
        plan_id: int,
        horizon_start: datetime,
        horizon_end: datetime,
        top_n: int = 2,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Analyze baseline plan and return top-N optimized options."""

        t0 = time.perf_counter()
        telemetry: Dict[str, float] = {}

        def mark(name: str, start: float) -> None:
            telemetry[name] = (time.perf_counter() - start) * 1000.0

        # ---- Build plan + constraints/capacity ----
        t = time.perf_counter()
        plan = self._plan_builder.get_plan(db, plan_id, horizon_start, horizon_end)
        plan.constraints = self._plan_builder.get_constraints(db, horizon_start, horizon_end)
        plan.capacity = self._plan_builder.get_capacity(db, horizon_start, horizon_end)
        plan.due_dates = self._plan_builder.get_due_dates(db, plan_id)
        mark("build_plan_ms", t)

        # ---- Baseline KPI + hotspots ----
        t = time.perf_counter()
        baseline_kpi = compute_kpi(plan)
        hotspots = find_hotspots(plan, baseline_kpi)
        mark("kpi_hotspots_ms", t)

        op_by_id: Dict[str, Any] = {}
        try:
            op_by_id = {str(op.op_id): op for op in (plan.operations or [])}
        except Exception:
            op_by_id = {}

        # ---- Baseline validation summary (for LLM + UI) ----
        baseline_errors = validate_plan(plan)
        baseline_err_summary: Dict[str, int] = {}
        try:
            for e in baseline_errors:
                c = str(getattr(e, "code", "") or "UNKNOWN")
                baseline_err_summary[c] = int(baseline_err_summary.get(c, 0)) + 1
        except Exception:
            baseline_err_summary = {}

        # ---- Prompt payload ----
        t = time.perf_counter()
        # ---- Build slim payload for LLM (avoid sending full calendar/busy_intervals) ----
        ortools_options = _build_ortools_options_for_llm(plan, hotspots)
        errors_for_llm: Dict[str, Any] = {
            "baseline_validation_summary": baseline_err_summary,
            "baseline_validation_errors_count": len(baseline_errors) if isinstance(baseline_errors, list) else None,
        }

        # Include a small sample of structured baseline errors so LLM can describe conflicts
        try:
            max_items = int(os.getenv("AI_BASELINE_ERRORS_SAMPLE_MAX", "8") or "8")
        except Exception:
            max_items = 8
        max_items = max(0, min(30, int(max_items)))
        try:
            raw_errors = [asdict(e) for e in (baseline_errors or [])]
            errors_for_llm["baseline_validation_errors_sample"] = _compact_validation_errors_for_llm(raw_errors, max_items)
        except Exception:
            errors_for_llm["baseline_validation_errors_sample"] = []

        payload_for_llm = build_payload(
            errors=errors_for_llm,
            ortools_options=ortools_options,
            context={
                "kpi_snapshot": _compact_kpi_for_llm(asdict(baseline_kpi)),
                "hotspots": _compact_hotspots_for_llm(hotspots),
                "business": extra_context or {},
            },
        )

        sys_prompt = build_system_prompt()
        user_prompt = build_user_prompt(payload_for_llm, top_n=top_n)
        mark("prompt_build_ms", t)

        # Optionally expose prompt for debugging (OFF by default)
        debug_prompt: Optional[Dict[str, Any]] = None
        try:
            if str(os.getenv("AI_DEBUG_PROMPT", "")).strip() in ("1", "true", "TRUE", "yes", "YES"):
                debug_prompt = {
                    "model": _active_llm_model_name(),
                    "system_prompt": sys_prompt,
                    "user_prompt": user_prompt,
                    "payload": payload_for_llm,
                }
        except Exception:
            debug_prompt = None

        proposals: List[Dict[str, Any]] = []
        llm_error: Optional[str] = None
        llm_skipped: bool = False
        llm_skip_reason: Optional[str] = None

        def _extra_context_has_late_or_unplanned(ctx: Optional[Dict[str, Any]]) -> bool:
            try:
                if not isinstance(ctx, dict):
                    return False
                prs = ctx.get("problem_rows") or []
                if not isinstance(prs, list):
                    return False
                for pr in prs:
                    if not isinstance(pr, dict):
                        continue
                    q = pr.get("qty") or {}
                    late = float((q.get("late") or 0) if isinstance(q, dict) else 0)
                    unplanned = float((q.get("unplanned") or 0) if isinstance(q, dict) else 0)
                    if late > 0 or unplanned > 0:
                        return True
                return False
            except Exception:
                return False

        # Only propose options when there is a real issue (late/unplanned/tardiness/conflict).
        try:
            late_ops = float(getattr(baseline_kpi, "late_ops", 0) or 0)
            tardy_min = float(getattr(baseline_kpi, "total_tardiness_minutes", 0) or 0)
        except Exception:
            late_ops = 0.0
            tardy_min = 0.0

        has_tardiness = (late_ops > 0) or (tardy_min > 0)
        has_conflicts = bool(baseline_errors)
        has_late_or_unplanned = _extra_context_has_late_or_unplanned(extra_context)
        has_issues = bool(has_tardiness or has_conflicts or has_late_or_unplanned)

        # Optional: capture full LLM I/O for debugging
        debug_llm_io: Optional[Dict[str, Any]] = None
        _explain_validation_enabled = _env_flag("AI_EXPLAIN_VALIDATION", True)
        _explain_max_errors = int(os.getenv("AI_EXPLAIN_VALIDATION_MAX_ERRORS", "80") or "80")
        _explain_timeout_s = int(os.getenv("AI_EXPLAIN_VALIDATION_TIMEOUT_S", "300") or "300")
        # Baseline explain adds an extra LLM call; disable by default for performance.
        _baseline_explain_enabled = _env_flag("AI_BASELINE_EXPLAIN", False)

        # baseline_errors + baseline_err_summary already computed above
        baseline_validation_explain = None
        try:
            if _baseline_explain_enabled and _explain_validation_enabled and baseline_errors:
                errs_dict0 = [asdict(e) for e in baseline_errors]
                errs_dict0 = _limit_validation_errors(errs_dict0, int(os.getenv("AI_BASELINE_EXPLAIN_MAX_ERRORS", "60") or "60"))

                from .openai_client import OpenAIClient

                explain_client0 = OpenAIClient(
                    model=_active_llm_model_name(),
                    timeout_s=int(os.getenv("AI_BASELINE_EXPLAIN_TIMEOUT_S", str(_explain_timeout_s)) or str(_explain_timeout_s)),
                    max_retries=_active_llm_max_retries(default_openai=2, default_gemini=2),
                )

                prompt0 = _build_validation_explain_prompt(int(plan_id), "BASELINE", errs_dict0)
                explain_raw0 = explain_client0.chat_text(
                    system_prompt="Bạn chỉ được output JSON thuần.",
                    user_prompt=prompt0,
                )

                try:
                    baseline_validation_explain = json.loads(explain_raw0)
                except Exception:
                    baseline_validation_explain = {
                        "summary": "Không parse được phân tích tổng thể từ LLM.",
                        "issues": [],
                        "recommendations": [],
                        "_raw": explain_raw0,
                    }

                # sanitize to avoid leaking machine codes like M011/M012
                baseline_validation_explain = _sanitize_validation_explain(baseline_validation_explain)
        except Exception:
            baseline_validation_explain = None

        try:
            if str(os.getenv("AI_DEBUG_LLM_IO", "")).strip() in ("1", "true", "TRUE", "yes", "YES"):
                debug_llm_io = {"model": _active_llm_model_name()}
        except Exception:
            debug_llm_io = None

        # ---- LLM ----
        t = time.perf_counter()
        llm_out: Optional[Dict[str, Any]] = None
        llm_analysis: Optional[Dict[str, Any]] = None
        if has_issues or _env_flag("AI_ALWAYS_PROPOSE", True):
            try:
                # Default to a bounded response time for interactive UI.
                # Can be overridden via AI_PROPOSE_TIMEOUT_S when you intentionally want longer waits.
                propose_timeout_s = int(os.getenv("AI_PROPOSE_TIMEOUT_S", "150") or "150")
                propose_max_retries = int(os.getenv("AI_PROPOSE_MAX_RETRIES", "1") or "1")
                llm_out = call_gpt(
                    payload=payload_for_llm,
                    system_prompt=sys_prompt,
                    user_prompt=user_prompt,
                    timeout_s=propose_timeout_s,
                    max_retries=propose_max_retries,
                )
                try:
                    if isinstance(llm_out, dict) and isinstance(llm_out.get("analysis"), dict):
                        llm_analysis = llm_out.get("analysis")
                except Exception:
                    llm_analysis = None
                if debug_llm_io is not None and isinstance(llm_out, dict):
                    debug_llm_io["parsed"] = llm_out
                    raw = llm_out.get("__debug_openai_raw_output_text")
                    if raw:
                        debug_llm_io["raw_output_text"] = raw
                proposals = list((llm_out or {}).get("proposals") or [])
            except OpenAIClientError as e:
                llm_error = str(e)
                logger.warning("LLM failed: %s", llm_error)
            except Exception as e:
                llm_error = str(e)
                logger.exception("Unexpected LLM error")
            finally:
                mark("llm_call_ms", t)
        else:
            llm_skipped = True
            llm_skip_reason = "No late/unplanned, no tardiness, and no baseline conflicts detected."
            telemetry["llm_call_ms"] = 0.0

        options: List[Dict[str, Any]] = []

        # ---- Apply/reschedule/validate per option ----
        t_opts = time.perf_counter()
        # If LLM returned too many proposals, cap hard.
        cap_props = int(os.getenv("AI_PROPOSALS_MAX", str(top_n)) or str(top_n))
        cap_props = max(1, min(20, cap_props))

        for p in proposals[: min(top_n, cap_props)]:
            proposal_id = str(p.get("id") or "")
            goal = str(p.get("title") or p.get("goal") or "")

            content_lines = p.get("content_lines")
            if isinstance(content_lines, list):
                lines2 = [str(x) for x in content_lines if x is not None and str(x).strip()]
            else:
                lines2 = []

            moves_raw = list(p.get("moves") or [])

            template_no = _coerce_template_no(
                p.get("template_no"),
                moves_raw=[m for m in moves_raw if isinstance(m, dict)],
                goal=goal,
                content_lines=lines2,
            )

            notes = str(p.get("notes") or "")
            if not notes and lines2:
                notes = "\n".join(lines2)

            priority = str(p.get("priority") or (f"P{template_no}" if template_no else "P2"))

            # Reconcile inconsistent LLM output:
            # Some responses set template_no=1 for all proposals but keep correct priority/title.
            try:
                pr = priority.strip().upper()
                g = (goal or "").lower()
                inferred = None
                if "điều chỉnh" in g and "giao hàng" in g:
                    inferred = 6
                elif "thuê" in g or "thời vụ" in g or "thoi vu" in g:
                    inferred = 4
                elif "đổi máy" in g or "bo sung" in g or "bổ sung" in g or "máy" in g:
                    inferred = 2

                if inferred is None:
                    if pr == "P4":
                        inferred = 6
                    elif pr == "P3":
                        inferred = 4
                    elif pr == "P2":
                        inferred = 2
                    elif pr == "P1":
                        inferred = 1

                if inferred in (1, 2, 3, 4, 5, 6):
                    if template_no is None:
                        template_no = inferred
                    elif int(template_no) == 1 and inferred in (2, 4, 6):
                        template_no = inferred
            except Exception:
                pass

            # Parse moves
            moves: List[Move] = [_parse_move(m) for m in moves_raw if isinstance(m, dict)]

            # Only allow auto-apply when backend can actually execute at least one move.
            executable_present = any(getattr(mv, "type", None) in EXECUTABLE_MOVE_TYPES for mv in moves)

            # Trust LLM's can_apply flag only as a *hint*.
            llm_can_apply = bool(p.get("can_apply")) if p.get("can_apply") is not None else True
            can_apply = bool(llm_can_apply and executable_present and (template_no in (1, 2) if template_no else True))

            # Enrich moves with baseline context (best-effort) for UI.
            moves_for_ui: List[Dict[str, Any]] = []
            for mv in moves:
                op = op_by_id.get(str(mv.op_id)) if getattr(mv, "op_id", None) else None

                # Fill missing from_machine for MOVE_TO_ALTERNATE_MACHINE
                try:
                    if mv.type == "MOVE_TO_ALTERNATE_MACHINE" and not mv.from_machine and op and getattr(op, "machine", None):
                        mv.from_machine = str(getattr(op, "machine"))
                except Exception:
                    pass

                mv_dict = asdict(mv)
                mv_dict.setdefault("desc", _build_move_desc(mv, op))

                if op:
                    mv_dict.setdefault("step_name", getattr(op, "ten_cong_doan", None))
                    mv_dict.setdefault("dept_name", getattr(op, "ten_bo_phan", None))
                    mv_dict.setdefault("tp_name", getattr(op, "ten_thanh_pham", None))
                    mv_dict.setdefault("btp_name", getattr(op, "ten_ban_thanh_pham", None))
                    mv_dict.setdefault("so_don_hang", getattr(op, "so_don_hang", None))
                    mv_dict.setdefault("op_context", _op_display_fields(op))

                moves_for_ui.append(mv_dict)

            patched, scope = apply_moves(plan, moves)
            patched = local_reschedule(patched, scope)
            errors = validate_plan(patched)

            # Summarize errors by code for UI
            err_summary: Dict[str, int] = {}
            try:
                for e in errors:
                    c = str(getattr(e, "code", "") or "UNKNOWN")
                    err_summary[c] = int(err_summary.get(c, 0)) + 1
            except Exception:
                err_summary = {}

            # LLM: explain validation errors in business terms (optional)
            validation_explain = None
            try:
                if _explain_validation_enabled and errors:
                    # serialize + cap to avoid huge prompts
                    errs_dict = [asdict(e) for e in errors]
                    errs_dict = _limit_validation_errors(errs_dict, _explain_max_errors)

                    # quick grouping for UI even without LLM
                    grouped_for_ui = _group_validation_errors_for_ui(errs_dict, max_per_code=10)

                    # Use OpenAI client to turn it into user-facing text
                    from .openai_client import OpenAIClient

                    explain_client = OpenAIClient(
                        model=_active_llm_model_name(),
                        timeout_s=_explain_timeout_s,
                        max_retries=_active_llm_max_retries(default_openai=2, default_gemini=2),
                    )

                    prompt = _build_validation_explain_prompt(int(plan_id), str(proposal_id), errs_dict)
                    explain_raw = explain_client.chat_text(
                        system_prompt="Bạn chỉ được output JSON thuần.",
                        user_prompt=prompt,
                    )

                    try:
                        validation_explain = json.loads(explain_raw)
                    except Exception:
                        # Fallback: still return grouped structured errors
                        validation_explain = {
                            "summary": "Không parse được giải thích từ LLM.",
                            "issues": [],
                            "recommendations": [],
                            "top_violations": [],
                            "_raw": explain_raw,
                        }

                    # sanitize to avoid leaking machine codes like M011/M012
                    validation_explain = _sanitize_validation_explain(validation_explain)

                    # Always attach structured grouped data (for UI drilldown)
                    if isinstance(validation_explain, dict):
                        validation_explain.setdefault("grouped_errors", grouped_for_ui)
            except Exception as _ex:
                # Do not expose technical details to end users.
                validation_explain = {
                    "summary": "Phương án hiện vẫn có xung đột lịch ở một số bộ phận/công đoạn. Cần điều chỉnh để lịch khả thi.",
                    "issues": [],
                    "recommendations": [],
                    "grouped_errors": _group_validation_errors_for_ui([asdict(e) for e in errors], max_per_code=10),
                    "_error": str(_ex),
                }

            kpi_after = compute_kpi(patched) if not errors else None

            delta: Dict[str, float] = {}
            if kpi_after:
                delta = {
                    "late_ops_delta": float(kpi_after.late_ops - baseline_kpi.late_ops),
                    "tardiness_minutes_delta": float(kpi_after.total_tardiness_minutes - baseline_kpi.total_tardiness_minutes),
                    "setup_minutes_delta": float(kpi_after.total_setup_minutes - baseline_kpi.total_setup_minutes),
                }

            # Build rows for UI table
            plan_rows: List[Dict[str, Any]] = []
            try:
                plan_rows = calendarize_plan(patched)
            except Exception:
                plan_rows = []

            options.append(
                {
                    "proposal_id": proposal_id,
                    "goal": goal,
                    "notes": notes,
                    "template_no": template_no,
                    "priority": priority,
                    "can_apply": bool(can_apply and bool(moves_for_ui)),
                    "content_lines": lines2,
                    "moves": moves_for_ui,
                    "validation_errors": [asdict(e) for e in errors],
                    "validation_summary": err_summary,
                    "validation_explain": validation_explain,
                    "kpi_before": asdict(baseline_kpi),
                    "kpi_after": asdict(kpi_after) if kpi_after else None,
                    "delta": delta,
                    "plan_rows": plan_rows,
                }
            )

        mark("options_build_ms", t_opts)

        # Baseline rows for optional UI compare
        t = time.perf_counter()
        baseline_rows: List[Dict[str, Any]] = []
        try:
            baseline_rows = calendarize_plan(plan)
        except Exception:
            baseline_rows = []
        mark("baseline_calendarize_ms", t)

        telemetry["total_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "AI analyze plan_id=%s total=%.0fms build_plan=%.0fms llm=%.0fms options=%.0fms",
            plan_id,
            telemetry.get("total_ms", 0.0),
            telemetry.get("build_plan_ms", 0.0),
            telemetry.get("llm_call_ms", 0.0),
            telemetry.get("options_build_ms", 0.0),
        )

        result: Dict[str, Any] = {
            "baseline_kpi": asdict(baseline_kpi),
            "baseline_rows": baseline_rows,
            "analysis": llm_analysis,
            "options": options,
            "llm_error": llm_error,
            "has_issues": has_issues,
            "llm_skipped": llm_skipped,
            "llm_skip_reason": llm_skip_reason,
            "telemetry": telemetry,
            "baseline_validation_summary": baseline_err_summary,
            "baseline_validation_explain": baseline_validation_explain,
            "debug_prompt": debug_prompt,
            "debug_llm_io": debug_llm_io,
        }

        return result


def _parse_move(d: Dict[str, Any]) -> Move:
    # Keep permissive; validator/rescheduler will decide.
    exp = d.get("expected_impact")
    expected_impact: Dict[str, Any] = {}
    try:
        if isinstance(exp, dict):
            expected_impact = exp
        elif isinstance(exp, list):
            # allow list of [key, value] pairs
            tmp: Dict[str, Any] = {}
            for item in exp:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    k, v = item
                    tmp[str(k)] = v
            expected_impact = tmp
        elif exp is None:
            expected_impact = {}
        else:
            # unexpected shape from LLM; keep raw for debug but don't crash
            expected_impact = {"_raw": exp}
    except Exception:
        expected_impact = {}

    return Move(
        type=d.get("type"),
        machine=d.get("machine"),
        op_ids_before=d.get("op_ids_before"),
        op_ids_after=d.get("op_ids_after"),
        op_id=d.get("op_id"),
        from_machine=d.get("from_machine"),
        to_machine=d.get("to_machine"),
        from_day=d.get("from_day"),
        to_day=d.get("to_day"),
        reason=str(d.get("reason") or ""),
        expected_impact=expected_impact,
    )


def _strip_machine_codes(text: str) -> str:
    """Remove machine codes like M011/M012 from user-facing strings."""

    try:
        import re

        if not text:
            return ""
        s = str(text)
        # Replace standalone M### patterns.
        s = re.sub(r"\bM\d{2,4}\b", "máy", s, flags=re.IGNORECASE)
        # Clean double spaces after replacement
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s
    except Exception:
        return str(text or "")


def _sanitize_validation_explain(ex: Any) -> Any:
    """Sanitize LLM explain to be business-friendly and avoid leaking machine codes."""

    if not isinstance(ex, dict):
        return ex

    try:
        # sanitize top-level summary
        if isinstance(ex.get("summary"), str):
            ex["summary"] = _strip_machine_codes(ex.get("summary") or "")

        # sanitize issues
        issues = ex.get("issues")
        if isinstance(issues, list):
            for it in issues:
                if not isinstance(it, dict):
                    continue
                if isinstance(it.get("title"), str):
                    it["title"] = _strip_machine_codes(it.get("title") or "")
                if isinstance(it.get("detail"), str):
                    it["detail"] = _strip_machine_codes(it.get("detail") or "")

        # sanitize recommendations
        recs = ex.get("recommendations")
        if isinstance(recs, list):
            import re
            cleaned: list[dict] = []
            for r in recs:
                if not isinstance(r, dict):
                    continue
                action = _strip_machine_codes(r.get("action") or "").strip()
                why = _strip_machine_codes(r.get("why") or "").strip()
                eff = _strip_machine_codes(r.get("expected_effect") or "").strip()
                # Loại bỏ triệt để nếu còn từ "máy" hoặc pattern M### hoặc các biến thể liên quan
                if any(re.search(r"(\bM\d{2,4}\b|máy|machine|thiết bị|công suất|tắc nghẽn|giảm tải|cân bằng tải)", s, re.IGNORECASE) for s in [action, why, eff]):
                    continue
                if not action:
                    continue
                cleaned.append({"action": action, "why": why, "expected_effect": eff})

            # ---- Merge similar lines to avoid long duplicated machine-based bullets ----
            # Heuristic: if multiple items mention the same verb/intent, combine into one.
            merged: list[dict] = []
            def _norm(s: str) -> str:
                return " ".join(str(s or "").lower().split())

            buckets: dict[str, dict] = {}
            for it in cleaned:
                a = _norm(it.get("action", ""))
                # key by leading phrase (first ~6 words)
                words = a.split()
                k = " ".join(words[:6]) if words else a
                if not k:
                    continue
                if k not in buckets:
                    buckets[k] = {"action": it.get("action"), "why": [], "expected_effect": []}
                if it.get("why"):
                    buckets[k]["why"].append(it.get("why"))
                if it.get("expected_effect"):
                    buckets[k]["expected_effect"].append(it.get("expected_effect"))

            for b in buckets.values():
                why_u = []
                seen = set()
                for x in b.get("why") or []:
                    nx = _norm(x)
                    if not nx or nx in seen:
                        continue
                    seen.add(nx)
                    why_u.append(x)
                eff_u = []
                seen2 = set()
                for x in b.get("expected_effect") or []:
                    nx = _norm(x)
                    if not nx or nx in seen2:
                        continue
                    seen2.add(nx)
                    eff_u.append(x)

                merged.append(
                    {
                        "action": b.get("action") or "",
                        "why": " • ".join(why_u[:2]),
                        "expected_effect": " • ".join(eff_u[:2]),
                    }
                )

            # Keep at most 2 recommendations for UI conciseness
            ex["recommendations"] = merged[:2]

        return ex
    except Exception:
        return ex


def _op_display_fields(op: Any) -> Dict[str, Any]:
    """Pick a compact set of business-friendly fields for UI/LLM."""

    if not op:
        return {}
    try:
        return {
            "so_don_hang": getattr(op, "so_don_hang", None),
            "ten_thanh_pham": getattr(op, "ten_thanh_pham", None),
            "ten_ban_thanh_pham": getattr(op, "ten_ban_thanh_pham", None),
            "ten_cong_doan": getattr(op, "ten_cong_doan", None),
            "ten_bo_phan": getattr(op, "ten_bo_phan", None),
            "ma_cong_doan": getattr(op, "ma_cong_doan", None),
            "ma_cong_doan_lon": getattr(op, "ma_cong_doan_lon", None),
            "machine": getattr(op, "machine", None),
        }
    except Exception:
        return {}


def _build_move_desc(mv: Move, op: Any | None) -> str:
    """Build a concrete user-facing step for the UI."""

    base_reason = str(getattr(mv, "reason", "") or "").strip()
    if base_reason:
        # Keep LLM reason as primary, it is often business-friendly.
        return base_reason

    # Fallback: deterministic templates
    ten_cd = getattr(op, "ten_cong_doan", None) if op else None
    ten_bp = getattr(op, "ten_bo_phan", None) if op else None
    tp = getattr(op, "ten_thanh_pham", None) if op else None
    btp = getattr(op, "ten_ban_thanh_pham", None) if op else None
    dh = getattr(op, "so_don_hang", None) if op else None

    pieces = [p for p in [btp or tp, ten_cd, ten_bp, dh] if p]
    obj = " / ".join([str(p) for p in pieces]) if pieces else None

    if mv.type == "MOVE_TO_ALTERNATE_MACHINE":
        if obj:
            return f"Chuyển công đoạn sang nguồn lực/máy thay thế: {obj}"
        return "Chuyển công đoạn sang nguồn lực/máy thay thế"
    if mv.type == "SWAP_ORDER_ON_MACHINE":
        return "Sắp xếp lại thứ tự chạy trên nguồn lực/máy"
    if mv.type == "PULL_FORWARD_CRITICAL_OP":
        if obj:
            return f"Ưu tiên chạy sớm công đoạn quan trọng: {obj}"
        return "Ưu tiên chạy sớm công đoạn quan trọng"
    if mv.type == "SHIFT_TO_FILL_IDLE_WINDOW":
        if obj:
            return f"Dịch chuyển để lấp khoảng trống thời gian: {obj}"
        return "Dịch chuyển để lấp khoảng trống thời gian"
    if mv.type == "BATCH_SAME_SETUP_FAMILY":
        return "Gom nhóm các công đoạn cùng setup để giảm thời gian chuyển đổi"

    return "Điều chỉnh đề xuất"
