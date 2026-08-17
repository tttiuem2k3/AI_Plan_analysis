from __future__ import annotations

from datetime import date
from pydantic import BaseModel, Field
from typing import List, Optional


class DonHangChiTietUpsert(BaseModel):
    dinh_muc_id: int
    so_luong_dat_hang: Optional[int] = None
    ngay_giao_hang: Optional[date] = None


class DonHangChiTietOut(BaseModel):
    dinh_muc_id: int
    ma_san_pham: Optional[str] = None
    ten_san_pham: Optional[str] = None
    so_luong_dat_hang: Optional[int] = None
    ngay_giao_hang: Optional[date] = None


class DonHangCreate(BaseModel):
    loai_don_hang: Optional[str] = Field(default=None, max_length=50)
    so_chung_tu: str = Field(..., max_length=50)
    khach_hang: Optional[str] = Field(default=None, max_length=255)
    ngay_giao_hang: Optional[date] = None
    chi_tiets: List[DonHangChiTietUpsert] = Field(default_factory=list)


class DonHangUpdate(BaseModel):
    loai_don_hang: Optional[str] = Field(default=None, max_length=50)
    khach_hang: Optional[str] = Field(default=None, max_length=255)
    ngay_giao_hang: Optional[date] = None
    chi_tiets: List[DonHangChiTietUpsert] = Field(default_factory=list)


class DonHangOut(BaseModel):
    don_hang_id: int
    loai_don_hang: Optional[str] = None
    tinh_trang_don_hang: Optional[str] = None
    so_chung_tu: str
    khach_hang: Optional[str] = None
    ngay_giao_hang: Optional[date] = None
    chi_tiets: List[DonHangChiTietOut] = Field(default_factory=list)


# Tree: Loại đơn hàng -> các đơn -> sản phẩm
class DonHangNode(BaseModel):
    key: str
    kind: str  # LOAI | DON_HANG | SAN_PHAM
    label: str

    # meta
    don_hang_id: Optional[int] = None
    loai_don_hang: Optional[str] = None
    tinh_trang_don_hang: Optional[str] = None

    so_chung_tu: Optional[str] = None
    khach_hang: Optional[str] = None
    ngay_giao_hang: Optional[date] = None

    dinh_muc_id: Optional[int] = None
    ma_san_pham: Optional[str] = None
    ten_san_pham: Optional[str] = None
    so_luong_dat_hang: Optional[int] = None
    ngay_giao_hang: Optional[date] = None

    children: List[DonHangNode] = Field(default_factory=list)
