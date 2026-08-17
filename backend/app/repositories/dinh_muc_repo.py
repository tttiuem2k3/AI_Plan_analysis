# backend/app/repositories/dinh_muc_repo.py
from sqlalchemy.orm import Session
from sqlalchemy import select, delete, func, update
from ..models.dinh_muc import DinhMucSanPham, TP_BTP
from ..models.nguon_luc import DM_CongDoan as CongDoanModel

def get_tree_tp_btp(db: Session):
    tps = db.execute(
        select(DinhMucSanPham)
        .where(DinhMucSanPham.LoaiSanPham == "TP")
        .order_by(DinhMucSanPham.MaSanPham)
    ).scalars().all()

    # join link + btp + congdoan name
    rows = db.execute(
        select(
            TP_BTP.TP_DinhMucID,
            TP_BTP.ThuTuSX,
            TP_BTP.CongDoan,
            DinhMucSanPham,
            CongDoanModel.TenCongDoan
        )
        .join(DinhMucSanPham, DinhMucSanPham.DinhMucID == TP_BTP.BTP_DinhMucID)
        .outerjoin(CongDoanModel, CongDoanModel.MaCongDoan == DinhMucSanPham.MaCongDoan)
        .order_by(TP_BTP.TP_DinhMucID, TP_BTP.ThuTuSX, DinhMucSanPham.MaSanPham)
    ).all()

    children_map = {}
    for tp_id, thutusx_tp, congdoan_tp, btp, ten_cd in rows:
        children_map.setdefault(tp_id, []).append((thutusx_tp, congdoan_tp, btp, ten_cd))

    return tps, children_map

def get_by_id(db: Session, product_id: int):
    return db.get(DinhMucSanPham, product_id)

def get_by_ma(db: Session, ma: str):
    return db.execute(select(DinhMucSanPham).where(DinhMucSanPham.MaSanPham == ma)).scalar_one_or_none()

def create_product(db: Session, obj: DinhMucSanPham):
    db.add(obj)
    db.flush()
    return obj

def update_product_fields(db: Session, obj: DinhMucSanPham, payload: dict):
    for k, v in payload.items():
        setattr(obj, k, v)
    db.flush()
    return obj

def delete_product(db: Session, product_id: int):
    db.execute(delete(DinhMucSanPham).where(DinhMucSanPham.DinhMucID == product_id))

def unlink_all_of_tp(db: Session, tp_id: int):
    db.execute(delete(TP_BTP).where(TP_BTP.TP_DinhMucID == tp_id))

def unlink_tp_btp(db: Session, tp_id: int, btp_id: int):
    db.execute(delete(TP_BTP).where(TP_BTP.TP_DinhMucID == tp_id, TP_BTP.BTP_DinhMucID == btp_id))

def link_tp_btp(db: Session, tp_id: int, btp_id: int, thu_tu_sx: int, cong_doan_tp: str | None):
    existed = db.execute(
        select(TP_BTP).where(TP_BTP.TP_DinhMucID == tp_id, TP_BTP.BTP_DinhMucID == btp_id)
    ).scalar_one_or_none()
    if existed:
        return

    db.add(TP_BTP(TP_DinhMucID=tp_id, BTP_DinhMucID=btp_id, ThuTuSX=thu_tu_sx, CongDoan=cong_doan_tp))
    db.flush()

def get_max_thutu_tp(db: Session, tp_id: int) -> int:
    mx = db.execute(
        select(func.max(TP_BTP.ThuTuSX)).where(TP_BTP.TP_DinhMucID == tp_id)
    ).scalar_one_or_none()
    return int(mx or 0)

def get_links_of_tp(db: Session, tp_id: int):
    return db.execute(
        select(TP_BTP).where(TP_BTP.TP_DinhMucID == tp_id).order_by(TP_BTP.ThuTuSX, TP_BTP.BTP_DinhMucID)
    ).scalars().all()

def update_link_meta(db: Session, tp_id: int, btp_id: int, thutu: int | None, congdoan_tp: str | None):
    db.execute(
        update(TP_BTP)
        .where(TP_BTP.TP_DinhMucID == tp_id, TP_BTP.BTP_DinhMucID == btp_id)
        .values(ThuTuSX=thutu, CongDoan=congdoan_tp)
    )
    db.flush()

def list_btp(db: Session, keyword: str | None):
    q = select(DinhMucSanPham).where(DinhMucSanPham.LoaiSanPham == "BTP")
    if keyword:
        kw = f"%{keyword}%"
        q = q.where((DinhMucSanPham.MaSanPham.like(kw)) | (DinhMucSanPham.TenSanPham.like(kw)))
    return db.execute(q.order_by(DinhMucSanPham.MaSanPham)).scalars().all()

def count_btp_used(db: Session, btp_id: int) -> int:
    return len(db.execute(select(TP_BTP).where(TP_BTP.BTP_DinhMucID == btp_id)).scalars().all())
