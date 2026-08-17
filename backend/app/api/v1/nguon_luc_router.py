from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from backend.app.core.database import get_db
from backend.app.schemas.nguon_luc_schema import (
    CongDoanLonSchema,
    CongDoanSchema,
    NguonLucSchema,
    CongDoanNguonLucSchema,
    NguonLucTreeSchema,
    CongDoanCongDoanLonSchema,
)
from backend.app.services.nguon_luc_service import (
    CongDoanLonService,
    CongDoanService,
    NguonLucService,
    CongDoanNguonLucService,
    CongDoanCongDoanLonService,
)

router = APIRouter(prefix="/nguon-luc", tags=["NguonLuc"])

@router.get("/congdoanlon", response_model=list[CongDoanLonSchema])
def get_all_congdoanlon(db: Session = Depends(get_db)):
    return CongDoanLonService.get_all(db)

@router.post("/congdoanlon", response_model=CongDoanLonSchema)
def create_congdoanlon(data: CongDoanLonSchema, db: Session = Depends(get_db)):
    try:
        return CongDoanLonService.create(db, data)
    except ValueError as e:
        if str(e) == "DUPLICATE_KEY":
            raise HTTPException(status_code=409, detail="Trùng khóa chính (MaCongDoanLon).")
        raise

@router.put("/congdoanlon/{ma}", response_model=CongDoanLonSchema)
def update_congdoanlon(ma: str, data: CongDoanLonSchema, db: Session = Depends(get_db)):
    return CongDoanLonService.update(db, ma, data)

@router.delete("/congdoanlon/{ma}")
def delete_congdoanlon(ma: str, db: Session = Depends(get_db)):
    try:
        return CongDoanLonService.delete(db, ma)
    except ValueError as e:
        if str(e) == "FK_CONSTRAINT":
            raise HTTPException(status_code=409, detail="Không thể xóa bộ phận vì vẫn còn công đoạn đang gắn trong bộ phận. Hãy gỡ công đoạn trước.")
        raise

@router.get("/congdoan", response_model=list[CongDoanSchema])
def get_all_congdoan(db: Session = Depends(get_db)):
    return CongDoanService.get_all(db)

@router.post("/congdoan", response_model=CongDoanSchema)
def create_congdoan(data: CongDoanSchema, db: Session = Depends(get_db)):
    try:
        return CongDoanService.create(db, data)
    except ValueError as e:
        if str(e) == "DUPLICATE_KEY":
            raise HTTPException(status_code=409, detail="Trùng khóa chính (MaCongDoan).")
        raise

@router.put("/congdoan/{ma}", response_model=CongDoanSchema)
def update_congdoan(ma: str, data: CongDoanSchema, db: Session = Depends(get_db)):
    return CongDoanService.update(db, ma, data)

@router.delete("/congdoan/{ma}")
def delete_congdoan(ma: str, db: Session = Depends(get_db)):
    try:
        return CongDoanService.delete(db, ma)
    except ValueError as e:
        if str(e) == "FK_CONSTRAINT":
            raise HTTPException(status_code=409, detail="Không thể xóa công đoạn, hãy gỡ các nguồn lực phụ thuộc.")
        raise

# Gán công đoạn vào bộ phận (công đoạn lớn)
@router.post("/congdoan/attach", response_model=CongDoanCongDoanLonSchema)
def attach_congdoan(data: CongDoanCongDoanLonSchema, db: Session = Depends(get_db)):
    try:
        return CongDoanCongDoanLonService.attach(db, data)
    except ValueError as e:
        if str(e) == "DUPLICATE_KEY":
            raise HTTPException(status_code=409, detail="Công đoạn đã thuộc bộ phận khác.")
        raise

# Bỏ gán công đoạn khỏi bộ phận
@router.delete("/congdoan/detach/{ma_cong_doan}")
def detach_congdoan(ma_cong_doan: str, db: Session = Depends(get_db)):
    obj = CongDoanCongDoanLonService.detach(db, ma_cong_doan)
    if not obj:
        raise HTTPException(status_code=404, detail="Công đoạn chưa được gán bộ phận.")
    return {"message": "Đã bỏ gán công đoạn khỏi bộ phận."}

@router.get("/cong-doan/list", response_model=list[CongDoanSchema])
def get_cong_doan_list(db: Session = Depends(get_db)):
    return CongDoanService.get_all(db)

@router.get("/nguonluc", response_model=list[NguonLucSchema])
def get_all_nguonluc(db: Session = Depends(get_db)):
    return NguonLucService.get_all(db)

@router.get("/nguonluc/{ma}", response_model=NguonLucSchema)
def get_one_nguonluc(ma: str, db: Session = Depends(get_db)):
    obj = NguonLucService.get_one(db, ma)
    if not obj:
        raise HTTPException(status_code=404, detail="Không tìm thấy nguồn lực.")
    return obj

