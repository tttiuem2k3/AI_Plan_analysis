from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...ai_plan_analyzer.service import PlanAIService, PlanBuilderPort
from ...repositories.ke_hoach_repo import KeHoachRepo
from ...models.nguon_luc import DM_CongDoanLon, DM_CongDoan
from ...models.dinh_muc import DinhMucSanPham
from ...models.don_hang import DonHangSX
from ...ai_plan_analyzer.dtos import Operation as AIOperation, Plan as AIPlan, Segment as AISegment


router = APIRouter(prefix="/plans", tags=["AI - Optimize Plan"])


class KeHoachPlanBuilder(PlanBuilderPort):
    """Adapter from existing KeHoachRepo plan segments (KeHoachSX_ChiTiet) to AI Plan."""

    def get_plan(self, db: Session, plan_id: int, horizon_start: datetime, horizon_end: datetime) -> AIPlan:  # type: ignore[override]
        seg_rows = KeHoachRepo.get_chitiet_by_kehoach(db, int(plan_id))
        if not seg_rows:
            raise ValueError("Plan not found")

        # --- Preload name mappings (best-effort) ---
        # Dept code -> name
        dept_codes = sorted({str(r.get("MaCongDoanLon")) for r in seg_rows if r.get("MaCongDoanLon")})
        dept_name_by_code: dict[str, str] = {}
        if dept_codes:
            for d in db.query(DM_CongDoanLon).filter(DM_CongDoanLon.MaCongDoanLon.in_(dept_codes)).all():
                dept_name_by_code[str(d.MaCongDoanLon)] = str(d.TenCongDoanLon)

        # Step code -> name
        step_codes = sorted({str(r.get("MaCongDoan")) for r in seg_rows if r.get("MaCongDoan")})
        step_name_by_code: dict[str, str] = {}
        if step_codes:
            for cd in db.query(DM_CongDoan).filter(DM_CongDoan.MaCongDoan.in_(step_codes)).all():
                step_name_by_code[str(cd.MaCongDoan)] = str(cd.TenCongDoan)

        # TP/BTP id -> name (TenSanPham)
        prod_ids = sorted(
            {
                int(x)
                for r in seg_rows
                for x in (r.get("TP_DinhMucID"), r.get("BTP_DinhMucID"))
                if x is not None
            }
        )
        prod_name_by_id: dict[int, str] = {}
        if prod_ids:
            for p in db.query(DinhMucSanPham).filter(DinhMucSanPham.DinhMucID.in_(prod_ids)).all():
                try:
                    prod_name_by_id[int(p.DinhMucID)] = str(p.TenSanPham)
                except Exception:
                    continue

        # DonHangID -> SoDonHang
        don_hang_ids = sorted({int(r.get("DonHangID")) for r in seg_rows if r.get("DonHangID") is not None})
        so_don_hang_by_id: dict[int, str] = {}
        if don_hang_ids:
            for dh in db.query(DonHangSX).filter(DonHangSX.DonHangID.in_(don_hang_ids)).all():
                try:
                    so_don_hang_by_id[int(dh.DonHangID)] = str(dh.SoDonHang)
                except Exception:
                    continue

        # Group segments into operations by business key
        grouped = {}
        for r in seg_rows:
            # IMPORTANT: include MaNguonLuc in grouping key.
            # Otherwise segments of the same step split across machines are merged,
            # and the analyzer may assign a wrong/dominant machine to a BTP.
            machine_key = None
            if r.get("MaNguonLuc") not in (None, ""):
                machine_key = str(r.get("MaNguonLuc"))
            key = (
                int(r.get("DonHangID")) if r.get("DonHangID") is not None else None,
                str(r.get("LineKey") or ""),
                int(r.get("TP_DinhMucID")) if r.get("TP_DinhMucID") is not None else None,
                int(r.get("BTP_DinhMucID")) if r.get("BTP_DinhMucID") is not None else None,
                str(r.get("MaCongDoan") or ""),
                int(r.get("ThuTuSX")) if r.get("ThuTuSX") is not None else None,
                machine_key,
            )
            grouped.setdefault(key, []).append(r)

        ops: list[AIOperation] = []
        for (don_hang_id, line_key, tp_id, btp_id, ma_cd, thu_tu_sx, machine_key), rows in grouped.items():
            # Stable op_id
            # NOTE: LineKey may contain '::' which would break tokenization; encode it.
            lk_enc = quote(str(line_key or ""), safe="")
            op_id = (
                f"KH{plan_id}::DH{don_hang_id}::LK{lk_enc}::TP{tp_id}::BTP{btp_id}"
                f"::CD{ma_cd}::T{thu_tu_sx}"
            )

            # Include machine token to avoid collisions between same (DH/LK/TP/BTP/CD/T)
            # that are executed on different resources.
            machine = str(machine_key) if machine_key not in (None, "") else None
            if machine:
                op_id = op_id + f"::M{machine}"

            ma_cdl = None
            for rr in rows:
                if rr.get("MaCongDoanLon"):
                    ma_cdl = str(rr.get("MaCongDoanLon"))
                    break

            # Quantities and minutes are stored on segments; aggregate best-effort
            total_qty = 0.0
            setup_total = 0.0
            run_total = 0.0
            due_dt = None
            segments: list[AISegment] = []

            for rr in rows:
                total_qty = max(total_qty, float(rr.get("SoLuongSX") or 0.0))
                setup_total += float(rr.get("SetupMinutes") or 0.0)
                run_total += float(rr.get("RunMinutes") or 0.0)
                if due_dt is None and rr.get("DueDT"):
                    due_dt = rr.get("DueDT")

                st = rr.get("StartDT")
                en = rr.get("EndDT")
                if st and en:
                    segments.append(AISegment(start=st, end=en, qty=float(rr.get("SoLuongSX") or 0.0), is_setup=False))

            # Derive unit minutes if possible (avoid div by zero)
            unit_min = 0.0
            if total_qty > 0 and run_total > 0:
                unit_min = float(run_total) / float(total_qty)

            ops.append(
                AIOperation(
                    op_id=str(op_id),
                    plan_id=int(plan_id),
                    don_hang_id=don_hang_id,
                    so_don_hang=(so_don_hang_by_id.get(int(don_hang_id)) if don_hang_id is not None else None),
                    tp_id=tp_id,
                    ten_thanh_pham=(prod_name_by_id.get(int(tp_id)) if tp_id is not None else None),
                    btp_id=btp_id,
                    ten_ban_thanh_pham=(prod_name_by_id.get(int(btp_id)) if btp_id is not None else None),
                    thu_tu_sx=thu_tu_sx,
                    ma_cong_doan=ma_cd or None,
                    ten_cong_doan=(step_name_by_code.get(str(ma_cd)) if ma_cd else None),
                    ma_cong_doan_lon=ma_cdl,
                    ten_bo_phan=(dept_name_by_code.get(ma_cdl) if ma_cdl else None),
                    machine=machine,
                    loai_nguon_luc=("labor" if (str(machine or "").upper() == "M000") else "machine") if machine else None,
                    nhan_su_phan_bo=0.0,
                    total_qty=total_qty,
                    dinh_muc_phut_moi_sp=unit_min,
                    setup_minutes=float(setup_total),
                    segments=segments,
                    meta={
                        "line_key": line_key,
                        "run_minutes_total": run_total,
                        "setup_minutes_total": setup_total,
                    },
                )
            )

        return AIPlan(
            plan_id=int(plan_id),
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            operations=ops,
        )

    def get_constraints(self, db: Session, horizon_start: datetime, horizon_end: datetime):  # type: ignore[override]
        # Busy intervals from other plans (rule #3)
        # - machine: MaNguonLuc
        # - dept: MaCongDoanLon
        # Exclude plan being optimized will be injected at service-level? Here we can't know plan_id.
        # So we keep it empty here; ai_optimize_plan sẽ patch thêm theo plan_id.
        return {
            "work_hours": {"regular_hours": 7, "ot_hours_max": 4, "outsource_people_max_per_day": 15},
            "horizon": {"start": horizon_start.isoformat(), "end": horizon_end.isoformat()},
            "machine_sequences": {},
            "busy_intervals": [],
        }

    def get_capacity(self, db: Session, horizon_start: datetime, horizon_end: datetime):  # type: ignore[override]
        return {}

    def get_due_dates(self, db: Session, plan_id: int):  # type: ignore[override]
        seg_rows = KeHoachRepo.get_chitiet_by_kehoach(db, int(plan_id))
        if not seg_rows:
            return {}
        out = {}
        # same grouping key/op_id as get_plan
        for r in seg_rows:
            don_hang_id = int(r.get("DonHangID")) if r.get("DonHangID") is not None else None
            line_key = str(r.get("LineKey") or "")
            tp_id = int(r.get("TP_DinhMucID")) if r.get("TP_DinhMucID") is not None else None
            btp_id = int(r.get("BTP_DinhMucID")) if r.get("BTP_DinhMucID") is not None else None
            ma_cd = str(r.get("MaCongDoan") or "")
            thu_tu_sx = int(r.get("ThuTuSX")) if r.get("ThuTuSX") is not None else None
            lk_enc = quote(str(line_key or ""), safe="")
            op_id = (
                f"KH{plan_id}::DH{don_hang_id}::LK{lk_enc}::TP{tp_id}::BTP{btp_id}"
                f"::CD{ma_cd}::T{thu_tu_sx}"
            )
            due = r.get("DueDT")
            if due:
                out[str(op_id)] = due
        return out


