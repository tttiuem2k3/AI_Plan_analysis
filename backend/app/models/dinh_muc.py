# ==========================================
# backend/app/models/dinh_muc.py
# ==========================================

from sqlalchemy import Column, BigInteger, String, DECIMAL, Integer, ForeignKey, Unicode
from ..core.database import Base

class DinhMucSanPham(Base):
    __tablename__ = "DM_DinhMucSanPham"

    DinhMucID = Column(BigInteger, primary_key=True, index=True)
    LoaiSanPham = Column(Unicode(10), nullable=False)           # TP | BTP
    MaSanPham = Column(Unicode(50), nullable=False, unique=True)
    TenSanPham = Column(Unicode(255), nullable=False)
    DinhLuong = Column(DECIMAL(18, 5), nullable=True)
    DinhMucThoiGian = Column(DECIMAL(18, 2), nullable=True)

    # theo yêu cầu mới (master-level)
    ThuTuSX = Column(Integer, nullable=True)                   # không dùng cho TP-BTP numbering (để NULL)
    MaCongDoan = Column(Unicode(50), ForeignKey('DM_CongDoan.MaCongDoan'), nullable=True)  # lưu MaCongDoan

class TP_BTP(Base):
    __tablename__ = "DM_TP_BTP"

    TP_DinhMucID = Column(BigInteger, ForeignKey("DM_DinhMucSanPham.DinhMucID"), primary_key=True)
    BTP_DinhMucID = Column(BigInteger, ForeignKey("DM_DinhMucSanPham.DinhMucID"), primary_key=True)

    # đánh số theo từng TP (đúng nghiệp vụ)
    ThuTuSX = Column(Integer, nullable=True)
    CongDoan = Column(Unicode(50), nullable=True)  # optional (nếu muốn override theo TP)
