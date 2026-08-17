# backend/app/schemas/dinh_muc_schema.py
from pydantic import BaseModel
from typing import List, Optional, Literal

Loai = Literal["TP", "BTP"]

class ProductBase(BaseModel):
    loai_san_pham: Loai
    ma_san_pham: str
    ten_san_pham: str
    dinh_luong: Optional[float] = None
    dinh_muc_thoi_gian: Optional[float] = None
    # master fields (theo yêu cầu)
    thu_tu_sx: Optional[int] = None
    cong_doan: Optional[str] = None   # MaCongDoan

class ProductCreate(ProductBase):
    pass

class ProductUpdate(BaseModel):
    loai_san_pham: Loai
    ten_san_pham: str
    dinh_luong: Optional[float] = None
    dinh_muc_thoi_gian: Optional[float] = None
    thu_tu_sx: Optional[int] = None
    cong_doan: Optional[str] = None

class ProductOut(ProductBase):
    dinh_muc_id: int

# BTP hiển thị trong 1 TP (có thứ tự theo TP)
class BTPInTP(ProductOut):
    thu_tu_sx_tp: Optional[int] = None   # thứ tự theo TP (từ DM_TP_BTP)
    cong_doan_tp: Optional[str] = None   # nếu override theo TP

class TPNode(ProductOut):
    children: List[BTPInTP] = []

class LinkExistingBTP(BaseModel):
    btp_id: int
    cong_doan_tp: Optional[str] = None

class CreateAndLinkBTP(BaseModel):
    ma_san_pham: str
    ten_san_pham: str
    dinh_luong: Optional[float] = None
    dinh_muc_thoi_gian: Optional[float] = None
    cong_doan: Optional[str] = None      # MaCongDoan (master)

class UpdateLinkMeta(BaseModel):
    # cập nhật thứ tự và/hoặc công đoạn theo TP
    thu_tu_sx_tp: Optional[int] = None
    cong_doan_tp: Optional[str] = None