class _BusyIntervalsPlanBuilder(PlanBuilderPort):
    def __init__(self, inner: PlanBuilderPort, *, db: Session, plan_id: int):
        self._inner = inner
        self._db = db
        self._plan_id = int(plan_id)

    def get_plan(self, db: Session, plan_id: int, horizon_start: datetime, horizon_end: datetime) -> AIPlan:  # type: ignore[override]
        return self._inner.get_plan(db, plan_id, horizon_start, horizon_end)

    def get_constraints(self, db: Session, horizon_start: datetime, horizon_end: datetime):  # type: ignore[override]
        c = dict(self._inner.get_constraints(db, horizon_start, horizon_end) or {})
        c["busy_intervals"] = KeHoachRepo.get_busy_intervals_other_plans(
            self._db,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            exclude_ke_hoach_id=self._plan_id,
        )
        return c

    def get_capacity(self, db: Session, horizon_start: datetime, horizon_end: datetime):  # type: ignore[override]
        return self._inner.get_capacity(db, horizon_start, horizon_end)

    def get_due_dates(self, db: Session, plan_id: int):  # type: ignore[override]
        return self._inner.get_due_dates(db, plan_id)


class CalendarPayloadPlanBuilder(PlanBuilderPort):
    """Build AI Plan from a calendar payload instead of DB.

    Intended for remote clients who send `Header/Days/Rows` JSON to this server.
    """

    def __init__(
        self,
        calendar: dict,
        *,
        busy_intervals: Optional[list[dict]] = None,
        related_production_plans: Optional[list[dict]] = None,
        enforce_dept_no_overlap: Optional[bool] = None,
    ):
        self._calendar = calendar if isinstance(calendar, dict) else {}
        self._busy_intervals = busy_intervals if isinstance(busy_intervals, list) else []
        self._related_production_plans = related_production_plans if isinstance(related_production_plans, list) else []
        self._enforce_dept_no_overlap = enforce_dept_no_overlap

    @staticmethod
    def _split_csv(v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            out = []
            for x in v:
                s = str(x or "").strip()
                if s:
                    out.append(s)
            return out
        s = str(v or "").strip()
        if not s:
            return []
        return [x.strip() for x in s.split(",") if x and str(x).strip()]

    @staticmethod
    def _parse_dt(v: Any) -> Optional[datetime]:
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, date) and not isinstance(v, datetime):
            return datetime.combine(v, datetime.min.time())
        if isinstance(v, (int, float)):
            # avoid guessing epoch units
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            s = s.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s)
            except Exception:
                # best-effort for common formats
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(s[:19] if "T" in fmt or ":" in fmt else s[:10], fmt)
                        return dt
                    except Exception:
                        continue
        return None

    @staticmethod
    def _to_int(v: Any) -> Optional[int]:
        try:
            if v in (None, ""):
                return None
            if isinstance(v, bool):
                return None
            if isinstance(v, int):
                return int(v)
            if isinstance(v, float):
                return int(v)
            s = str(v).strip()
            if not s:
                return None
            if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
                return int(s)
            return int(float(s))
        except Exception:
            return None

    @staticmethod
    def _to_float(v: Any, default: float = 0.0) -> float:
        try:
            if v in (None, ""):
                return float(default)
            if isinstance(v, bool):
                return float(default)
            if isinstance(v, (int, float)):
                return float(v)
            s = str(v).strip()
            if not s:
                return float(default)
            return float(s)
        except Exception:
            return float(default)

    @staticmethod
    def _id_token(v: Any, *, default: str = "0") -> str:
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

    @staticmethod
    def _pick_first(row: Dict[str, Any], keys: list[str], default: Any = None) -> Any:
        if not isinstance(row, dict):
            return default
        for k in keys:
            if k in row and row.get(k) not in (None, ""):
                return row.get(k)
        return default

    @staticmethod
    def _extract_resource_codes(row: Dict[str, Any]) -> list[str]:
        out: list[str] = []

        # 1) Internal normalized field (comma-separated string)
        for x in CalendarPayloadPlanBuilder._split_csv(row.get("MaNguonLuc")):
            if x and x not in out:
                out.append(x)

        # 2) Normalized details preserved from API ResourceIDs
        details = row.get("ResourceIDDetails")
        if isinstance(details, list):
            for item in details:
                if not isinstance(item, dict):
                    continue
                c = str(item.get("ResourceID") or "").strip()
                if c and c not in out:
                    out.append(c)

        # 3) API raw field: ResourceIDs (list[dict]|list[str]|str)
        raw = row.get("ResourceIDs")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    c = str(item.get("ResourceID") or item.get("MaNguonLuc") or "").strip()
                else:
                    c = str(item or "").strip()
                if c and c not in out:
                    out.append(c)
        elif isinstance(raw, dict):
            c = str(raw.get("ResourceID") or raw.get("MaNguonLuc") or "").strip()
            if c and c not in out:
                out.append(c)
        else:
            for x in CalendarPayloadPlanBuilder._split_csv(raw):
                if x and x not in out:
                    out.append(x)

        return out

    @staticmethod
    def _pick_primary_machine(row: Dict[str, Any], machine_list: list[str]) -> Optional[str]:
        # Prefer machine with largest TimeResource from API ResourceIDs details when available.
        best_code: Optional[str] = None
        best_time: float = -1.0
        details = row.get("ResourceIDDetails")
        if isinstance(details, list):
            for item in details:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("ResourceID") or "").strip()
                if not code:
                    continue
                try:
                    t = float(item.get("TimeResource") or 0.0)
                except Exception:
                    t = 0.0
                if t > best_time:
                    best_time = t
                    best_code = code

        if best_code:
            return str(best_code)
        if machine_list:
            return str(machine_list[0])
        return None

    def get_plan(self, db: Session, plan_id: int, horizon_start: datetime, horizon_end: datetime) -> AIPlan:  # type: ignore[override]
        rows = self._calendar.get("Rows") or []
        if not isinstance(rows, list) or not rows:
            raise ValueError("Missing calendar Rows")

        header = self._calendar.get("Header") if isinstance(self._calendar.get("Header"), dict) else {}
        plan_apk = self._pick_first(header, ["PlanAPK", "APK"])

        ops: list[AIOperation] = []
        for r in rows:
            if not isinstance(r, dict):
                continue

            don_hang_raw = self._pick_first(r, ["DonHangID", "OrderNo", "SoDonHang", "SoChungTu"])
            don_hang_id = self._to_int(don_hang_raw)
            line_key = str(self._pick_first(r, ["LineKey"]) or "")
            tp_raw = self._pick_first(r, ["TP_DinhMucID", "APK_MT2141", "IdTP"])
            btp_raw = self._pick_first(r, ["BTP_DinhMucID", "APK_MT2142", "IdBTP"])
            thu_tu_raw = self._pick_first(r, ["ThuTuSX", "ProductionSequence"])
            # Keep API identifiers as-is (APK_MT2141/APK_MT2142 may be GUID-like strings).
            tp_id = tp_raw if tp_raw not in (None, "") else None
            btp_id = btp_raw if btp_raw not in (None, "") else None
            ma_cd = str(self._pick_first(r, ["MaCongDoan", "PhaseID"]) or "") or None
            thu_tu_sx = self._to_int(thu_tu_raw)

            if not line_key:
                line_key = "::".join(
                    [
                        str(self._pick_first(r, ["OrderNo", "DonHangID"]) or ""),
                        str(self._pick_first(r, ["FinishedProductCode", "MaTP"]) or ""),
                        str(self._pick_first(r, ["SemiFinishedProductCode", "MaBTP"]) or ""),
                    ]
                )

            # ResourceIDs from API are first-class inputs for OR-tools.
            machine_list = self._extract_resource_codes(r)
            machine = self._pick_primary_machine(r, machine_list)
            if machine:
                machine = str(machine)

            dh_token = self._id_token(don_hang_raw, default="0")
            tp_token = self._id_token(tp_raw, default="0")
            btp_token = self._id_token(btp_raw, default="0")
            tts_token = self._id_token(thu_tu_raw, default="0")
            lk_enc = quote(str(line_key or ""), safe="")
            op_id = (
                f"KH{int(plan_id)}::DH{dh_token}::LK{lk_enc}::TP{tp_token}::BTP{btp_token}"
                f"::CD{str(ma_cd or '')}::T{tts_token}"
            )
            if machine:
                op_id = op_id + f"::M{machine}"

            total_qty = self._to_float(self._pick_first(r, ["TotalQty", "TotalQuantity", "SoLuongSX"]), 0.0)
            setup_total = self._to_float(self._pick_first(r, ["TotalSetup", "SetupMinutes"]), 0.0)
            run_total = self._to_float(self._pick_first(r, ["TotalRun", "RunMinutes"]), 0.0)

            # API TimeLimit is treated as unit minutes when TotalRun is not provided.
            unit_min = 0.0
            time_limit = self._to_float(self._pick_first(r, ["DinhMucThoiGian", "TimeLimit"]), 0.0)
            if total_qty > 0 and run_total > 0:
                unit_min = float(run_total) / float(total_qty)
            elif total_qty > 0 and time_limit > 0:
                unit_min = float(time_limit)
                run_total = float(total_qty) * float(unit_min)

            st = self._parse_dt(self._pick_first(r, ["Start", "StartDate", "StartDT"]))
            en = self._parse_dt(self._pick_first(r, ["End", "EndDate", "EndDT"]))
            segs: list[AISegment] = []
            if st and en:
                segs.append(AISegment(start=st, end=en, qty=total_qty, is_setup=False))

            ops.append(
                AIOperation(
                    op_id=str(op_id),
                    plan_id=int(plan_id),
                    don_hang_id=don_hang_id,
                    so_don_hang=str(self._pick_first(r, ["SoChungTu", "SoDonHang", "OrderNo"]) or "") or None,
                    tp_id=tp_id,
                    ten_thanh_pham=str(self._pick_first(r, ["TenThanhPham", "FinishedProductName", "FinishedProductNameEn"]) or "") or None,
                    btp_id=btp_id,
                    ten_ban_thanh_pham=str(self._pick_first(r, ["TenBanThanhPham", "SemiFinishedProductName"]) or "") or None,
                    thu_tu_sx=thu_tu_sx,
                    ma_cong_doan=ma_cd,
                    ten_cong_doan=str(self._pick_first(r, ["TenCongDoan", "PhaseName"]) or "") or None,
                    ma_cong_doan_lon=str(self._pick_first(r, ["MaCongDoanLon", "PhaseGroupID", "PhaseGroupName"]) or "") or None,
                    ten_bo_phan=str(self._pick_first(r, ["TenBoPhan", "PhaseGroupName"]) or "") or None,
                    machine=machine,
                    loai_nguon_luc=("labor" if (str(machine or "").upper() == "M000") else "machine") if machine else None,
                    nhan_su_phan_bo=0.0,
                    total_qty=total_qty,
                    dinh_muc_phut_moi_sp=unit_min,
                    setup_minutes=float(setup_total),
                    segments=segs,
                    meta={
                        "line_key": line_key,
                        "machines": machine_list,
                        "run_minutes_total": run_total,
                        "setup_minutes_total": setup_total,
                        "plan_apk": plan_apk,
                        "customer_name": self._pick_first(r, ["KhachHang", "CustomerName"]),
                    },
                )
            )

        return AIPlan(plan_id=int(plan_id), horizon_start=horizon_start, horizon_end=horizon_end, operations=ops)

    def get_constraints(self, db: Session, horizon_start: datetime, horizon_end: datetime):  # type: ignore[override]
        c: dict[str, Any] = {
            "work_hours": {"regular_hours": 7, "ot_hours_max": 4, "outsource_people_max_per_day": 15},
            "horizon": {"start": horizon_start.isoformat(), "end": horizon_end.isoformat()},
            "machine_sequences": {},
            "busy_intervals": self._busy_intervals,
            "related_production_plans": self._related_production_plans,
        }
        if self._enforce_dept_no_overlap is not None:
            c["enforce_dept_no_overlap"] = bool(self._enforce_dept_no_overlap)
        return c

    def get_capacity(self, db: Session, horizon_start: datetime, horizon_end: datetime):  # type: ignore[override]
        return {}

    def get_due_dates(self, db: Session, plan_id: int):  # type: ignore[override]
        rows = self._calendar.get("Rows") or []
        if not isinstance(rows, list):
            return {}
        out: dict[str, datetime] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            due = self._parse_dt(self._pick_first(r, ["DueDT", "DueDate"]))
            if not due:
                continue
            # build same op_id as get_plan
            don_hang_raw = self._pick_first(r, ["DonHangID", "OrderNo", "SoDonHang", "SoChungTu"])
            line_key = str(self._pick_first(r, ["LineKey"]) or "")
            tp_raw = self._pick_first(r, ["TP_DinhMucID", "APK_MT2141", "IdTP"])
            btp_raw = self._pick_first(r, ["BTP_DinhMucID", "APK_MT2142", "IdBTP"])
            thu_tu_raw = self._pick_first(r, ["ThuTuSX", "ProductionSequence"])
            ma_cd = str(self._pick_first(r, ["MaCongDoan", "PhaseID"]) or "") or None
            machine_list = self._extract_resource_codes(r)
            machine = self._pick_primary_machine(r, machine_list)

            if not line_key:
                line_key = "::".join(
                    [
                        str(self._pick_first(r, ["OrderNo", "DonHangID"]) or ""),
                        str(self._pick_first(r, ["FinishedProductCode", "MaTP"]) or ""),
                        str(self._pick_first(r, ["SemiFinishedProductCode", "MaBTP"]) or ""),
                    ]
                )

            dh_token = self._id_token(don_hang_raw, default="0")
            tp_token = self._id_token(tp_raw, default="0")
            btp_token = self._id_token(btp_raw, default="0")
            tts_token = self._id_token(thu_tu_raw, default="0")
            lk_enc = quote(str(line_key or ""), safe="")
            op_id = (
                f"KH{int(plan_id)}::DH{dh_token}::LK{lk_enc}::TP{tp_token}::BTP{btp_token}"
                f"::CD{str(ma_cd or '')}::T{tts_token}"
            )
            if machine:
                op_id = op_id + f"::M{str(machine)}"

            out[str(op_id)] = due
        return out


@router.post("/{plan_id}/ai-optimize", summary="AI tối ưu kế hoạch sản xuất (proposals + validate + KPI)")
def ai_optimize_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    horizon_start: datetime = Query(..., description="ISO datetime"),
    horizon_end: datetime = Query(..., description="ISO datetime"),
    top_n: int = Query(3, ge=1, le=20),
):
    inner = KeHoachPlanBuilder()
    builder = _BusyIntervalsPlanBuilder(inner, db=db, plan_id=int(plan_id))
    svc = PlanAIService(plan_builder=builder)
    return svc.analyze_and_propose(db, plan_id, horizon_start, horizon_end, top_n=top_n)
