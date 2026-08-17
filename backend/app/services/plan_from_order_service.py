from __future__ import annotations

from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

# Scheduler core (greedy) was referenced as `app.ai_forecast.scheduler_core` in an earlier version.
# In this workspace the module may not exist; keep a safe fallback with a clear error.
try:
    from ..ai_forecast.scheduler_core import Operation, ResourceType, schedule_greedy  # type: ignore
except Exception:  # pragma: no cover
    try:
        # Optional fallback if scheduler_core is placed elsewhere in your project
        from ..utils.scheduler_core import Operation, ResourceType, schedule_greedy  # type: ignore
    except Exception as e:  # pragma: no cover
        raise ImportError(
            "Missing scheduler module. Expected `backend/app/ai_forecast/scheduler_core.py` (or utils/scheduler_core.py) "
            "providing Operation, ResourceType, schedule_greedy."
        ) from e

from ..models.dinh_muc import DinhMucSanPham, TP_BTP
from ..models.don_hang import DonHangSX, DonHangSXChiTiet
from ..models.ke_hoach import KeHoachSX, KeHoachSX_ChiTiet
from ..models.nguon_luc import DM_CongDoan_CongDoanLon, DM_CongDoan_Nguon_Luc
from ..repositories.ke_hoach_repo import KeHoachRepo


def _now_vn() -> datetime:
    # DB/FE currently uses naive dates; treat as Asia/Ho_Chi_Minh for now
    return datetime.now()


def _as_due_dt(d: Optional[datetime | object], fallback_days: int = 7) -> datetime:
    """Normalize due date to end-of-day naive datetime."""

    if isinstance(d, datetime):
        dd = d.date()
    elif d is None:
        dd = None
    else:
        # SQLAlchemy Date type usually behaves like `datetime.date`
        try:
            dd = d
        except Exception:
            dd = None

    if dd is None:
        return _now_vn().replace(second=0, microsecond=0) + timedelta(days=fallback_days)

    return datetime.combine(dd, datetime.min.time()) + timedelta(hours=23, minutes=59)


def _make_line_key(don_hang_id: int, tp_dinh_muc_id: int, due_dt: datetime) -> str:
    return f"DH{don_hang_id}::TP{tp_dinh_muc_id}::DUE{due_dt.date().isoformat()}"


def _fmt_makh(ngay_lap: datetime, seq_in_month: int) -> str:
    return f"KHSX/{ngay_lap.month:02d}/{ngay_lap.year:04d}/{seq_in_month:03d}"


def _month_window(dt: datetime) -> tuple[datetime, datetime]:
    start = datetime(dt.year, dt.month, 1)
    end = datetime(dt.year + (1 if dt.month == 12 else 0), 1 if dt.month == 12 else (dt.month + 1), 1)
    return start, end


def _next_seq_in_month_by_count(db: Session, ngay_lap: datetime) -> int:
    """Return next sequence number using current count in month as primary rule.

    Business expectation:
    - If there is 1 plan currently in month list, the next new plan should be .../002.
    """

    start, end = _month_window(ngay_lap)
    rows = db.execute(
        text(
            """
            SELECT MaKeHoach
            FROM KeHoachSX
            WHERE NgayLap >= :start AND NgayLap < :end
            """
        ),
        {"start": start, "end": end},
    ).fetchall()

    used_seq: set[int] = set()
    for row in rows:
        try:
            mk = str(row[0] or "").strip()
            if len(mk) >= 3 and mk[-3:].isdigit():
                used_seq.add(int(mk[-3:]))
        except Exception:
            continue

    candidate = int(len(rows)) + 1
    while candidate in used_seq:
        candidate += 1
    return max(1, candidate)


def _generate_makh(db: Session, ngay_lap: datetime) -> str:
    next_seq = _next_seq_in_month_by_count(db, ngay_lap)
    return _fmt_makh(ngay_lap, next_seq)


