# backend/app/api/v1/health.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
from ...core.database import get_db

router = APIRouter(prefix="/health", tags=["Health"])

@router.get("/db")
def health_db(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True}
