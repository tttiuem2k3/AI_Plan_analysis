from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.schemas.nguon_luc_schema import CongDoanSchema
from backend.app.services.nguon_luc_service import CongDoanService

# Router riêng để FE gọi /api/v1/cong-doan/list
router = APIRouter(prefix="/cong-doan", tags=["CongDoan"])


@router.get("/list", response_model=list[CongDoanSchema])
def list_cong_doan(db: Session = Depends(get_db)):
    return CongDoanService.get_all(db)