def create_plan_from_order(db: Session, don_hang_id: int) -> dict:
    order: DonHangSX | None = db.get(DonHangSX, don_hang_id)
    if not order:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn hàng")

    if (order.TinhTrangDonHang or "").strip() != "Chưa lập kế hoạch":
        raise HTTPException(status_code=409, detail="Đơn hàng không ở trạng thái 'Chưa lập kế hoạch'")

    details: List[DonHangSXChiTiet] = list(order.chi_tiets or [])
    if not details:
        raise HTTPException(status_code=409, detail="Đơn hàng chưa có dòng sản phẩm")

    # Header date bounds from due
    due_dts: List[datetime] = []
    for d in details:
        due_dts.append(_as_due_dt(d.NgayGiaoHang or order.NgayGiaoHang))

    today = _now_vn().date()

    # Business rule: DenNgay inherits from order due date (NgayGiaoHang).
    # To be safe and consistent, take the latest due across header + all lines.
    all_due_dates = []
    if order.NgayGiaoHang is not None:
        all_due_dates.append(order.NgayGiaoHang)
    for d in details:
        if d.NgayGiaoHang is not None:
            all_due_dates.append(d.NgayGiaoHang)

    if all_due_dates:
        den_ngay = max(all_due_dates)
    else:
        den_ngay = max([x.date() for x in due_dts], default=today)

    # TuNgay should start from today so we can spread production up to DenNgay.
    tu_ngay = today
    if tu_ngay > den_ngay:
        tu_ngay = den_ngay

    # Create plan header (must include MaKeHoach)
    ngay_lap = _now_vn()

    # Retry on unique conflict
    last_err = None
    for _ in range(3):
        try:
            plan = KeHoachSX(
                MaKeHoach=_generate_makh(db, ngay_lap),
                NgayLap=ngay_lap,
                TuNgay=tu_ngay,
                DenNgay=den_ngay,
                TrangThai="Chưa sản xuất",
            )
            db.add(plan)
            db.flush()
            break
        except IntegrityError as e:
            db.rollback()
            last_err = e
            # regenerate by moving sequence forward
            continue

    if last_err and not getattr(plan, "KeHoachID", None):
        raise last_err

    # -------- Master data --------
    tp_ids = [int(x.DinhMucID) for x in details]

    links = db.query(TP_BTP).filter(TP_BTP.TP_DinhMucID.in_(tp_ids)).all()
    links_by_tp: Dict[int, List[TP_BTP]] = {}
    for l in links:
        links_by_tp.setdefault(int(l.TP_DinhMucID), []).append(l)
    for tp, arr in links_by_tp.items():
        arr.sort(key=lambda x: (x.ThuTuSX or 0, x.BTP_DinhMucID))

    btp_ids = sorted({int(l.BTP_DinhMucID) for l in links})
    if not btp_ids:
        raise HTTPException(status_code=409, detail="Không có định mức TP→BTP cho các TP trong đơn")

    products = db.query(DinhMucSanPham).filter(DinhMucSanPham.DinhMucID.in_(btp_ids)).all()
    prod_by_id: Dict[int, DinhMucSanPham] = {int(p.DinhMucID): p for p in products}

    cdcl = db.query(DM_CongDoan_CongDoanLon).all()
    cdl_by_cd: Dict[str, str] = {str(x.MaCongDoan): str(x.MaCongDoanLon) for x in cdcl if x.MaCongDoan}

    cd_nl = db.query(DM_CongDoan_Nguon_Luc).all()
    resources_by_cd: Dict[str, List[str]] = {}
    for m in cd_nl:
        if not m.MaCongDoan or not m.MaNguonLuc:
            continue
        resources_by_cd.setdefault(str(m.MaCongDoan), []).append(str(m.MaNguonLuc))

    # -------- Seed busy intervals from existing plans --------
    busy_machine: Dict[str, List[Tuple[datetime, datetime]]] = {}
    busy_dept: Dict[str, List[Tuple[datetime, datetime]]] = {}

    busy_sql = text(
        """
        SELECT ct.MaNguonLuc, mapcd.MaCongDoanLon, ct.StartDT, ct.EndDT
        FROM KeHoachSX_ChiTiet ct
        LEFT JOIN DM_CongDoan_CongDoanLon mapcd ON mapcd.MaCongDoan = ct.MaCongDoan
        WHERE ct.KeHoachID <> :ke_hoach_id
          AND ct.SegmentType = 'RUN'
          AND ct.StartDT IS NOT NULL
          AND ct.EndDT IS NOT NULL
        """
    )
    for r in db.execute(busy_sql, {"ke_hoach_id": int(plan.KeHoachID)}).mappings().all():
        st = r.get("StartDT")
        en = r.get("EndDT")
        if not st or not en:
            continue
        m = r.get("MaNguonLuc")
        d = r.get("MaCongDoanLon")
        if m:
            busy_machine.setdefault(str(m), []).append((st, en))
        if d:
            busy_dept.setdefault(str(d), []).append((st, en))

    # -------- Build operations --------
    ops: List[Operation] = []
    ops_by_line_tp: Dict[Tuple[str, int], List[Operation]] = {}

    for line in details:
        tp_id = int(line.DinhMucID)
        so_luong_tp = float(line.SoLuongDatHang or 0)
        due_dt = _as_due_dt(line.NgayGiaoHang or order.NgayGiaoHang)

        line_key = _make_line_key(int(order.DonHangID), tp_id, due_dt)

        btp_links = links_by_tp.get(tp_id, [])
        if not btp_links:
            continue

        for l in btp_links:
            btp_id = int(l.BTP_DinhMucID)
            btp = prod_by_id.get(btp_id)
            if not btp:
                continue

            if (not btp.MaCongDoan) or (btp.DinhMucThoiGian is None):
                raise HTTPException(
                    status_code=409,
                    detail=f"Thiếu MaCongDoan/DinhMucThoiGian cho BTP DinhMucID={btp_id}",
                )

            # SoLuongSX (BTP) = SoLuongDatHang(TP) * DinhLuong(BTP)
            dinh_luong = float(btp.DinhLuong or 1)
            qty_btp = so_luong_tp * dinh_luong

            ma_cd = (btp.MaCongDoan or "").strip()
            ma_cdl = (cdl_by_cd.get(ma_cd) or "").strip()

            allowed_res = resources_by_cd.get(ma_cd, [])
            allowed_res = [str(x) for x in allowed_res if x]
            # Hint: pick the first explicit resource (not M000) if any
            explicit = [x for x in allowed_res if str(x).strip().upper() != "M000"]
            ma_nl_hint = explicit[0] if explicit else None

            # Determine resource type:
            # - Any explicit Mxxx => MACHINE
            # - Else any explicit resource (e.g. NLxxx) => LABOR (but with resource-id non-overlap)
            # - Else => LABOR (dept-only)
            resource_type = ResourceType.LABOR
            if any(str(x).strip().upper().startswith("M") for x in explicit):
                resource_type = ResourceType.MACHINE

            run_minutes = int(float(btp.DinhMucThoiGian or 0))
            setup_minutes = 0

            op = Operation(
                op_id=f"DH{int(order.DonHangID)}_TP{tp_id}_BTP{btp_id}_CD{ma_cd}_T{int(l.ThuTuSX or 0)}",
                line_key=line_key,
                don_hang_id=int(order.DonHangID),
                tp_dinh_muc_id=tp_id,
                btp_dinh_muc_id=btp_id,
                ma_cong_doan=ma_cd,
                ma_cong_doan_lon=ma_cdl,
                thu_tu_sx=int(l.ThuTuSX or 0),
                qty=qty_btp,
                run_minutes=run_minutes,
                setup_minutes=setup_minutes,
                due_dt=due_dt,
                ma_nguon_luc_hint=ma_nl_hint,
                resource_type=resource_type,
                allowed_machines=[str(x) for x in allowed_res if x],
            )

            ops.append(op)
            ops_by_line_tp.setdefault((line_key, tp_id), []).append(op)

    if not ops:
        raise HTTPException(status_code=409, detail="Không tạo được operations (thiếu định mức hoặc dữ liệu)")

    # precedence by ThuTuSX within same (line_key, tp)
    for (_lk, _tp_id), arr in ops_by_line_tp.items():
        arr.sort(key=lambda x: (int(x.thu_tu_sx or 0), str(x.ma_cong_doan or "")))
        prev_id: Optional[str] = None
        for op in arr:
            if prev_id:
                op.predecessors.append(prev_id)
            prev_id = op.op_id

    # -------- Schedule (hour/minute resolution) --------
    # Enforce hard window: start at 00:00 of TuNgay, end at 23:59 of DenNgay.
    # This ensures we don't create plans that violate delivery-date constraints.
    horizon_start = datetime.combine(plan.TuNgay, time(0, 0)).replace(second=0, microsecond=0)
    horizon_end = datetime.combine(plan.DenNgay, time(23, 59)).replace(second=0, microsecond=0)
    if horizon_end <= horizon_start:
        horizon_end = horizon_start + timedelta(minutes=1)

    segs = schedule_greedy(
        operations=ops,
        start_dt=horizon_start,
        horizon_end=horizon_end,
        busy_by_machine=busy_machine,
        busy_by_dept=busy_dept,
    )

    # Soft validation: keep creating plan even when due/horizon cannot be fully satisfied.
    # We return warnings so users can still proceed and then optimize/adjust.
    warnings: List[str] = []
    late_violations: List[str] = []
    horizon_violations: List[str] = []
    max_end_dt: Optional[datetime] = None

    for s in segs or []:
        try:
            if s.end_dt and (max_end_dt is None or s.end_dt > max_end_dt):
                max_end_dt = s.end_dt

            if s.end_dt and s.end_dt > horizon_end:
                horizon_violations.append(
                    f"Vượt phạm vi kế hoạch: {s.op_id} kết thúc {s.end_dt} > {horizon_end}"
                )

            if s.end_dt and s.due_dt and s.end_dt > s.due_dt:
                late_violations.append(
                    f"Vượt ngày giao: {s.op_id} kết thúc {s.end_dt} > hạn {s.due_dt}"
                )
        except Exception:
            continue

    # If real schedule exceeds current DenNgay, auto-extend header DenNgay to keep consistency.
    if max_end_dt is not None:
        try:
            if max_end_dt.date() > plan.DenNgay:
                old_den = plan.DenNgay
                plan.DenNgay = max_end_dt.date()
                warnings.append(
                    f"Đã tự động nới DenNgay từ {old_den} lên {plan.DenNgay} để phù hợp lịch sản xuất thực tế."
                )
        except Exception:
            pass

    if late_violations:
        warnings.append(
            "Kế hoạch có công đoạn trễ hạn giao; cần tối ưu tăng ca/nguồn lực hoặc điều chỉnh lịch giao."
        )
    if horizon_violations:
        warnings.append(
            "Kế hoạch ban đầu vượt phạm vi thời gian header; hệ thống đã tự điều chỉnh phạm vi khi có thể."
        )

    # -------- Persist segments --------
    segment_id = 1
    for s in segs:
        row = KeHoachSX_ChiTiet(
            KeHoachID=int(plan.KeHoachID),
            SegmentID=segment_id,
            SegmentType="RUN",
            DonHangID=s.don_hang_id,
            LineKey=s.line_key,
            TP_DinhMucID=s.tp_dinh_muc_id,
            BTP_DinhMucID=s.btp_dinh_muc_id,
            ThuTuSX=s.thu_tu_sx,
            MaCongDoan=s.ma_cong_doan,
            MaCongDoanLon=s.ma_cong_doan_lon,
            MaNguonLuc=s.ma_nguon_luc,
            SoLuongSX=int(round(float(getattr(s, "so_luong_sx", 0) or 0))),
            LaborUsed=None,
            SetupMinutes=s.setup_minutes,
            RunMinutes=s.run_minutes,
            StartDT=s.start_dt,
            EndDT=s.end_dt,
            DueDT=s.due_dt,
            Note=s.note,
        )
        db.add(row)
        segment_id += 1

    order.TinhTrangDonHang = "Đã lập kế hoạch"

    db.commit()
    db.refresh(plan)

    # Post-create diagnostics against business constraints on quantity/capacity/due date.
    violation_items: List[dict] = []
    try:
        cal = KeHoachRepo.get_calendar_view(db, int(plan.KeHoachID)) or {}
        for row in (cal.get("Rows") or []):
            late_qty = int(float(row.get("LateQty") or 0))
            unplanned_qty = int(float(row.get("UnplannedQty") or 0))
            overcap_qty = int(float(row.get("OverCapacityQty") or 0))
            if late_qty <= 0 and unplanned_qty <= 0 and overcap_qty <= 0:
                continue
            violation_items.append(
                {
                    "don_hang_id": row.get("DonHangID"),
                    "tp": {
                        "code": row.get("MaTP"),
                        "name": row.get("TenThanhPham"),
                    },
                    "btp": {
                        "code": row.get("MaBTP"),
                        "name": row.get("TenBanThanhPham"),
                    },
                    "dept": {
                        "code": row.get("MaCongDoanLon"),
                        "name": row.get("TenBoPhan"),
                    },
                    "resource": {
                        "code": row.get("MaNguonLuc"),
                        "name": row.get("TenNguonLuc"),
                    },
                    "late_qty": late_qty,
                    "unplanned_qty": unplanned_qty,
                    "over_capacity_qty": overcap_qty,
                    "due_date": row.get("DueDT"),
                }
            )
    except Exception:
        violation_items = []

    if violation_items:
        warnings.append(
            "Kế hoạch mới còn vi phạm ràng buộc sản lượng/công suất/due-date; cần dùng AI phân tích để đề xuất phương án điều chỉnh."
        )

    return {
        "ke_hoach_id": int(plan.KeHoachID),
        "segment_count": len(segs),
        "warnings": warnings,
        "late_violations_count": len(late_violations),
        "horizon_violations_count": len(horizon_violations),
        "violations_sample": (late_violations + horizon_violations)[:20],
        "constraint_violations_count": len(violation_items),
        "constraint_violations": violation_items[:100],
    }


