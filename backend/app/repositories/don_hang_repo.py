from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import select, delete

from ..models.don_hang import DonHangSX, DonHangSXChiTiet
from ..models.dinh_muc import DinhMucSanPham


def list_orders_with_details(db: Session):
    # eager load via relationship (selectin) already configured
    return db.execute(select(DonHangSX).order_by(DonHangSX.LoaiDonHang, DonHangSX.SoChungTu)).scalars().all()


def get_order(db: Session, don_hang_id: int):
    return db.get(DonHangSX, don_hang_id)


def get_order_by_sochungtu(db: Session, so_chung_tu: str):
    return db.execute(select(DonHangSX).where(DonHangSX.SoChungTu == so_chung_tu)).scalar_one_or_none()


def create_order(db: Session, obj: DonHangSX):
    db.add(obj)
    db.flush()
    return obj


def update_order_fields(db: Session, obj: DonHangSX, payload: dict):
    for k, v in payload.items():
        setattr(obj, k, v)
    db.flush()
    return obj


def delete_order(db: Session, don_hang_id: int):
    db.execute(delete(DonHangSX).where(DonHangSX.DonHangID == don_hang_id))


def replace_details(db: Session, don_hang_id: int, details: list[dict]):
    # simplest: delete all then insert again
    db.execute(delete(DonHangSXChiTiet).where(DonHangSXChiTiet.DonHangID == don_hang_id))
    for d in details:
        db.add(
            DonHangSXChiTiet(
                DonHangID=don_hang_id,
                DinhMucID=int(d["dinh_muc_id"]),
                SoLuongDatHang=d.get("so_luong_dat_hang"),
                NgayGiaoHang=d.get("ngay_giao_hang"),
            )
        )
    db.flush()


def map_product_info(db: Session, dinh_muc_ids: list[int]):
    if not dinh_muc_ids:
        return {}
    rows = db.execute(
        select(DinhMucSanPham.DinhMucID, DinhMucSanPham.MaSanPham, DinhMucSanPham.TenSanPham)
        .where(DinhMucSanPham.DinhMucID.in_(dinh_muc_ids))
    ).all()
    return {int(i): {"ma": ma, "ten": ten} for i, ma, ten in rows}


def list_products(db: Session, keyword: str | None = None):
    q = select(DinhMucSanPham).where(DinhMucSanPham.LoaiSanPham == "TP")
    if keyword:
        kw = f"%{keyword}%"
        q = q.where((DinhMucSanPham.MaSanPham.like(kw)) | (DinhMucSanPham.TenSanPham.like(kw)))
    return db.execute(q.order_by(DinhMucSanPham.MaSanPham)).scalars().all()


class DonHangRepo:
    @staticmethod
    def get_orders_chua_lap_ke_hoach(db: Session):
        return db.query(DonHangSX).filter(DonHangSX.TinhTrangDonHang == "Chưa lập kế hoạch").all()
