from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ...core.database import get_db
from ...schemas.ke_hoach_schema import KeHoachSXCreate, KeHoachSXUpdate, PlanFromOrdersRequest
from ...services.ke_hoach_service import KeHoachService
from ...services.plan_from_order_service import create_plan_from_order, create_plan_from_orders

router = APIRouter(prefix="/kehoach", tags=["Kế hoạch sản xuất"])

@router.get("/", summary="Danh sách kế hoạch sản xuất")
def list_kehoach(skip: int = 0, limit: int = 100, search: str = None, db: Session = Depends(get_db)):
    return KeHoachService.get_all(db, skip, limit, search)

@router.get("/{ke_hoach_id}", summary="Chi tiết kế hoạch")
def get_kehoach(ke_hoach_id: int, db: Session = Depends(get_db)):
    kehoach = KeHoachService.get_by_id(db, ke_hoach_id)
    if not kehoach:
        raise HTTPException(status_code=404, detail="Không tìm thấy kế hoạch")
    return kehoach

@router.post("/", summary="Tạo kế hoạch sản xuất")
def create_kehoach(obj_in: KeHoachSXCreate, db: Session = Depends(get_db)):
    return KeHoachService.create(db, obj_in)

@router.put("/{ke_hoach_id}", summary="Cập nhật kế hoạch sản xuất")
def update_kehoach(ke_hoach_id: int, obj_in: KeHoachSXUpdate, db: Session = Depends(get_db)):
    return KeHoachService.update(db, ke_hoach_id, obj_in)

@router.delete("/{ke_hoach_id}", summary="Xóa kế hoạch sản xuất")
def delete_kehoach(ke_hoach_id: int, db: Session = Depends(get_db)):
    return KeHoachService.delete(db, ke_hoach_id)

@router.get("/{ke_hoach_id}/chitiet", summary="Chi tiết các segment của kế hoạch")
def get_chitiet(ke_hoach_id: int, db: Session = Depends(get_db)):
    return KeHoachService.get_chitiet_by_kehoach(db, ke_hoach_id)

@router.get("/{ke_hoach_id}/btp", summary="BTP cần sản xuất (grouped)")
def get_btp_grouped(ke_hoach_id: int, db: Session = Depends(get_db)):
    return KeHoachService.get_btp_grouped(db, ke_hoach_id)

@router.post("/plan-from-order/{don_hang_id}", summary="Lập kế hoạch từ 1 đơn hàng (tự động phân rã TP→BTP và tạo segments)")
def plan_from_order(don_hang_id: int, db: Session = Depends(get_db)):
    return create_plan_from_order(db, don_hang_id)


@router.post(
    "/plan-from-orders",
    summary="Lập kế hoạch từ nhiều đơn hàng (tạo 1 kế hoạch chung, tự động phân rã TP→BTP và tạo segments)",
)
def plan_from_orders(req: PlanFromOrdersRequest, db: Session = Depends(get_db)):
    return create_plan_from_orders(db, req.don_hang_ids)

@router.get("/{ke_hoach_id}/calendar", summary="Dữ liệu kế hoạch dạng lịch theo ngày (kế thừa từ đơn hàng)")
def get_calendar_view(ke_hoach_id: int, db: Session = Depends(get_db)):
    data = KeHoachService.get_calendar_view(db, ke_hoach_id)
    if not data:
        raise HTTPException(status_code=404, detail="Không tìm thấy kế hoạch")
    return data

@router.get("/alerts/late", summary="Cảnh báo: đơn/kế hoạch đang sản xuất nhưng bị trễ")
def get_late_alerts(db: Session = Depends(get_db)):
    """Return alerts for plans/orders that are likely late.

    Rule (pragmatic, DB-agnostic):
    - Plan is considered 'in progress' when TrangThai indicates it has started.
    - A plan/order is considered 'late' when today > DenNgay (plan due) OR today > order due date.

    The repo maps TrangThai via compute_plan_status(TuNgay, DenNgay), so this endpoint
    relies on plan dates and (if available) related order due dates.
    """
    return KeHoachService.get_late_alerts(db)