def create_plan_from_orders(db: Session, don_hang_ids: List[int]) -> dict:
    ids = [int(x) for x in (don_hang_ids or []) if int(x) > 0]
    # keep user order but remove duplicates
    seen: set[int] = set()
    ids = [x for x in ids if not (x in seen or seen.add(x))]

    if not ids:
        raise HTTPException(status_code=422, detail="Danh sách đơn hàng trống")

    orders: List[DonHangSX] = (
        db.query(DonHangSX).filter(DonHangSX.DonHangID.in_(ids)).all()  # type: ignore[attr-defined]
    )
    by_id: Dict[int, DonHangSX] = {int(o.DonHangID): o for o in orders}

    missing = [x for x in ids if x not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy đơn hàng: {missing}")

    not_ready: List[int] = []
    empty_detail: List[int] = []
    all_details: List[Tuple[DonHangSX, DonHangSXChiTiet]] = []
    for x in ids:
        order = by_id[x]
        if (order.TinhTrangDonHang or "").strip() != "Chưa lập kế hoạch":
            not_ready.append(int(order.DonHangID))
            continue
        details: List[DonHangSXChiTiet] = list(order.chi_tiets or [])
        if not details:
            empty_detail.append(int(order.DonHangID))
            continue
        for d in details:
            all_details.append((order, d))

    if not_ready:
        raise HTTPException(
            status_code=409,
            detail=f"Đơn hàng không ở trạng thái 'Chưa lập kế hoạch': {sorted(set(not_ready))}",
        )
    if empty_detail:
        raise HTTPException(
            status_code=409,
            detail=f"Đơn hàng chưa có dòng sản phẩm: {sorted(set(empty_detail))}",
        )

    today = _now_vn().date()

    all_due_dates = []
    for order_id in ids:
        order = by_id[order_id]
        if order.NgayGiaoHang is not None:
            all_due_dates.append(order.NgayGiaoHang)
        for d in (order.chi_tiets or []):
            if getattr(d, "NgayGiaoHang", None) is not None:
                all_due_dates.append(d.NgayGiaoHang)

    if all_due_dates:
        den_ngay = max(all_due_dates)
    else:
        # fallback: use latest normalized due dt among all detail lines
        due_dts = [_as_due_dt(d.NgayGiaoHang or o.NgayGiaoHang) for (o, d) in all_details]
        den_ngay = max([x.date() for x in due_dts], default=today)

    tu_ngay = today
    if tu_ngay > den_ngay:
        tu_ngay = den_ngay

    # Create one combined plan header
    ngay_lap = _now_vn()
    last_err = None
    plan = None
    for _ in range(3):
        try:
            plan = KeHoachSX(
                MaKeHoach=_generate_makh(db, ngay_lap),
                NgayLap=ngay_lap,
                TuNgay=tu_ngay,
                DenNgay=den_ngay,
                TrangThai="Chưa sản xuất",
            )
            db.add(plan)
            db.flush()
            break
        except IntegrityError as e:
            db.rollback()
            last_err = e
            continue

    if plan is None or not getattr(plan, "KeHoachID", None):
        if last_err:
            raise last_err
        raise HTTPException(status_code=500, detail="Không thể tạo kế hoạch")

    # -------- Master data --------
    tp_ids = [int(d.DinhMucID) for (_o, d) in all_details]

    links = db.query(TP_BTP).filter(TP_BTP.TP_DinhMucID.in_(tp_ids)).all()
    links_by_tp: Dict[int, List[TP_BTP]] = {}
    for l in links:
        links_by_tp.setdefault(int(l.TP_DinhMucID), []).append(l)
    for tp, arr in links_by_tp.items():
        arr.sort(key=lambda x: (x.ThuTuSX or 0, x.BTP_DinhMucID))

    btp_ids = sorted({int(l.BTP_DinhMucID) for l in links})
    if not btp_ids:
        raise HTTPException(status_code=409, detail="Không có định mức TP→BTP cho các TP trong các đơn")

    products = db.query(DinhMucSanPham).filter(DinhMucSanPham.DinhMucID.in_(btp_ids)).all()
    prod_by_id: Dict[int, DinhMucSanPham] = {int(p.DinhMucID): p for p in products}

    cdcl = db.query(DM_CongDoan_CongDoanLon).all()
    cdl_by_cd: Dict[str, str] = {str(x.MaCongDoan): str(x.MaCongDoanLon) for x in cdcl if x.MaCongDoan}

    cd_nl = db.query(DM_CongDoan_Nguon_Luc).all()
    resources_by_cd: Dict[str, List[str]] = {}
    for m in cd_nl:
        if not m.MaCongDoan or not m.MaNguonLuc:
            continue
        resources_by_cd.setdefault(str(m.MaCongDoan), []).append(str(m.MaNguonLuc))

    # -------- Seed busy intervals from existing plans --------
    busy_machine: Dict[str, List[Tuple[datetime, datetime]]] = {}
    busy_dept: Dict[str, List[Tuple[datetime, datetime]]] = {}

    busy_sql = text(
        """
        SELECT ct.MaNguonLuc, mapcd.MaCongDoanLon, ct.StartDT, ct.EndDT
        FROM KeHoachSX_ChiTiet ct
        LEFT JOIN DM_CongDoan_CongDoanLon mapcd ON mapcd.MaCongDoan = ct.MaCongDoan
        WHERE ct.KeHoachID <> :ke_hoach_id
          AND ct.SegmentType = 'RUN'
          AND ct.StartDT IS NOT NULL
          AND ct.EndDT IS NOT NULL
        """
    )
    for r in db.execute(busy_sql, {"ke_hoach_id": int(plan.KeHoachID)}).mappings().all():
        st = r.get("StartDT")
        en = r.get("EndDT")
        if not st or not en:
            continue
        m = r.get("MaNguonLuc")
        d = r.get("MaCongDoanLon")
        if m:
            busy_machine.setdefault(str(m), []).append((st, en))
        if d:
            busy_dept.setdefault(str(d), []).append((st, en))

    # -------- Build operations --------
    ops: List[Operation] = []
    ops_by_line_tp: Dict[Tuple[str, int], List[Operation]] = {}
    ops_count_by_order: Dict[int, int] = {int(x): 0 for x in ids}

    for order_id in ids:
        order = by_id[order_id]
        details: List[DonHangSXChiTiet] = list(order.chi_tiets or [])
        for line in details:
            tp_id = int(line.DinhMucID)
            so_luong_tp = float(line.SoLuongDatHang or 0)
            due_dt = _as_due_dt(line.NgayGiaoHang or order.NgayGiaoHang)

            line_key = _make_line_key(int(order.DonHangID), tp_id, due_dt)

            btp_links = links_by_tp.get(tp_id, [])
            if not btp_links:
                continue

            for l in btp_links:
                btp_id = int(l.BTP_DinhMucID)
                btp = prod_by_id.get(btp_id)
                if not btp:
                    continue

                if (not btp.MaCongDoan) or (btp.DinhMucThoiGian is None):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Thiếu MaCongDoan/DinhMucThoiGian cho BTP DinhMucID={btp_id}",
                    )

                dinh_luong = float(btp.DinhLuong or 1)
                qty_btp = so_luong_tp * dinh_luong

                ma_cd = (btp.MaCongDoan or "").strip()
                ma_cdl = (cdl_by_cd.get(ma_cd) or "").strip()

                allowed_res = resources_by_cd.get(ma_cd, [])
                allowed_res = [str(x) for x in allowed_res if x]
                explicit = [x for x in allowed_res if str(x).strip().upper() != "M000"]
                ma_nl_hint = explicit[0] if explicit else None

                resource_type = ResourceType.LABOR
                if any(str(x).strip().upper().startswith("M") for x in explicit):
                    resource_type = ResourceType.MACHINE

                run_minutes = int(float(btp.DinhMucThoiGian or 0))
                setup_minutes = 0

                op = Operation(
                    op_id=f"DH{int(order.DonHangID)}_TP{tp_id}_BTP{btp_id}_CD{ma_cd}_T{int(l.ThuTuSX or 0)}",
                    line_key=line_key,
                    don_hang_id=int(order.DonHangID),
                    tp_dinh_muc_id=tp_id,
                    btp_dinh_muc_id=btp_id,
                    ma_cong_doan=ma_cd,
                    ma_cong_doan_lon=ma_cdl,
                    thu_tu_sx=int(l.ThuTuSX or 0),
                    qty=qty_btp,
                    run_minutes=run_minutes,
                    setup_minutes=setup_minutes,
                    due_dt=due_dt,
                    ma_nguon_luc_hint=ma_nl_hint,
                    resource_type=resource_type,
                    allowed_machines=[str(x) for x in allowed_res if x],
                )

                ops.append(op)
                ops_by_line_tp.setdefault((line_key, tp_id), []).append(op)
                ops_count_by_order[int(order.DonHangID)] = ops_count_by_order.get(int(order.DonHangID), 0) + 1

    no_ops_orders = sorted([oid for oid, cnt in ops_count_by_order.items() if cnt <= 0])
    if no_ops_orders:
        raise HTTPException(
            status_code=409,
            detail=f"Không tạo được operations cho các đơn: {no_ops_orders} (thiếu định mức TP→BTP hoặc dữ liệu)",
        )

    # precedence by ThuTuSX within same (line_key, tp)
    for (_lk, _tp_id), arr in ops_by_line_tp.items():
        arr.sort(key=lambda x: (int(x.thu_tu_sx or 0), str(x.ma_cong_doan or "")))
        prev_id: Optional[str] = None
        for op in arr:
            if prev_id:
                op.predecessors.append(prev_id)
            prev_id = op.op_id

    # -------- Schedule --------
    horizon_start = datetime.combine(plan.TuNgay, time(0, 0)).replace(second=0, microsecond=0)
    horizon_end = datetime.combine(plan.DenNgay, time(23, 59)).replace(second=0, microsecond=0)
    if horizon_end <= horizon_start:
        horizon_end = horizon_start + timedelta(minutes=1)

    segs = schedule_greedy(
        operations=ops,
        start_dt=horizon_start,
        horizon_end=horizon_end,
        busy_by_machine=busy_machine,
        busy_by_dept=busy_dept,
    )

    warnings: List[str] = []
    late_violations: List[str] = []
    horizon_violations: List[str] = []
    max_end_dt: Optional[datetime] = None

    for s in segs or []:
        try:
            if s.end_dt and (max_end_dt is None or s.end_dt > max_end_dt):
                max_end_dt = s.end_dt

            if s.end_dt and s.end_dt > horizon_end:
                horizon_violations.append(
                    f"Vượt phạm vi kế hoạch: {s.op_id} kết thúc {s.end_dt} > {horizon_end}"
                )

            if s.end_dt and s.due_dt and s.end_dt > s.due_dt:
                late_violations.append(
                    f"Vượt ngày giao: {s.op_id} kết thúc {s.end_dt} > hạn {s.due_dt}"
                )
        except Exception:
            continue

    if max_end_dt is not None:
        try:
            if max_end_dt.date() > plan.DenNgay:
                old_den = plan.DenNgay
                plan.DenNgay = max_end_dt.date()
                warnings.append(
                    f"Đã tự động nới DenNgay từ {old_den} lên {plan.DenNgay} để phù hợp lịch sản xuất thực tế."
                )
        except Exception:
            pass

    if late_violations:
        warnings.append(
            "Kế hoạch có công đoạn trễ hạn giao; cần tối ưu tăng ca/nguồn lực hoặc điều chỉnh lịch giao."
        )
    if horizon_violations:
        warnings.append(
            "Kế hoạch ban đầu vượt phạm vi thời gian header; hệ thống đã tự điều chỉnh phạm vi khi có thể."
        )

    # -------- Persist segments --------
    segment_id = 1
    for s in segs:
        row = KeHoachSX_ChiTiet(
            KeHoachID=int(plan.KeHoachID),
            SegmentID=segment_id,
            SegmentType="RUN",
            DonHangID=s.don_hang_id,
            LineKey=s.line_key,
            TP_DinhMucID=s.tp_dinh_muc_id,
            BTP_DinhMucID=s.btp_dinh_muc_id,
            ThuTuSX=s.thu_tu_sx,
            MaCongDoan=s.ma_cong_doan,
            MaCongDoanLon=s.ma_cong_doan_lon,
            MaNguonLuc=s.ma_nguon_luc,
            SoLuongSX=int(round(float(getattr(s, "so_luong_sx", 0) or 0))),
            LaborUsed=None,
            SetupMinutes=s.setup_minutes,
            RunMinutes=s.run_minutes,
            StartDT=s.start_dt,
            EndDT=s.end_dt,
            DueDT=s.due_dt,
            Note=s.note,
        )
        db.add(row)
        segment_id += 1

    for order_id in ids:
        by_id[order_id].TinhTrangDonHang = "Đã lập kế hoạch"

    db.commit()
    db.refresh(plan)

    violation_items: List[dict] = []
    try:
        cal = KeHoachRepo.get_calendar_view(db, int(plan.KeHoachID)) or {}
        for row in (cal.get("Rows") or []):
            late_qty = int(float(row.get("LateQty") or 0))
            unplanned_qty = int(float(row.get("UnplannedQty") or 0))
            overcap_qty = int(float(row.get("OverCapacityQty") or 0))
            if late_qty <= 0 and unplanned_qty <= 0 and overcap_qty <= 0:
                continue
            violation_items.append(
                {
                    "don_hang_id": row.get("DonHangID"),
                    "tp": {
                        "code": row.get("MaTP"),
                        "name": row.get("TenThanhPham"),
                    },
                    "btp": {
                        "code": row.get("MaBTP"),
                        "name": row.get("TenBanThanhPham"),
                    },
                    "dept": {
                        "code": row.get("MaCongDoanLon"),
                        "name": row.get("TenBoPhan"),
                    },
                    "resource": {
                        "code": row.get("MaNguonLuc"),
                        "name": row.get("TenNguonLuc"),
                    },
                    "late_qty": late_qty,
                    "unplanned_qty": unplanned_qty,
                    "over_capacity_qty": overcap_qty,
                    "due_date": row.get("DueDT"),
                }
            )
    except Exception:
        violation_items = []

    if violation_items:
        warnings.append(
            "Kế hoạch mới còn vi phạm ràng buộc sản lượng/công suất/due-date; cần dùng AI phân tích để đề xuất phương án điều chỉnh."
        )

    return {
        "ke_hoach_id": int(plan.KeHoachID),
        "segment_count": len(segs),
        "order_ids": ids,
        "order_count": len(ids),
        "warnings": warnings,
        "late_violations_count": len(late_violations),
        "horizon_violations_count": len(horizon_violations),
        "violations_sample": (late_violations + horizon_violations)[:20],
        "constraint_violations_count": len(violation_items),
        "constraint_violations": violation_items[:100],
    }