@router.post("/nguonluc", response_model=NguonLucSchema)
def create_nguonluc(data: NguonLucSchema, db: Session = Depends(get_db)):
    try:
        return NguonLucService.create(db, data)
    except ValueError as e:
        if str(e) == "DUPLICATE_KEY":
            raise HTTPException(status_code=409, detail="Trùng khóa chính (MaNguonLuc).")
        raise

@router.put("/nguonluc/{ma}", response_model=NguonLucSchema)
def update_nguonluc(ma: str, data: NguonLucSchema, db: Session = Depends(get_db)):
    return NguonLucService.update(db, ma, data)

@router.delete("/nguonluc/{ma}")
def delete_nguonluc(ma: str, db: Session = Depends(get_db)):
    try:
        return NguonLucService.delete(db, ma)
    except ValueError as e:
        if str(e) == "FK_CONSTRAINT":
            raise HTTPException(status_code=409, detail="Nguồn lực đang được dùng trong công đoạn. Hãy gỡ liên kết trước.")
        raise

@router.get("/mapping", response_model=list[CongDoanNguonLucSchema])
def get_all_mapping(db: Session = Depends(get_db)):
    return CongDoanNguonLucService.get_all(db)

@router.post("/mapping", response_model=CongDoanNguonLucSchema)
def create_mapping(data: CongDoanNguonLucSchema, db: Session = Depends(get_db)):
    # Mapping dùng để gắn nguồn lực vào công đoạn (many-to-many).
    # Không tạo mới nguồn lực ở đây; FE có thể gọi /nguonluc trước nếu cần.
    try:
        return CongDoanNguonLucService.create(db, data)
    except ValueError as e:
        if str(e) == "DUPLICATE_KEY":
            raise HTTPException(status_code=409, detail="Nguồn lực đã được gắn vào công đoạn này.")
        raise

@router.delete("/mapping/{ma_cong_doan}/{ma_nguon_luc}")
def delete_mapping(ma_cong_doan: str, ma_nguon_luc: str, db: Session = Depends(get_db)):
    deleted = CongDoanNguonLucService.delete(db, ma_cong_doan, ma_nguon_luc)
    if not deleted:
        raise HTTPException(status_code=404, detail="Liên kết công đoạn - nguồn lực không tồn tại.")
    return {"message": "Đã gỡ nguồn lực khỏi công đoạn."}

# API lấy dữ liệu dạng cây cho frontend
@router.get("/tree", response_model=list[NguonLucTreeSchema])
def get_tree(search: str = '', db: Session = Depends(get_db)):
    # Trả về dữ liệu phân cấp: Công đoạn lớn -> Công đoạn -> Nguồn lực
    cd_lons = CongDoanLonService.get_all(db) or []
    cd_list = CongDoanService.get_all(db) or []
    nl_list = NguonLucService.get_all(db) or []
    mapping_cd_nl = CongDoanNguonLucService.get_all(db) or []
    mapping_cd_cdl = CongDoanCongDoanLonService.get_all(db) or []

    nl_dict = {nl.MaNguonLuc: nl for nl in nl_list}

    # Index mapping: bộ phận -> [công đoạn]
    cd_by_cdl = {}
    for m in mapping_cd_cdl:
        cd_by_cdl.setdefault(m.MaCongDoanLon, []).append(m.MaCongDoan)

    # Index mapping: công đoạn -> [nguồn lực]
    nl_by_cd = {}
    for m in mapping_cd_nl:
        nl_by_cd.setdefault(m.MaCongDoan, []).append(m.MaNguonLuc)

    cd_dict = {cd.MaCongDoan: cd for cd in cd_list}

    tree = []
    for cd_lon in cd_lons:
        cd_lon_node = {
            'loai': 'Công đoạn lớn',
            'ma': cd_lon.MaCongDoanLon,
            'ten': cd_lon.TenCongDoanLon,
            'nhan_su': cd_lon.SoNhanSu,
            'children': []
        }

        for ma_cd in cd_by_cdl.get(cd_lon.MaCongDoanLon, []):
            cd = cd_dict.get(ma_cd)
            if not cd:
                continue

            cd_node = {
                'loai': 'Công đoạn',
                'ma': cd.MaCongDoan,
                'ten': cd.TenCongDoan,
                'children': []
            }

            for ma_nl in nl_by_cd.get(cd.MaCongDoan, []):
                nl = nl_dict.get(ma_nl)
                if not nl:
                    continue
                cd_node['children'].append({
                    'loai': 'Nguồn lực',
                    'ma': nl.MaNguonLuc,
                    'ten': nl.TenNguonLuc,
                    'nhan_su_phu_thuoc': nl.NhanSuPhanBo,
                    'thoi_gian_thiet_lap': nl.ThoiGianThietLap
                })

            cd_lon_node['children'].append(cd_node)

        tree.append(cd_lon_node)

    # Nếu có search thì lọc theo tên/mã (giữ logic như cũ)
    if search:
        s = search.lower()

        def match(item):
            return s in (item.get('ma', '').lower()) or s in (item.get('ten', '').lower())

        tree = [cdl for cdl in tree if match(cdl) or any(match(cd) for cd in cdl.get('children', []))]

    return tree
