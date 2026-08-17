from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models.don_hang import DonHangSX
from ..repositories import don_hang_repo
from ..repositories.don_hang_repo import DonHangRepo

class DonHangService:
    @staticmethod
    def get_orders_chua_lap_ke_hoach(db: Session):
        return DonHangRepo.get_orders_chua_lap_ke_hoach(db)


# Backward-compatible: router gọi trực tiếp hàm module-level
def get_orders_chua_lap_ke_hoach(db: Session):
    orders = DonHangRepo.get_orders_chua_lap_ke_hoach(db)
    return [_to_order_out(db, o) for o in (orders or [])]

# Optional alias (một số nơi đặt tên này)
def list_orders_for_plan(db: Session):
    return get_orders_chua_lap_ke_hoach(db)


def _to_detail_out(db: Session, d):
    return {
        "dinh_muc_id": int(d.DinhMucID),
        "so_luong_dat_hang": d.SoLuongDatHang,
    }


def _to_order_out(db: Session, o: DonHangSX):
    # enrich product info
    dinh_muc_ids = [int(x.DinhMucID) for x in (o.chi_tiets or [])]
    mp = don_hang_repo.map_product_info(db, dinh_muc_ids)

    chi_tiets = []
    for x in (o.chi_tiets or []):
        info = mp.get(int(x.DinhMucID), {})
        chi_tiets.append({
            "dinh_muc_id": int(x.DinhMucID),
            "ma_san_pham": info.get("ma"),
            "ten_san_pham": info.get("ten"),
            "so_luong_dat_hang": x.SoLuongDatHang,
            "ngay_giao_hang": x.NgayGiaoHang or o.NgayGiaoHang,
        })

    return {
        "don_hang_id": int(o.DonHangID),
        "loai_don_hang": o.LoaiDonHang,
        "tinh_trang_don_hang": o.TinhTrangDonHang,
        "so_chung_tu": o.SoChungTu,
        "khach_hang": o.KhachHang,
        "ngay_giao_hang": o.NgayGiaoHang,
        "chi_tiets": chi_tiets,
    }


def get_tree(db: Session):
    orders = don_hang_repo.list_orders_with_details(db)

    # group by loai
    groups: dict[str, list[DonHangSX]] = {}
    for o in orders:
        key = (o.LoaiDonHang or "(Chưa phân loại)").strip() or "(Chưa phân loại)"
        groups.setdefault(key, []).append(o)

    result = []
    for loai, items in groups.items():
        loai_node = {
            "key": f"LOAI::{loai}",
            "kind": "LOAI",
            "label": loai,
            "loai_don_hang": loai,
            "children": [],
        }

        for o in items:
            order_node = {
                "key": f"DONHANG::{int(o.DonHangID)}",
                "kind": "DON_HANG",
                "label": o.SoChungTu,
                "don_hang_id": int(o.DonHangID),
                "loai_don_hang": o.LoaiDonHang,
                "tinh_trang_don_hang": o.TinhTrangDonHang,
                "so_chung_tu": o.SoChungTu,
                "khach_hang": o.KhachHang,
                "ngay_giao_hang": o.NgayGiaoHang,
                "children": [],
            }

            out = _to_order_out(db, o)
            for ct in out["chi_tiets"]:
                order_node["children"].append({
                    "key": f"SANPHAM::{int(o.DonHangID)}::{int(ct['dinh_muc_id'])}",
                    "kind": "SAN_PHAM",
                    "label": f"{ct.get('ma_san_pham') or ''} - {ct.get('ten_san_pham') or ''}".strip(" -"),
                    "don_hang_id": int(o.DonHangID),
                    "dinh_muc_id": int(ct["dinh_muc_id"]),
                    "ma_san_pham": ct.get("ma_san_pham"),
                    "ten_san_pham": ct.get("ten_san_pham"),
                    "so_luong_dat_hang": ct.get("so_luong_dat_hang"),
                    "ngay_giao_hang": ct.get("ngay_giao_hang"),
                    "children": [],
                })

            loai_node["children"].append(order_node)

        result.append(loai_node)

    # stable order by loai
    result.sort(key=lambda x: (x.get("label") or "").lower())
    return result


def create_order(db: Session, payload):
    if don_hang_repo.get_order_by_sochungtu(db, payload.so_chung_tu):
        raise HTTPException(status_code=409, detail="Số chứng từ đã tồn tại")

    obj = DonHangSX(
        LoaiDonHang=payload.loai_don_hang,
        TinhTrangDonHang="Chưa lập kế hoạch",
        SoChungTu=payload.so_chung_tu,
        KhachHang=payload.khach_hang,
        NgayGiaoHang=payload.ngay_giao_hang,
    )
    don_hang_repo.create_order(db, obj)

    # validate products exist
    ids = [int(x.dinh_muc_id) for x in (payload.chi_tiets or [])]
    mp = don_hang_repo.map_product_info(db, ids)
    missing = [i for i in ids if i not in mp]
    if missing:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy sản phẩm (DinhMucID): {missing}")

    don_hang_repo.replace_details(db, int(obj.DonHangID), [
        {
            "dinh_muc_id": int(x.dinh_muc_id),
            "so_luong_dat_hang": x.so_luong_dat_hang,
            "ngay_giao_hang": x.ngay_giao_hang or payload.ngay_giao_hang,
        }
        for x in (payload.chi_tiets or [])
    ])

    db.commit()
    db.refresh(obj)
    return _to_order_out(db, obj)


def update_order(db: Session, don_hang_id: int, payload):
    obj = don_hang_repo.get_order(db, don_hang_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn hàng")

    don_hang_repo.update_order_fields(db, obj, {
        "LoaiDonHang": payload.loai_don_hang,
        "KhachHang": payload.khach_hang,
        "NgayGiaoHang": payload.ngay_giao_hang,
        # Không cho cập nhật TinhTrangDonHang tại màn đơn hàng
    })

    ids = [int(x.dinh_muc_id) for x in (payload.chi_tiets or [])]
    mp = don_hang_repo.map_product_info(db, ids)
    missing = [i for i in ids if i not in mp]
    if missing:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy sản phẩm (DinhMucID): {missing}")

    don_hang_repo.replace_details(db, int(obj.DonHangID), [
        {
            "dinh_muc_id": int(x.dinh_muc_id),
            "so_luong_dat_hang": x.so_luong_dat_hang,
            "ngay_giao_hang": x.ngay_giao_hang or payload.ngay_giao_hang,
        }
        for x in (payload.chi_tiets or [])
    ])

    db.commit()
    db.refresh(obj)
    return _to_order_out(db, obj)


def delete_order(db: Session, don_hang_id: int):
    obj = don_hang_repo.get_order(db, don_hang_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn hàng")

    don_hang_repo.delete_order(db, don_hang_id)
    db.commit()
    return {"ok": True}


def list_products(db: Session, q: str | None):
    rows = don_hang_repo.list_products(db, q)
    return [
        {
            "dinh_muc_id": int(x.DinhMucID),
            "ma_san_pham": x.MaSanPham,
            "ten_san_pham": x.TenSanPham,
        }
        for x in rows
    ]
