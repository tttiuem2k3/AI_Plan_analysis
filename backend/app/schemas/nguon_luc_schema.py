from pydantic import BaseModel
from typing import Optional

class CongDoanLonSchema(BaseModel):
    MaCongDoanLon: str
    TenCongDoanLon: str
    SoNhanSu: Optional[int]

class CongDoanSchema(BaseModel):
    MaCongDoan: str
    TenCongDoan: str

class CongDoanCongDoanLonSchema(BaseModel):
    MaCongDoan: str
    MaCongDoanLon: str

class NguonLucSchema(BaseModel):
    MaNguonLuc: str
    TenNguonLuc: str
    NhanSuPhanBo: Optional[int]
    ThoiGianThietLap: Optional[float]

class CongDoanNguonLucSchema(BaseModel):
    MaCongDoan: str
    MaNguonLuc: str

# Tree response
class NguonLucTreeSchema(BaseModel):
    loai: str
    ma: str
    ten: str
    nhan_su: Optional[int] = None
    nhan_su_phu_thuoc: Optional[int] = None
    thoi_gian_thiet_lap: Optional[float] = None
    children: Optional[list] = None
