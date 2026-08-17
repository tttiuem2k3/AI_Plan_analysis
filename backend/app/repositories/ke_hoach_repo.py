from __future__ import annotations

from datetime import date, datetime, timedelta
from collections.abc import Mapping
from typing import List, Optional

from sqlalchemy import text, bindparam
from sqlalchemy.orm import Session


def _safe_int_from_float(x, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(round(float(x)))
    except Exception:
        return default


def _compute_overtime_capacity_per_day(
    *,
    resource_count: int,
    setup_minutes: float,
    dinh_muc_thoi_gian_minutes: float,
    overtime_hours: float = 4.0,
) -> int:
    """Compute max extra quantity/day via overtime.

    Formula (as requested):
      Năng lực tăng ca = ((4*60 * Số lượng nguồn lực )- Thời gian thiết lập ) / (Thời gian định mức)
    """

    rc = int(resource_count or 0)
    if rc <= 0:
        return 0
    dm = float(dinh_muc_thoi_gian_minutes or 0)
    if dm <= 0:
        return 0
    setup = float(setup_minutes or 0)
    ot_minutes = overtime_hours * 60.0 * float(rc)
    cap = (ot_minutes - setup) / dm
    if cap <= 0:
        return 0
    return max(0, _safe_int_from_float(cap, 0))


def _alloc_sequential_capped(
    *,
    target_qty: int,
    idx_list: list[int],
    cap_int: int,
    has_capacity: bool,
) -> tuple[list[int], int]:
    """Allocate sequentially into idx_list with per-day cap."""

    if target_qty <= 0 or not idx_list:
        return [0] * len(idx_list), int(target_qty)

    plan = [0] * len(idx_list)

    # No capacity => level-load evenly.
    if not has_capacity:
        base = int(target_qty) // len(idx_list)
        rem = int(target_qty) - base * len(idx_list)
        for j in range(len(idx_list)):
            plan[j] = base + (1 if j < rem else 0)
        return plan, 0

    remaining_local = int(target_qty)
    for j in range(len(idx_list)):
        if remaining_local <= 0:
            break
        take = min(int(cap_int), remaining_local)
        plan[j] = int(take)
        remaining_local -= int(take)

    return plan, int(remaining_local)


def _alloc_overtime_backfill(
    *,
    remaining_qty: int,
    idx_pre_due: list[int],
    daily: list[int],
    overtime_cap_int: int,
) -> tuple[int, int]:
    """Backfill missing qty into pre-due days using overtime, capped per day.

    Strategy: allocate from latest pre-due day backwards (closest to due date first).
    """

    if remaining_qty <= 0:
        return int(remaining_qty), 0
    if overtime_cap_int <= 0 or not idx_pre_due:
        return int(remaining_qty), 0

    overtime_qty = 0
    rem = int(remaining_qty)
    for i in reversed(idx_pre_due):
        if rem <= 0:
            break
        take = min(int(overtime_cap_int), rem)
        daily[i] = int(daily[i]) + int(take)
        overtime_qty += int(take)
        rem -= int(take)

    return int(rem), int(overtime_qty)
from sqlalchemy.exc import IntegrityError

from ..models.ke_hoach import KeHoachSX, KeHoachSX_ChiTiet
from ..utils.ke_hoach_status import compute_plan_status


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

    We still avoid duplicates by checking existing MaKeHoach suffixes and moving forward when needed.
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


class KeHoachRepo:
    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100, search: Optional[str] = None):
        q = db.query(KeHoachSX)

        try:
            skip_i = max(0, int(skip or 0))
        except Exception:
            skip_i = 0
        try:
            limit_i = int(limit or 100)
        except Exception:
            limit_i = 100
        limit_i = max(1, min(1000, limit_i))

        # Lọc theo search: ưu tiên lọc theo mã kế hoạch / trạng thái hiển thị
        rows = (
            q.order_by(KeHoachSX.NgayLap.asc(), KeHoachSX.KeHoachID.asc())
            .offset(skip_i)
            .limit(limit_i)
            .all()
        )

        # Bulk fetch related order document numbers (SoChungTu) for all plans.
        plan_ids = [int(r.KeHoachID) for r in rows if getattr(r, "KeHoachID", None) is not None]
        so_ct_by_plan: dict[int, list[str]] = {}
        if plan_ids:
            try:
                sql = (
                    text(
                        """
                        SELECT c.KeHoachID AS KeHoachID, o.SoChungTu AS SoChungTu
                        FROM KeHoachSX_ChiTiet c
                        JOIN DM_DonHangSX o ON o.DonHangID = c.DonHangID
                        WHERE c.KeHoachID IN :ids
                        """
                    ).bindparams(bindparam("ids", expanding=True))
                )
                rs = db.execute(sql, {"ids": plan_ids}).fetchall()
                tmp: dict[int, set[str]] = {}
                for kehoach_id, so_ct in rs:
                    if kehoach_id is None or so_ct is None:
                        continue
                    pid = int(kehoach_id)
                    tmp.setdefault(pid, set()).add(str(so_ct))
                so_ct_by_plan = {pid: sorted(list(s)) for pid, s in tmp.items()}
            except Exception:
                so_ct_by_plan = {}

        # compute running sequence per month
        seq_map = {}  # (year, month) -> counter
        out = []
        for r in rows:
            ym = (r.NgayLap.year, r.NgayLap.month)
            seq_map[ym] = seq_map.get(ym, 0) + 1
            status = compute_plan_status(r.TuNgay, r.DenNgay)

            # Prefer persisted MaKeHoach; fallback to computed for legacy rows
            makh = getattr(r, "MaKeHoach", None) or _fmt_makh(r.NgayLap, seq_map[ym])
            so_ct_list = so_ct_by_plan.get(int(r.KeHoachID), [])
            so_ct = ", ".join(so_ct_list) if so_ct_list else ""

            out.append(
                {
                    "KeHoachID": int(r.KeHoachID),
                    "MaKeHoach": makh,
                    "SoChungTu": so_ct,
                    "NgayLap": r.NgayLap,
                    "TuNgay": r.TuNgay,
                    "DenNgay": r.DenNgay,
                    "TrangThai": status,
                }
            )

        # apply search after compute (so search can match MaKeHoach or TrangThai)
        if search:
            s = str(search).strip().lower()
            out = [
                x
                for x in out
                if (s in str(x.get("MaKeHoach", "")).lower()) or (s in str(x.get("TrangThai", "")).lower())
                or (s in str(x.get("SoChungTu", "")).lower())
            ]

        # apply pagination after compute
        out.sort(key=lambda x: (x["NgayLap"], x["KeHoachID"]))
        out = out[max(0, int(skip)) : max(0, int(skip)) + max(1, int(limit))]

        # return newest first like old behavior
        out.sort(key=lambda x: x["NgayLap"], reverse=True)
        return out

    @staticmethod
    def get_by_id(db: Session, ke_hoach_id: int):
        r = db.query(KeHoachSX).filter(KeHoachSX.KeHoachID == ke_hoach_id).first()
        if not r:
            return None

        # Related order document numbers
        so_ct = ""
        try:
            sql = text(
                """
                SELECT DISTINCT o.SoChungTu AS SoChungTu
                FROM KeHoachSX_ChiTiet c
                JOIN DM_DonHangSX o ON o.DonHangID = c.DonHangID
                WHERE c.KeHoachID = :id
                """
            )
            rs = db.execute(sql, {"id": int(ke_hoach_id)}).fetchall()
            docs = sorted({str(x[0]) for x in rs if x and x[0] is not None})
            so_ct = ", ".join(docs) if docs else ""
        except Exception:
            so_ct = ""

        # Prefer persisted MaKeHoach if present
        persisted = getattr(r, "MaKeHoach", None)
        if persisted:
            makh = persisted
        else:
            start, end = _month_window(r.NgayLap)
            ids_in_month = (
                db.query(KeHoachSX.KeHoachID)
                .filter(KeHoachSX.NgayLap >= start)
                .filter(KeHoachSX.NgayLap < end)
                .order_by(KeHoachSX.NgayLap.asc(), KeHoachSX.KeHoachID.asc())
                .all()
            )
            id_list = [int(x[0]) for x in ids_in_month]
            try:
                seq = id_list.index(int(r.KeHoachID)) + 1
            except ValueError:
                seq = 1
            makh = _fmt_makh(r.NgayLap, seq)

        return {
            "KeHoachID": int(r.KeHoachID),
            "MaKeHoach": makh,
            "SoChungTu": so_ct,
            "NgayLap": r.NgayLap,
            "TuNgay": r.TuNgay,
            "DenNgay": r.DenNgay,
            "TrangThai": compute_plan_status(r.TuNgay, r.DenNgay),
        }

    @staticmethod
    def create(db: Session, obj_in):
        data = obj_in.dict(exclude_unset=True) if hasattr(obj_in, "dict") else dict(obj_in or {})

        # Server-controlled fields
        data.pop("MaKeHoach", None)

        # Ensure NgayLap exists (used for MaKeHoach month/year)
        ngay_lap = data.get("NgayLap")
        if not ngay_lap:
            ngay_lap = datetime.now()
            data["NgayLap"] = ngay_lap

        # Retry on unique conflict (concurrent creates)
        last_err = None
        for _ in range(3):
            try:
                next_seq = _next_seq_in_month_by_count(db, ngay_lap)
                data["MaKeHoach"] = _fmt_makh(ngay_lap, next_seq)

                obj = KeHoachSX(**data)
                db.add(obj)
                db.commit()
                db.refresh(obj)
                return obj
            except IntegrityError as e:
                db.rollback()
                last_err = e
                continue

        # If still failing after retries, raise the last error
        if last_err:
            raise last_err

        obj = KeHoachSX(**data)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    @staticmethod
    def update(db: Session, ke_hoach_id: int, obj_in):
        obj = db.query(KeHoachSX).filter(KeHoachSX.KeHoachID == ke_hoach_id).first()
        if not obj:
            return None
        for k, v in obj_in.dict(exclude_unset=True).items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj

    @staticmethod
    def delete(db: Session, ke_hoach_id: int):
        obj = db.query(KeHoachSX).filter(KeHoachSX.KeHoachID == ke_hoach_id).first()
        if obj:
            db.delete(obj)
            db.commit()
        return obj

    @staticmethod
    def delete_plan_and_reset_orders(db: Session, ke_hoach_id: int) -> dict:
        """Rule:
        1) Xóa KeHoachSX_ChiTiet + KeHoachSX.
        2) Update các đơn hàng xuất hiện trong chi tiết kế hoạch về TinhTrangDonHang='Chưa lập kế hoạch'.
        """

        # Lấy danh sách DonHangID liên quan trước khi xóa
        order_ids = (
            db.query(KeHoachSX_ChiTiet.DonHangID)
            .filter(KeHoachSX_ChiTiet.KeHoachID == ke_hoach_id)
            .distinct()
            .all()
        )
        order_ids = [int(x[0]) for x in order_ids if x and x[0] is not None]

        # Xóa chi tiết
        db.query(KeHoachSX_ChiTiet).filter(KeHoachSX_ChiTiet.KeHoachID == ke_hoach_id).delete(synchronize_session=False)

        # Xóa header
        obj = db.query(KeHoachSX).filter(KeHoachSX.KeHoachID == ke_hoach_id).first()
        if obj:
            db.delete(obj)

        # Reset trạng thái đơn hàng
        if order_ids:
            # Không import model DonHangSX ở repo để tránh vòng lặp; dùng SQL thô
            reset_sql = (
                text(
                    """
                    UPDATE DM_DonHangSX
                    SET TinhTrangDonHang = N'Chưa lập kế hoạch'
                    WHERE DonHangID IN :ids
                    """
                ).bindparams(bindparam("ids", expanding=True))
            )
            db.execute(reset_sql, {"ids": [int(x) for x in order_ids]})

        db.commit()

        return {
            "deleted": bool(obj),
            "reset_order_ids": order_ids,
        }

    @staticmethod
    def get_chitiet_by_kehoach(db: Session, ke_hoach_id: int):
        rows = (
            db.query(KeHoachSX_ChiTiet)
            .filter(KeHoachSX_ChiTiet.KeHoachID == ke_hoach_id)
            .order_by(KeHoachSX_ChiTiet.SegmentID.asc())
            .all()
        )
        out = []
        for r in rows:
            out.append(
                {
                    "KeHoachID": int(r.KeHoachID),
                    "SegmentID": int(r.SegmentID),
                    "SegmentType": r.SegmentType,
                    "DonHangID": r.DonHangID,
                    "LineKey": r.LineKey,
                    "TP_DinhMucID": r.TP_DinhMucID,
                    "BTP_DinhMucID": r.BTP_DinhMucID,
                    "ThuTuSX": r.ThuTuSX,
                    "MaCongDoan": r.MaCongDoan,
                    "MaCongDoanLon": r.MaCongDoanLon,
                    "MaNguonLuc": r.MaNguonLuc,
                    "SoLuongSX": r.SoLuongSX,
                    "LaborUsed": r.LaborUsed,
                    "SetupMinutes": r.SetupMinutes,
                    "RunMinutes": r.RunMinutes,
                    "StartDT": r.StartDT,
                    "EndDT": r.EndDT,
                    "DueDT": r.DueDT,
                    "Note": r.Note,
                }
            )
        return out

    @staticmethod
    def get_btp_grouped(db: Session, ke_hoach_id: int):
        """Group segments by (DonHangID,LineKey,TP,BTP,ThuTuSX).

        Returns dict-like rows (mappings) that already contain all enriched
        display fields for the plan detail/calendar UI.

        Business rules:
        - Nguồn lực hiển thị theo nguồn lực THỰC TẾ đã gán trong KeHoachSX_ChiTiet.MaNguonLuc.
        - Nếu nguồn lực thực tế là M000 => hiển thị 'Nhân công'.
        - Số lượng nguồn lực:
            * Nếu có máy thực tế (MaNguonLuc != 'M000'): đếm số máy distinct đã gán.
            * Nếu chỉ có 'M000': lấy DM_CongDoanLon.SoNhanSu theo MaCongDoanLon.
        """

        sql = text(
            """
            WITH base AS (
                SELECT
                    ct.KeHoachID,
                    ct.DonHangID,
                    ct.LineKey,
                    ct.TP_DinhMucID,
                    ct.BTP_DinhMucID,
                    ct.ThuTuSX,
                    MIN(ct.MaCongDoan) AS MaCongDoan,
                    COALESCE(
                        MIN(ct.MaCongDoanLon),
                        MIN(mapcd.MaCongDoanLon)
                    ) AS MaCongDoanLon,

                    SUM(COALESCE(ct.SetupMinutes,0)) AS TotalSetup,
                    SUM(COALESCE(ct.RunMinutes,0)) AS TotalRun,
                    MAX(COALESCE(ct.SoLuongSX,0)) AS TotalQty,
                    MIN(ct.StartDT) AS Start,
                    MAX(ct.EndDT) AS [End],
                    MIN(ct.DueDT) AS DueDT
                FROM KeHoachSX_ChiTiet ct
                LEFT JOIN DM_CongDoan_CongDoanLon mapcd ON mapcd.MaCongDoan = ct.MaCongDoan
                WHERE ct.KeHoachID = :id
                GROUP BY
                    ct.KeHoachID,
                    ct.DonHangID,
                    ct.LineKey,
                    ct.TP_DinhMucID,
                    ct.BTP_DinhMucID,
                    ct.ThuTuSX
            ),
            assigned AS (
                SELECT
                    b.KeHoachID,
                    b.DonHangID,
                    b.LineKey,
                    b.TP_DinhMucID,
                    b.BTP_DinhMucID,
                    b.ThuTuSX,

                    -- comma-separated assigned machine names (exclude M000)
                    STUFF((
                        SELECT DISTINCT N', ' + COALESCE(nl2.TenNguonLuc, ct2.MaNguonLuc)
                        FROM KeHoachSX_ChiTiet ct2
                        LEFT JOIN DM_NguonLuc nl2 ON nl2.MaNguonLuc = ct2.MaNguonLuc
                        WHERE ct2.KeHoachID = b.KeHoachID
                          AND ct2.DonHangID = b.DonHangID
                          AND ct2.LineKey = b.LineKey
                          AND ct2.TP_DinhMucID = b.TP_DinhMucID
                          AND ct2.BTP_DinhMucID = b.BTP_DinhMucID
                          AND ct2.ThuTuSX = b.ThuTuSX
                          AND ct2.MaNguonLuc IS NOT NULL
                          AND ct2.MaNguonLuc <> 'M000'
                        FOR XML PATH(''), TYPE
                    ).value('.', 'NVARCHAR(MAX)'), 1, 2, N'') AS AssignedMachineText,

                    -- comma-separated assigned machine codes (exclude M000)
                    STUFF((
                        SELECT DISTINCT N', ' + COALESCE(ct2.MaNguonLuc, N'')
                        FROM KeHoachSX_ChiTiet ct2
                        WHERE ct2.KeHoachID = b.KeHoachID
                          AND ct2.DonHangID = b.DonHangID
                          AND ct2.LineKey = b.LineKey
                          AND ct2.TP_DinhMucID = b.TP_DinhMucID
                          AND ct2.BTP_DinhMucID = b.BTP_DinhMucID
                          AND ct2.ThuTuSX = b.ThuTuSX
                          AND ct2.MaNguonLuc IS NOT NULL
                          AND ct2.MaNguonLuc <> 'M000'
                        FOR XML PATH(''), TYPE
                    ).value('.', 'NVARCHAR(MAX)'), 1, 2, N'') AS AssignedMachineCodes,

                    -- count distinct assigned machines (exclude M000)
                    (
                        SELECT COUNT(1)
                        FROM (
                            SELECT DISTINCT ct2.MaNguonLuc
                            FROM KeHoachSX_ChiTiet ct2
                            WHERE ct2.KeHoachID = b.KeHoachID
                              AND ct2.DonHangID = b.DonHangID
                              AND ct2.LineKey = b.LineKey
                              AND ct2.TP_DinhMucID = b.TP_DinhMucID
                              AND ct2.BTP_DinhMucID = b.BTP_DinhMucID
                              AND ct2.ThuTuSX = b.ThuTuSX
                              AND ct2.MaNguonLuc IS NOT NULL
                              AND ct2.MaNguonLuc <> 'M000'
                        ) t
                    ) AS AssignedMachineCount,

                    -- labor flag (assigned)
                    CASE WHEN EXISTS(
                        SELECT 1
                        FROM KeHoachSX_ChiTiet ct2
                        WHERE ct2.KeHoachID = b.KeHoachID
                          AND ct2.DonHangID = b.DonHangID
                          AND ct2.LineKey = b.LineKey
                          AND ct2.TP_DinhMucID = b.TP_DinhMucID
                          AND ct2.BTP_DinhMucID = b.BTP_DinhMucID
                          AND ct2.ThuTuSX = b.ThuTuSX
                          AND ct2.MaNguonLuc = 'M000'
                    ) THEN 1 ELSE 0 END AS AssignedHasLabor,

                    -- total setup time across assigned machines (exclude M000)
                    COALESCE((
                        SELECT SUM(COALESCE(nl.ThoiGianThietLap,0))
                        FROM (
                            SELECT DISTINCT ct2.MaNguonLuc
                            FROM KeHoachSX_ChiTiet ct2
                            WHERE ct2.KeHoachID = b.KeHoachID
                              AND ct2.DonHangID = b.DonHangID
                              AND ct2.LineKey = b.LineKey
                              AND ct2.TP_DinhMucID = b.TP_DinhMucID
                              AND ct2.BTP_DinhMucID = b.BTP_DinhMucID
                              AND ct2.ThuTuSX = b.ThuTuSX
                              AND ct2.MaNguonLuc IS NOT NULL
                              AND ct2.MaNguonLuc <> 'M000'
                        ) x
                        LEFT JOIN DM_NguonLuc nl ON nl.MaNguonLuc = x.MaNguonLuc
                    ), 0) AS AssignedTotalSetupTime
                FROM base b
            ),
            res AS (
                SELECT
                    b.KeHoachID,
                    b.DonHangID,
                    b.LineKey,
                    b.TP_DinhMucID,
                    b.BTP_DinhMucID,
                    b.ThuTuSX,
                    b.MaCongDoan,
                    -- comma-separated machine names (exclude M000)
                    STUFF((
                        SELECT DISTINCT N', ' + COALESCE(nl2.TenNguonLuc, m2.MaNguonLuc)
                        FROM DM_CongDoan_Nguon_Luc m2
                        LEFT JOIN DM_NguonLuc nl2 ON nl2.MaNguonLuc = m2.MaNguonLuc
                        WHERE m2.MaCongDoan = b.MaCongDoan
                          AND m2.MaNguonLuc <> 'M000'
                        FOR XML PATH(''), TYPE
                    ).value('.', 'NVARCHAR(MAX)'), 1, 2, N'') AS MachineText,
                                        -- comma-separated machine codes (exclude M000)
                                        STUFF((
                                                SELECT DISTINCT N', ' + COALESCE(m2.MaNguonLuc, N'')
                                                FROM DM_CongDoan_Nguon_Luc m2
                                                WHERE m2.MaCongDoan = b.MaCongDoan
                                                    AND m2.MaNguonLuc <> 'M000'
                                                FOR XML PATH(''), TYPE
                                        ).value('.', 'NVARCHAR(MAX)'), 1, 2, N'') AS MachineCodes,
                    -- count machines (exclude M000)
                    (
                        SELECT COUNT(1)
                        FROM DM_CongDoan_Nguon_Luc m3
                        WHERE m3.MaCongDoan = b.MaCongDoan
                          AND m3.MaNguonLuc <> 'M000'
                    ) AS MachineCount,
                    -- labor flag
                    CASE WHEN EXISTS(
                        SELECT 1 FROM DM_CongDoan_Nguon_Luc m4
                        WHERE m4.MaCongDoan = b.MaCongDoan
                          AND m4.MaNguonLuc = 'M000'
                    ) THEN 1 ELSE 0 END AS HasLabor,
                    -- total setup time across available machines (exclude M000)
                    COALESCE((
                        SELECT SUM(COALESCE(nl.ThoiGianThietLap,0))
                        FROM DM_CongDoan_Nguon_Luc m
                        LEFT JOIN DM_NguonLuc nl ON nl.MaNguonLuc = m.MaNguonLuc
                        WHERE m.MaCongDoan = b.MaCongDoan
                          AND m.MaNguonLuc <> 'M000'
                    ), 0) AS TotalSetupTime
                FROM base b
            )
            SELECT
                b.DonHangID,
                dh.SoChungTu AS SoChungTu,
                dh.KhachHang AS KhachHang,
                b.LineKey,
                b.TP_DinhMucID,
                b.BTP_DinhMucID,
                b.ThuTuSX,
                b.MaCongDoan,
                b.MaCongDoanLon,

                -- Nguồn lực display: prefer assigned resource from segments
                CASE
                    WHEN COALESCE(a.AssignedMachineCount,0) > 0 THEN a.AssignedMachineText
                    WHEN COALESCE(a.AssignedHasLabor,0) = 1 THEN N'Nhân công'
                    WHEN COALESCE(r.MachineCount,0) > 0 THEN r.MachineText
                    WHEN COALESCE(r.HasLabor,0) = 1 THEN N'Nhân công'
                    ELSE NULL
                END AS NguonLucText,
                CASE
                    WHEN COALESCE(a.AssignedMachineCount,0) > 0 THEN a.AssignedMachineCodes
                    WHEN COALESCE(a.AssignedHasLabor,0) = 1 THEN N'M000'
                    WHEN COALESCE(r.MachineCount,0) > 0 THEN r.MachineCodes
                    WHEN COALESCE(r.HasLabor,0) = 1 THEN N'M000'
                    ELSE NULL
                END AS MaNguonLuc,
                CASE
                    WHEN COALESCE(a.AssignedMachineCount,0) > 0 THEN a.AssignedMachineText
                    WHEN COALESCE(a.AssignedHasLabor,0) = 1 THEN N'Nhân công'
                    WHEN COALESCE(r.MachineCount,0) > 0 THEN r.MachineText
                    WHEN COALESCE(r.HasLabor,0) = 1 THEN N'Nhân công'
                    ELSE NULL
                END AS TenNguonLuc,

                b.TotalSetup,
                b.TotalRun,
                b.TotalQty,
                b.Start,
                b.[End],
                b.DueDT,

                tp.MaSanPham AS MaThanhPham,
                tp.TenSanPham  AS TenThanhPham,
                btp.MaSanPham AS MaBanThanhPham,
                btp.TenSanPham AS TenBanThanhPham,
                cdl.TenCongDoanLon AS TenBoPhan,
                cd.TenCongDoan AS TenCongDoan,
                COALESCE(cdl.SoNhanSu, 0) AS SoNhanSuBoPhan,

                -- Số lượng nguồn lực per business rule
                CASE
                    WHEN COALESCE(a.AssignedMachineCount,0) > 0 THEN a.AssignedMachineCount
                    WHEN COALESCE(a.AssignedHasLabor,0) = 1 THEN COALESCE(cdl.SoNhanSu, 0)
                    WHEN COALESCE(r.MachineCount,0) > 0 THEN r.MachineCount
                    WHEN COALESCE(r.HasLabor,0) = 1 THEN COALESCE(cdl.SoNhanSu, 0)
                    ELSE 0
                END AS SoLuongNguonLuc,

                MAX(btp.DinhMucThoiGian) AS DinhMucThoiGian,
                -- setup time: SUM of setup times of assigned machines
                CASE
                    WHEN COALESCE(a.AssignedMachineCount,0) > 0 THEN COALESCE(MAX(a.AssignedTotalSetupTime), 0)
                    WHEN COALESCE(r.MachineCount,0) > 0 THEN COALESCE(MAX(r.TotalSetupTime), 0)
                    ELSE 0
                END AS ThoiGianThietLapMay,

                -- production capacity per business rule
                CASE
                    WHEN COALESCE(a.AssignedMachineCount,0) > 0 THEN
                        CAST(
                            (
                                (8.0*60.0*COALESCE(a.AssignedMachineCount,0) - COALESCE(a.AssignedTotalSetupTime,0))
                                / NULLIF(MAX(btp.DinhMucThoiGian), 0)
                            ) AS float
                        )
                    WHEN COALESCE(a.AssignedHasLabor,0) = 1 THEN
                        CAST(
                            (
                                (8.0*60.0*COALESCE(cdl.SoNhanSu,0))
                                / NULLIF(MAX(btp.DinhMucThoiGian), 0)
                            ) AS float
                        )
                    WHEN COALESCE(r.MachineCount,0) > 0 THEN
                        CAST(
                            (
                                (8.0*60.0*COALESCE(r.MachineCount,0) - COALESCE(r.TotalSetupTime,0))
                                / NULLIF(MAX(btp.DinhMucThoiGian), 0)
                            ) AS float
                        )
                    WHEN COALESCE(r.HasLabor,0) = 1 THEN
                        CAST(
                            (
                                (8.0*60.0*COALESCE(cdl.SoNhanSu,0))
                                / NULLIF(MAX(btp.DinhMucThoiGian), 0)
                            ) AS float
                        )
                    ELSE 0
                END AS NangLucSanXuat

            FROM base b
            LEFT JOIN DM_DonHangSX dh ON dh.DonHangID = b.DonHangID
            LEFT JOIN assigned a ON a.KeHoachID=b.KeHoachID AND a.DonHangID=b.DonHangID AND a.LineKey=b.LineKey
                          AND a.TP_DinhMucID=b.TP_DinhMucID AND a.BTP_DinhMucID=b.BTP_DinhMucID AND a.ThuTuSX=b.ThuTuSX
            LEFT JOIN res r ON r.KeHoachID=b.KeHoachID AND r.DonHangID=b.DonHangID AND r.LineKey=b.LineKey
                          AND r.TP_DinhMucID=b.TP_DinhMucID AND r.BTP_DinhMucID=b.BTP_DinhMucID AND r.ThuTuSX=b.ThuTuSX
            LEFT JOIN DM_DinhMucSanPham tp
                 ON tp.DinhMucID = b.TP_DinhMucID
                AND tp.LoaiSanPham = N'TP'
            LEFT JOIN DM_DinhMucSanPham btp
                 ON btp.DinhMucID = b.BTP_DinhMucID
                AND btp.LoaiSanPham = N'BTP'
            LEFT JOIN DM_CongDoan cd        ON cd.MaCongDoan = b.MaCongDoan
            LEFT JOIN DM_CongDoanLon cdl    ON cdl.MaCongDoanLon = b.MaCongDoanLon

            GROUP BY
                b.DonHangID,
                dh.SoChungTu,
                dh.KhachHang,
                b.LineKey,
                b.TP_DinhMucID,
                b.BTP_DinhMucID,
                b.ThuTuSX,
                b.MaCongDoan,
                b.MaCongDoanLon,
                b.TotalSetup,
                b.TotalRun,
                b.TotalQty,
                b.Start,
                b.[End],
                b.DueDT,
                tp.MaSanPham,
                tp.TenSanPham,
                btp.MaSanPham,
                btp.TenSanPham,
                cdl.TenCongDoanLon,
                cd.TenCongDoan,
                a.AssignedMachineCodes,
                a.AssignedMachineText,
                a.AssignedMachineCount,
                a.AssignedHasLabor,
                a.AssignedTotalSetupTime,
                r.MachineCodes,
                r.MachineText,
                r.MachineCount,
                r.HasLabor,
                r.TotalSetupTime,
                cdl.SoNhanSu

            ORDER BY b.TP_DinhMucID, b.DueDT, b.ThuTuSX
            """
        )

        return db.execute(sql, {"id": int(ke_hoach_id)}).mappings().all()

    @staticmethod
    def get_calendar_view(db: Session, ke_hoach_id: int):
        """Return calendar view: Header, Days, Rows.

                Allocation strategy (Option B - spread across whole plan horizon, capped + overtime backfill):
        - Never allocate more than NangLucSanXuat per day.
        - Allowed window: from segment StartDT (clamped to TuNgay) up to plan DenNgay.
        - Prefer finishing by DueDT: allocate sequentially in [Start..min(DueDT,DenNgay)].
                - If still remaining before DueDT, backfill into days <= DueDT as overtime (tăng ca),
                    allowing over-capacity up to NangLucTangCa per day.
                - If remaining after that, allocate sequentially in (DueDT..DenNgay] (late but still capped by NangLucSanXuat).
        - If still remaining after all days up to DenNgay, keep it as UnplannedQty.

        Additional rule:
        - Chủ nhật không sản xuất: nếu ngày phân bổ rơi vào Chủ nhật thì bỏ qua và dời sang ngày kế tiếp.

        Note: We intentionally do NOT limit allocation by scheduler EndDT; EndDT represents
        the initial schedule estimate, but Option B requires leveling load across the plan window.
        """

        header = KeHoachRepo.get_by_id(db, ke_hoach_id)
        if not header:
            return None

        # days from header range
        tu: date = header["TuNgay"]
        den: date = header["DenNgay"]
        days: list[date] = []
        cur = tu
        while cur <= den:
            days.append(cur)
            cur = cur + timedelta(days=1)

        grouped = KeHoachRepo.get_btp_grouped(db, ke_hoach_id)

        def _to_dt(x):
            return x

        def _safe_int(x, default: int = 0) -> int:
            return _safe_int_from_float(x, default)

        def _to_date(x, default: date | None = None) -> date | None:
            if x is None:
                return default
            if isinstance(x, date) and not isinstance(x, datetime):
                return x
            if isinstance(x, datetime):
                return x.date()
            return default

        def _range_idx(start_d: date, end_d: date) -> list[int]:
            if start_d is None or end_d is None:
                return []
            if end_d < start_d:
                return []
            # Chủ nhật (weekday==6): bỏ qua
            return [i for i, dday in enumerate(days) if start_d <= dday <= end_d and dday.weekday() != 6]

        def _next_non_sunday(d0: date) -> date:
            d = d0
            for _ in range(8):
                if d.weekday() != 6:
                    return d
                d = d + timedelta(days=1)
            return d0

        def _chain_key(g: dict) -> tuple[int, str, int]:
            return (
                int(g.get("DonHangID") or 0),
                str(g.get("LineKey") or ""),
                int(g.get("TP_DinhMucID") or 0),
            )

        chains: dict[tuple[int, str, int], list[dict]] = {}
        for g in grouped:
            # SQLAlchemy .mappings().all() yields RowMapping (Mapping), not plain dict.
            if not isinstance(g, Mapping):
                continue
            gd = dict(g)
            chains.setdefault(_chain_key(gd), []).append(gd)

        rows = []
        for _ck, arr in chains.items():
            arr.sort(
                key=lambda x: (
                    _safe_int(x.get("ThuTuSX"), 0),
                    _safe_int(x.get("BTP_DinhMucID"), 0),
                    str(x.get("MaCongDoan") or ""),
                )
            )

            chain_available_day: date = tu
            chain_blocked = False
            blocked_by: dict | None = None
            prev_step_meta: dict | None = None

            for g in arr:
                donhang = g.get("DonHangID")
                linekey = g.get("LineKey")
                tp = g.get("TP_DinhMucID")
                btp = g.get("BTP_DinhMucID")
                thutu = g.get("ThuTuSX")
                macd = g.get("MaCongDoan")
                macdl = g.get("MaCongDoanLon")

                total_setup = g.get("TotalSetup", 0)
                total_run = g.get("TotalRun", 0)
                total_qty = g.get("TotalQty", 0)
                st = _to_dt(g.get("Start"))
                en = _to_dt(g.get("End"))
                duedt = _to_dt(g.get("DueDT"))

                st_d = st.date() if st else tu
                # Option B: do NOT use scheduler EndDT as allocation limit; allow until plan end.
                en_d = den
                due_d = _to_date(duedt, default=den)

                # clamp to header window
                if st_d < tu:
                    st_d = tu
                if en_d > den:
                    en_d = den
                if due_d is not None:
                    if due_d < tu:
                        due_d = tu
                    if due_d > den:
                        due_d = den

                # enforce chain dependency: cannot start before previous BTP finishes
                if chain_available_day and st_d < chain_available_day:
                    st_d = chain_available_day

                blocked_reason = None

                # If dependency pushes start beyond plan window, this step cannot be planned in this plan.
                # Keep window fields valid (start<=end) for UI/AI, but mark as blocked/outside window.
                original_st_d = st_d

                st_d = _next_non_sunday(st_d)
                if due_d is not None:
                    due_d = _next_non_sunday(due_d)

                pushed_outside_plan = bool(original_st_d and original_st_d > den)
                if pushed_outside_plan:
                    # clamp for display / downstream consumers
                    st_d = den
                    if not chain_blocked:
                        chain_blocked = True
                        blocked_by = prev_step_meta
                    # Make the reason explicit (keeps earlier blocked reason if present)
                    if not blocked_reason:
                        blocked_reason = "Ngoài khung kế hoạch: công đoạn/BTP trước kết thúc sau ngày kết thúc kế hoạch"
                    elif "Ngoài khung kế hoạch" not in str(blocked_reason):
                        blocked_reason = str(blocked_reason) + " (ngoài khung kế hoạch)"

                last_plan_day = min(en_d, due_d) if due_d else en_d

                idx_pre_due = _range_idx(st_d, last_plan_day)
                idx_post_due = []
                if due_d and en_d and en_d > last_plan_day:
                    idx_post_due = _range_idx(last_plan_day + timedelta(days=1), en_d)

                if not idx_pre_due and not idx_post_due:
                    for i, dday in enumerate(days):
                        if dday < st_d:
                            continue
                        if dday.weekday() != 6:
                            idx_pre_due = [i]
                            break

                daily = [0] * len(days)

                qty_total = _safe_int(total_qty, 0)
                cap_int = _safe_int(g.get("NangLucSanXuat"), 0)
                has_capacity = cap_int > 0

                overtime_cap_int = _compute_overtime_capacity_per_day(
                    resource_count=_safe_int(g.get("SoLuongNguonLuc"), 0),
                    setup_minutes=float(g.get("ThoiGianThietLapMay") or 0),
                    dinh_muc_thoi_gian_minutes=float(g.get("DinhMucThoiGian") or 0),
                    overtime_hours=4.0,
                )

                overtime_qty = 0
                late_qty = 0
                over_capacity_qty = 0
                unplanned_qty = 0
                is_blocked_by_pred = False

                if chain_blocked:
                    is_blocked_by_pred = True
                    blocked_reason = blocked_reason or "BTP trước chưa hoàn thành hoặc bị dừng"
                    unplanned_qty = int(max(0, qty_total))
                else:
                    remaining = qty_total

                    plan1, remaining = _alloc_sequential_capped(
                        target_qty=remaining,
                        idx_list=idx_pre_due,
                        cap_int=cap_int,
                        has_capacity=bool(has_capacity),
                    )
                    for j, i in enumerate(idx_pre_due):
                        daily[i] = int(plan1[j])

                    if remaining > 0 and has_capacity and overtime_cap_int > 0 and idx_pre_due:
                        remaining, overtime_qty = _alloc_overtime_backfill(
                            remaining_qty=remaining,
                            idx_pre_due=idx_pre_due,
                            daily=daily,
                            overtime_cap_int=overtime_cap_int,
                        )

                    if remaining > 0 and idx_post_due:
                        plan2, remaining2 = _alloc_sequential_capped(
                            target_qty=remaining,
                            idx_list=idx_post_due,
                            cap_int=cap_int,
                            has_capacity=bool(has_capacity),
                        )
                        for j, i in enumerate(idx_post_due):
                            daily[i] = int(plan2[j])
                        late_qty = int(sum(plan2))
                        remaining = remaining2

                    over_capacity_qty = int(overtime_qty)
                    unplanned_qty = int(max(0, remaining))

                    if not has_capacity:
                        over_capacity_qty = 0

                planned_total = int(sum(daily))
                last_prod_idx = None
                try:
                    nz = [i for i, v in enumerate(daily) if int(v or 0) > 0]
                    last_prod_idx = max(nz) if nz else None
                except Exception:
                    last_prod_idx = None

                if last_prod_idx is not None:
                    try:
                        chain_available_day = _next_non_sunday(days[int(last_prod_idx)] + timedelta(days=1))
                    except Exception:
                        chain_available_day = chain_available_day

                # Block successors when this step is disabled (qty=0) or not fully planned
                if not chain_blocked:
                    if qty_total <= 0 or int(unplanned_qty) > 0:
                        chain_blocked = True
                        blocked_by = {
                            "thu_tu_sx": thutu,
                            "btp_dinh_muc_id": btp,
                            "ma_cong_doan": macd,
                        }

                rows.append(
                    {
                        "DonHangID": donhang,
                        "SoChungTu": g.get("SoChungTu"),
                        "SoDonHang": g.get("SoChungTu"),
                        "KhachHang": g.get("KhachHang"),
                        "LineKey": linekey,
                        "TP_DinhMucID": tp,
                        "BTP_DinhMucID": btp,
                        "ThuTuSX": thutu,
                        "MaCongDoan": macd,
                        "MaCongDoanLon": macdl,
                        "MaNguonLuc": g.get("MaNguonLuc"),
                        "TenNguonLuc": g.get("TenNguonLuc"),
                        "MaTP": g.get("MaThanhPham"),
                        "MaBTP": g.get("MaBanThanhPham"),
                        "TotalSetup": total_setup,
                        "TotalRun": total_run,
                        "TotalQty": total_qty,
                        "Start": st,
                        "End": en,
                        "DueDT": duedt,

                        "TenThanhPham": g.get("TenThanhPham"),
                        "TenBanThanhPham": g.get("TenBanThanhPham"),
                        "TenBoPhan": g.get("TenBoPhan"),
                        "TenCongDoan": g.get("TenCongDoan"),
                        "SoNhanSuBoPhan": g.get("SoNhanSuBoPhan", 0),
                        "NguonLucText": g.get("NguonLucText"),
                        "SoLuongNguonLuc": g.get("SoLuongNguonLuc", 0),
                        "DinhMucThoiGian": g.get("DinhMucThoiGian"),
                        "ThoiGianThietLapMay": g.get("ThoiGianThietLapMay"),
                        "NangLucSanXuat": g.get("NangLucSanXuat", 0),
                        "NangLucTangCa": int(overtime_cap_int),

                        "DailyQty": daily,

                        # Dependency diagnostics
                        "IsBlockedByPredecessor": bool(is_blocked_by_pred),
                        "BlockedBy": blocked_by,
                        "BlockedReason": blocked_reason,

                        # Diagnostics for UI warnings
                        "HasCapacity": bool(has_capacity),
                        "CapacityPerDay": cap_int,
                        "OvertimeCapacityPerDay": int(overtime_cap_int),
                        "PlanWindowStart": st_d,
                        "PlanWindowEnd": en_d,
                        "PlannedTotal": planned_total,
                        "IsOverCapacity": bool(over_capacity_qty > 0),
                        "OverCapacityQty": int(over_capacity_qty),
                        "IsOvertime": bool(overtime_qty > 0),
                        "OvertimeQty": int(overtime_qty),
                        "IsLate": bool(late_qty > 0),
                        "LateQty": int(late_qty),
                        "UnplannedQty": int(unplanned_qty),
                    }
                )

                prev_step_meta = {
                    "thu_tu_sx": thutu,
                    "btp_dinh_muc_id": btp,
                    "ma_cong_doan": macd,
                }

        return {
            "Header": header,
            "Days": days,
            "Rows": rows,
        }

    @staticmethod
    def get_late_alerts(db):
        """Late alerts for plans that are already in progress.

        Output shape:
        {
          "today": "YYYY-MM-DD",
          "items": [
            {
              "level": "warning"|"danger",
              "ke_hoach_id": int,
              "ma_ke_hoach": str,
              "tu_ngay": "YYYY-MM-DD",
              "den_ngay": "YYYY-MM-DD",
              "due_date": "YYYY-MM-DD",  # max due date on related order/lines, fallback DenNgay
              "days_late": int,
              "orders": [ {"don_hang_id": int, "so_don_hang": str, "ngay_giao_hang": "YYYY-MM-DD"} ]
            }
          ]
        }
        """

        today = date.today()

        # 1) Pull plan headers (include computed MaKeHoach in code below for consistency)
        plans = KeHoachRepo.get_all(db, skip=0, limit=1000, search=None) or []

        # 2) For each plan, find related orders + their due dates via plan detail table
        #    Use max due date across order header and order lines.
        #    NOTE: Plan detail table is `KeHoachSX_ChiTiet` (no DM_ prefix) in this DB.
        sql = text(
            """
            SELECT
                ct.KeHoachID AS KeHoachID,
                dh.DonHangID AS DonHangID,
                dh.SoDonHang AS SoDonHang,
                MAX(COALESCE(dhct.NgayGiaoHang, dh.NgayGiaoHang)) AS DueDate
            FROM KeHoachSX_ChiTiet ct
            JOIN DM_DonHangSX dh ON dh.DonHangID = ct.DonHangID
            LEFT JOIN DM_DonHangSX_ChiTiet dhct ON dhct.DonHangID = dh.DonHangID
            WHERE ct.KeHoachID IN :ids
            GROUP BY ct.KeHoachID, dh.DonHangID, dh.SoDonHang
            """
        )

        ids = [int(p.get('KeHoachID')) for p in plans if p.get('KeHoachID') is not None]
        if not ids:
            return {"today": today.isoformat(), "items": []}

        # SQL Server doesn't support tuple expansion automatically -> build expanding param list
        params = {}
        ph = []
        for i, pid in enumerate(ids):
            k = f"id{i}"
            params[k] = pid
            ph.append(f":{k}")

        rel_rows = []
        try:
            sql2 = text(sql.text.replace("IN :ids", f"IN ({','.join(ph)})"))
            rel_rows = db.execute(sql2, params).mappings().all()
        except Exception:
            # If order/detail tables are missing in a specific demo DB, fall back to plan DenNgay only.
            rel_rows = []

        by_plan = {}
        for r in rel_rows:
            pid = int(r.get('KeHoachID'))
            by_plan.setdefault(pid, []).append(r)

        items = []

        def _is_in_progress(trang_thai: str) -> bool:
            s = (trang_thai or '').strip().lower()
            # Accept a few variants to be resilient.
            return ("đang" in s) or ("dang" in s) or ("in progress" in s)

        for p in plans:
            pid = int(p.get('KeHoachID'))
            tu = p.get('TuNgay')
            den = p.get('DenNgay')
            status = p.get('TrangThai')

            if not _is_in_progress(status):
                continue

            # compute due date = max order due within plan, fallback plan DenNgay
            rel = by_plan.get(pid, [])
            due_candidates = []
            orders = []
            for r in rel:
                dd = r.get('DueDate')
                if dd is not None:
                    due_candidates.append(dd)
                orders.append({
                    "don_hang_id": int(r.get('DonHangID')),
                    "so_don_hang": r.get('SoDonHang'),
                    "ngay_giao_hang": (dd.isoformat() if hasattr(dd, 'isoformat') else str(dd) if dd else None)
                })

            due = None
            if due_candidates:
                due = max(due_candidates)
            elif den is not None:
                due = den

            if due is None:
                continue

            # Normalize date
            due_date = due if isinstance(due, date) else getattr(due, 'date', lambda: None)()
            if due_date is None:
                try:
                    due_date = date.fromisoformat(str(due)[:10])
                except:
                    continue

            if today <= due_date:
                continue

            days_late = (today - due_date).days
            level = 'danger' if days_late >= 3 else 'warning'

            items.append({
                "level": level,
                "ke_hoach_id": pid,
                "ma_ke_hoach": p.get('MaKeHoach') or str(pid),
                "tu_ngay": (tu.isoformat() if hasattr(tu, 'isoformat') else str(tu) if tu else None),
                "den_ngay": (den.isoformat() if hasattr(den, 'isoformat') else str(den) if den else None),
                "due_date": due_date.isoformat(),
                "days_late": int(days_late),
                "orders": orders
            })

        # Sort: most late first
        items.sort(key=lambda x: x.get('days_late', 0), reverse=True)
        return {"today": today.isoformat(), "items": items}

    @staticmethod
    def get_busy_intervals_other_plans(
        db: Session,
        *,
        horizon_start: datetime,
        horizon_end: datetime,
        exclude_ke_hoach_id: int | None = None,
    ):
        """Lấy các khoảng thời gian bận từ *các kế hoạch khác* trong khoảng horizon.

        Trả về danh sách dict:
          - resource_type: 'machine' | 'dept'
          - resource_id: mã máy (MaNguonLuc) hoặc mã bộ phận (MaCongDoanLon)
          - start_dt, end_dt: datetime
          - ke_hoach_id, ma_ke_hoach, segment_id (best-effort)

        Lưu ý:
        - Chỉ lấy segment có StartDT/EndDT.
        - Loại trừ chính kế hoạch đang tối ưu (exclude_ke_hoach_id).
        """

        sql = text(
            """
            SELECT
                ct.KeHoachID AS KeHoachID,
                kh.MaKeHoach AS MaKeHoach,
                ct.SegmentID AS SegmentID,
                ct.MaNguonLuc AS MaNguonLuc,
                ct.MaCongDoanLon AS MaCongDoanLon,
                ct.StartDT AS StartDT,
                ct.EndDT AS EndDT
            FROM KeHoachSX_ChiTiet ct
            LEFT JOIN KeHoachSX kh ON kh.KeHoachID = ct.KeHoachID
            WHERE
                ct.StartDT IS NOT NULL AND ct.EndDT IS NOT NULL
                AND ct.StartDT < :h_end AND ct.EndDT > :h_start
                AND (:exclude_id IS NULL OR ct.KeHoachID <> :exclude_id)
            """
        )

        rows = db.execute(
            sql,
            {
                "h_start": horizon_start,
                "h_end": horizon_end,
                "exclude_id": int(exclude_ke_hoach_id) if exclude_ke_hoach_id is not None else None,
            },
        ).mappings().all()

        out = []
        for r in rows:
            st = r.get("StartDT")
            en = r.get("EndDT")
            if not st or not en:
                continue

            ke_hoach_id = int(r.get("KeHoachID")) if r.get("KeHoachID") is not None else None
            ma_ke_hoach = r.get("MaKeHoach")

            # machine busy
            if r.get("MaNguonLuc"):
                out.append(
                    {
                        "resource_type": "machine",
                        "resource_id": str(r.get("MaNguonLuc")),
                        "start_dt": st,
                        "end_dt": en,
                        "ke_hoach_id": ke_hoach_id,
                        "ma_ke_hoach": str(ma_ke_hoach) if ma_ke_hoach else None,
                        "segment_id": int(r.get("SegmentID")) if r.get("SegmentID") is not None else None,
                    }
                )

            # dept busy
            if r.get("MaCongDoanLon"):
                out.append(
                    {
                        "resource_type": "dept",
                        "resource_id": str(r.get("MaCongDoanLon")),
                        "start_dt": st,
                        "end_dt": en,
                        "ke_hoach_id": ke_hoach_id,
                        "ma_ke_hoach": str(ma_ke_hoach) if ma_ke_hoach else None,
                        "segment_id": int(r.get("SegmentID")) if r.get("SegmentID") is not None else None,
                    }
                )

        return out
