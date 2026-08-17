from sqlalchemy import Column, BigInteger, Unicode, Date, Integer, ForeignKey
from sqlalchemy.orm import relationship
from ..core.database import Base


class DonHangSX(Base):
    __tablename__ = "DM_DonHangSX"

    DonHangID = Column(BigInteger, primary_key=True, index=True, autoincrement=True)

    # Quản lý phân cấp: Loại Đơn Hàng -> Sản phẩm
    LoaiDonHang = Column(Unicode(50), nullable=True, index=True)

    # Tình trạng đơn hàng
    TinhTrangDonHang = Column(Unicode(50), nullable=True, index=True)

    SoChungTu = Column(Unicode(50), nullable=False, unique=True, index=True)
    KhachHang = Column(Unicode(255), nullable=True)
    NgayGiaoHang = Column(Date, nullable=True)

    # children
    chi_tiets = relationship(
        "DonHangSXChiTiet",
        back_populates="don_hang",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class DonHangSXChiTiet(Base):
    __tablename__ = "DM_DonHangSXChiTiet"

    DonHangID = Column(BigInteger, ForeignKey("DM_DonHangSX.DonHangID", ondelete="CASCADE"), primary_key=True)
    DinhMucID = Column(BigInteger, ForeignKey("DM_DinhMucSanPham.DinhMucID"), primary_key=True)
    SoLuongDatHang = Column(Integer, nullable=True)
    NgayGiaoHang = Column(Date, nullable=True)

    don_hang = relationship("DonHangSX", back_populates="chi_tiets")