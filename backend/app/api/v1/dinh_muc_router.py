# backend/app/api/v1/dinh_muc_router.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from ...core.database import get_db
from ...schemas.dinh_muc_schema import (
    TPNode, ProductCreate, ProductUpdate, ProductOut,
    LinkExistingBTP, CreateAndLinkBTP, UpdateLinkMeta
)
from ...services import dinh_muc_service
from typing import List

router = APIRouter(prefix="/dinh-muc", tags=["DinhMuc"])

@router.get("/tree", response_model=List[TPNode])
def get_tree(db: Session = Depends(get_db)):
    return dinh_muc_service.get_tree(db)

@router.post("/product", response_model=ProductOut)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)):
    return dinh_muc_service.create_product(db, payload)

@router.put("/product/{product_id}", response_model=ProductOut)
def update_product(product_id: int, payload: ProductUpdate, db: Session = Depends(get_db)):
    return dinh_muc_service.update_product(db, product_id, payload)

@router.delete("/product/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    return dinh_muc_service.delete_product(db, product_id)

@router.delete("/tp/{tp_id}/btp/{btp_id}")
def unlink_btp(tp_id: int, btp_id: int, db: Session = Depends(get_db)):
    return dinh_muc_service.unlink_btp(db, tp_id, btp_id)

@router.post("/tp/{tp_id}/btp/link")
def link_existing(tp_id: int, payload: LinkExistingBTP, db: Session = Depends(get_db)):
    return dinh_muc_service.link_existing_btp(db, tp_id, payload.btp_id, payload.cong_doan_tp)

@router.post("/tp/{tp_id}/btp/create")
def create_and_link(tp_id: int, payload: CreateAndLinkBTP, db: Session = Depends(get_db)):
    return dinh_muc_service.create_and_link_btp(db, tp_id, payload)

@router.patch("/tp/{tp_id}/btp/{btp_id}")
def patch_link_meta(tp_id: int, btp_id: int, payload: UpdateLinkMeta, db: Session = Depends(get_db)):
    return dinh_muc_service.update_link_meta(db, tp_id, btp_id, payload.thu_tu_sx_tp, payload.cong_doan_tp)

@router.get("/btp/list", response_model=List[ProductOut])
def list_btp(q: str | None = Query(None), db: Session = Depends(get_db)):
    return dinh_muc_service.list_btp(db, q)
