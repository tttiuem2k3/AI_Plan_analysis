from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import datetime, date


class KeHoachSXCreate(BaseModel):
    # Client only needs to provide the plan window; server will generate NgayLap/MaKeHoach.
    TuNgay: date
    DenNgay: date
    TrangThai: Optional[str] = None
    NgayLap: Optional[datetime] = None


class KeHoachSXUpdate(BaseModel):
    TuNgay: Optional[date] = None
    DenNgay: Optional[date] = None
    TrangThai: Optional[str] = None


class KeHoachSXInDB(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    KeHoachID: int
    MaKeHoach: str
    NgayLap: datetime
    TuNgay: date
    DenNgay: date
    TrangThai: Optional[str] = None



class KeHoachSX_ChiTietBase(BaseModel):
    KeHoachID: int
    SegmentID: int
    SegmentType: Optional[str]
    DonHangID: Optional[int]
    LineKey: Optional[str]
    TP_DinhMucID: Optional[int]
    BTP_DinhMucID: Optional[int]
    ThuTuSX: Optional[int]
    MaCongDoan: Optional[str]
    MaCongDoanLon: Optional[str]
    MaNguonLuc: Optional[str]
    SoLuongSX: Optional[int]
    LaborUsed: Optional[int]
    SetupMinutes: Optional[int]
    RunMinutes: Optional[int]
    StartDT: Optional[datetime]
    EndDT: Optional[datetime]
    DueDT: Optional[datetime]
    Note: Optional[str]


class KeHoachSX_ChiTietInDB(KeHoachSX_ChiTietBase):
    model_config = ConfigDict(from_attributes=True)


class KeHoachSXWithChiTiet(KeHoachSXInDB):
    chitiet: List[KeHoachSX_ChiTietInDB] = []


class PlanFromOrdersRequest(BaseModel):
    don_hang_ids: List[int]
