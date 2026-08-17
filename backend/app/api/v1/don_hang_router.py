from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List

from ...core.database import get_db
from ...schemas.don_hang_schema import DonHangNode, DonHangCreate, DonHangUpdate, DonHangOut
from ...services import don_hang_service

router = APIRouter(prefix="/don-hang", tags=["DonHang"])


@router.get("/tree", response_model=List[DonHangNode])
def get_tree(db: Session = Depends(get_db)):
    return don_hang_service.get_tree(db)


@router.post("/order", response_model=DonHangOut)
def create_order(payload: DonHangCreate, db: Session = Depends(get_db)):
    return don_hang_service.create_order(db, payload)


@router.put("/order/{don_hang_id}", response_model=DonHangOut)
def update_order(don_hang_id: int, payload: DonHangUpdate, db: Session = Depends(get_db)):
    return don_hang_service.update_order(db, don_hang_id, payload)


@router.delete("/order/{don_hang_id}")
def delete_order(don_hang_id: int, db: Session = Depends(get_db)):
    return don_hang_service.delete_order(db, don_hang_id)


@router.get("/product/list")
def list_products(q: str | None = Query(None), db: Session = Depends(get_db)):
    return don_hang_service.list_products(db, q)


@router.get("/order-for-plan", response_model=List[DonHangOut])
def list_orders_for_plan(db: Session = Depends(get_db)):
    # Chỉ lấy các đơn hàng có TinhTrangDonHang == "Chưa lập kế hoạch"
    return don_hang_service.list_orders_for_plan(db)


@router.get("/chua-lap-ke-hoach", response_model=List[DonHangOut])
def get_orders_chua_lap_ke_hoach(db: Session = Depends(get_db)):
    return don_hang_service.get_orders_chua_lap_ke_hoach(db)
