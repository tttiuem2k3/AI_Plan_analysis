from sqlalchemy import Column, String, Integer, Float, ForeignKey, Unicode
from backend.app.core.database import Base

class DM_CongDoanLon(Base):
    __tablename__ = "DM_CongDoanLon"
    MaCongDoanLon = Column(String, primary_key=True, index=True)
    
    TenCongDoanLon = Column(Unicode(255), nullable=False, unique=True)
    SoNhanSu = Column(Integer)

class DM_NguonLuc(Base):
    __tablename__ = "DM_NguonLuc"
    MaNguonLuc = Column(String, primary_key=True, index=True)
    TenNguonLuc = Column(Unicode(255), nullable=False, unique=True)
    NhanSuPhanBo = Column(Integer)
    ThoiGianThietLap = Column(Float)

class DM_CongDoan(Base):
    __tablename__ = "DM_CongDoan"
    MaCongDoan = Column(String, primary_key=True, index=True)
    TenCongDoan = Column(Unicode(255), nullable=False, unique=True)
    # MaCongDoanLon đã được loại bỏ khỏi bảng DM_CongDoan (chuyển sang bảng mapping DM_CongDoan_CongDoanLon)

class DM_CongDoan_CongDoanLon(Base):
    __tablename__ = "DM_CongDoan_CongDoanLon"
    # PK là MaCongDoan để đảm bảo 1 công đoạn chỉ thuộc tối đa 1 bộ phận
    MaCongDoan = Column(String, ForeignKey('DM_CongDoan.MaCongDoan'), primary_key=True)
    MaCongDoanLon = Column(String, ForeignKey('DM_CongDoanLon.MaCongDoanLon'), nullable=False, index=True)

class DM_CongDoan_Nguon_Luc(Base):
    __tablename__ = "DM_CongDoan_Nguon_Luc"
    MaCongDoan = Column(String, ForeignKey('DM_CongDoan.MaCongDoan'), primary_key=True)
    MaNguonLuc = Column(String, ForeignKey('DM_NguonLuc.MaNguonLuc'), primary_key=True)
