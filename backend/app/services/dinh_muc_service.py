# backend/app/services/dinh_muc_service.py
from sqlalchemy.orm import Session
from fastapi import HTTPException
from ..models.dinh_muc import DinhMucSanPham
from ..repositories import dinh_muc_repo

def to_out(p: DinhMucSanPham):
    return {
        "dinh_muc_id": p.DinhMucID,
        "loai_san_pham": p.LoaiSanPham,
        "ma_san_pham": p.MaSanPham,
        "ten_san_pham": p.TenSanPham,
        "dinh_luong": float(p.DinhLuong) if p.DinhLuong is not None else None,
        "dinh_muc_thoi_gian": float(p.DinhMucThoiGian) if p.DinhMucThoiGian is not None else None,
        "thu_tu_sx": p.ThuTuSX,
        "cong_doan": p.MaCongDoan,
    }

def resequence_tp(db: Session, tp_id: int):
    """Đảm bảo thứ tự BTP trong 1 TP luôn từ 1..n"""
    links = dinh_muc_repo.get_links_of_tp(db, tp_id)
    i = 1
    for lk in links:
        dinh_muc_repo.update_link_meta(db, tp_id, lk.BTP_DinhMucID, i, lk.CongDoan)
        i += 1

def get_tree(db: Session):
    tps, children_map = dinh_muc_repo.get_tree_tp_btp(db)
    result = []

    for tp in tps:
        node = to_out(tp)
        node["children"] = []

        # children_map item: (ThuTuSX_TP, CongDoan_TP, btp_obj, TenCongDoan(master))
        for thutu_tp, congdoan_tp, btp, ten_cd in children_map.get(tp.DinhMucID, []):
            child = to_out(btp)
            child["thu_tu_sx_tp"] = thutu_tp
            child["cong_doan_tp"] = congdoan_tp
            # tiện hiển thị tên công đoạn (nếu có)
            child["cong_doan_ten"] = ten_cd
            node["children"].append(child)

        result.append(node)

    return result

def create_product(db: Session, payload):
    if dinh_muc_repo.get_by_ma(db, payload.ma_san_pham):
        raise HTTPException(status_code=409, detail="Mã sản phẩm đã tồn tại")

    obj = DinhMucSanPham(
        LoaiSanPham=payload.loai_san_pham,
        MaSanPham=payload.ma_san_pham,
        TenSanPham=payload.ten_san_pham,
        DinhLuong=payload.dinh_luong,
        DinhMucThoiGian=payload.dinh_muc_thoi_gian,
        ThuTuSX=payload.thu_tu_sx,
        MaCongDoan=payload.cong_doan,
    )
    dinh_muc_repo.create_product(db, obj)
    db.commit()
    return to_out(obj)

def update_product(db: Session, product_id: int, payload):
    obj = dinh_muc_repo.get_by_id(db, product_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Không tìm thấy sản phẩm")

    # Không cho đổi mã khi cập nhật (TP/BTP)
    # Payload update không còn ma_san_pham; nếu client cố gửi thì schema cũng sẽ chặn.

    dinh_muc_repo.update_product_fields(db, obj, {
        "LoaiSanPham": payload.loai_san_pham,
        "TenSanPham": payload.ten_san_pham,
        "DinhLuong": payload.dinh_luong,
        "DinhMucThoiGian": payload.dinh_muc_thoi_gian,
        "ThuTuSX": payload.thu_tu_sx,
        "MaCongDoan": payload.cong_doan,
    })
    db.commit()
    return to_out(obj)

def delete_product(db: Session, product_id: int):
    obj = dinh_muc_repo.get_by_id(db, product_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Không tìm thấy sản phẩm")

    if obj.LoaiSanPham == "TP":
        dinh_muc_repo.unlink_all_of_tp(db, obj.DinhMucID)
        dinh_muc_repo.delete_product(db, obj.DinhMucID)
        db.commit()
        return {"ok": True}

    # BTP: nếu đang được dùng, chặn
    used = dinh_muc_repo.count_btp_used(db, obj.DinhMucID)
    if used > 0:
        raise HTTPException(status_code=409, detail="BTP đang được dùng trong TP khác. Hãy gỡ liên kết trước.")
    dinh_muc_repo.delete_product(db, obj.DinhMucID)
    db.commit()
    return {"ok": True}

def unlink_btp(db: Session, tp_id: int, btp_id: int):
    dinh_muc_repo.unlink_tp_btp(db, tp_id, btp_id)
    resequence_tp(db, tp_id)
    db.commit()
    return {"ok": True}

def link_existing_btp(db: Session, tp_id: int, btp_id: int, cong_doan_tp: str | None):
    tp = dinh_muc_repo.get_by_id(db, tp_id)
    btp = dinh_muc_repo.get_by_id(db, btp_id)
    if not tp or not btp:
        raise HTTPException(status_code=404, detail="Không tìm thấy TP/BTP")
    if tp.LoaiSanPham != "TP" or btp.LoaiSanPham != "BTP":
        raise HTTPException(status_code=409, detail="Quan hệ phải là TP -> BTP")

    next_no = dinh_muc_repo.get_max_thutu_tp(db, tp_id) + 1
    dinh_muc_repo.link_tp_btp(db, tp_id, btp_id, next_no, cong_doan_tp)

    resequence_tp(db, tp_id)
    db.commit()
    return {"ok": True}

def create_and_link_btp(db: Session, tp_id: int, payload):
    tp = dinh_muc_repo.get_by_id(db, tp_id)
    if not tp or tp.LoaiSanPham != "TP":
        raise HTTPException(status_code=404, detail="Không tìm thấy TP")

    if dinh_muc_repo.get_by_ma(db, payload.ma_san_pham):
        raise HTTPException(status_code=409, detail="Mã BTP đã tồn tại (hãy chọn BTP có sẵn)")

    btp = DinhMucSanPham(
        LoaiSanPham="BTP",
        MaSanPham=payload.ma_san_pham,
        TenSanPham=payload.ten_san_pham,
        DinhLuong=payload.dinh_luong,
        DinhMucThoiGian=payload.dinh_muc_thoi_gian,
        MaCongDoan=payload.cong_doan
    )
    dinh_muc_repo.create_product(db, btp)

    next_no = dinh_muc_repo.get_max_thutu_tp(db, tp_id) + 1
    dinh_muc_repo.link_tp_btp(db, tp_id, btp.DinhMucID, next_no, None)

    resequence_tp(db, tp_id)
    db.commit()
    return {"ok": True, "btp": to_out(btp)}

def update_link_meta(db: Session, tp_id: int, btp_id: int, thutu: int | None, cong_doan_tp: str | None):
    dinh_muc_repo.update_link_meta(db, tp_id, btp_id, thutu, cong_doan_tp)
    resequence_tp(db, tp_id)
    db.commit()
    return {"ok": True}

def list_btp(db: Session, keyword: str | None):
    rows = dinh_muc_repo.list_btp(db, keyword)
    return [to_out(x) for x in rows]