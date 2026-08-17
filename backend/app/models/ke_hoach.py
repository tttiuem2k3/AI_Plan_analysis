from __future__ import annotations

from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Integer, Unicode

from ..core.database import Base


class KeHoachSX(Base):
    __tablename__ = "KeHoachSX"

    KeHoachID = Column(BigInteger, primary_key=True, autoincrement=True)

    # Persisted plan code (format: KHSX/MM/YYYY/NNN)
    MaKeHoach = Column(Unicode(50), nullable=False)

    NgayLap = Column(DateTime, nullable=False)
    TuNgay = Column(Date, nullable=False)
    DenNgay = Column(Date, nullable=False)
    TrangThai = Column(Unicode(50), nullable=True)


class KeHoachSX_ChiTiet(Base):
    __tablename__ = "KeHoachSX_ChiTiet"

    KeHoachID = Column(BigInteger, ForeignKey("KeHoachSX.KeHoachID"), primary_key=True)
    SegmentID = Column(BigInteger, primary_key=True)

    SegmentType = Column(Unicode(10))  # SETUP/RUN/GAP
    DonHangID = Column(BigInteger)
    LineKey = Column(Unicode(50))
    TP_DinhMucID = Column(BigInteger)
    BTP_DinhMucID = Column(BigInteger)

    ThuTuSX = Column(Integer)
    MaCongDoan = Column(Unicode(50))
    MaCongDoanLon = Column(Unicode(50))
    MaNguonLuc = Column(Unicode(50))

    SoLuongSX = Column(Integer)
    LaborUsed = Column(Integer)

    SetupMinutes = Column(Integer)
    RunMinutes = Column(Integer)

    StartDT = Column(DateTime)
    EndDT = Column(DateTime)
    DueDT = Column(DateTime)

    Note = Column(Unicode(255))
