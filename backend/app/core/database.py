# backend/app/core/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings
from urllib.parse import quote_plus
from fastapi import HTTPException


def _normalize_mssql_server(server: str) -> str:
    raw = str(server or "").strip()
    if not raw:
        return "tcp:127.0.0.1,1433"

    low = raw.lower()
    if low.startswith("tcp:"):
        return raw

    # Named instance / explicit protocol / endpoint format: keep as-is
    if "\\" in raw or "," in raw or raw.startswith("np:"):
        return raw

    if low in {"localhost", ".", "(local)"}:
        return "tcp:127.0.0.1,1433"

    return f"tcp:{raw},1433"

def build_mssql_url() -> str:
    # Ưu tiên dùng ODBC connection string đầy đủ (ổn định, ít lỗi nhất)
    encrypt = "yes" if str(settings.MSSQL_ENCRYPT).lower() in ("yes", "true", "1") else "no"
    trust = "yes" if str(settings.MSSQL_TRUST_CERT).lower() in ("yes", "true", "1") else "no"
    server = _normalize_mssql_server(settings.MSSQL_SERVER)
    use_trusted = str(settings.MSSQL_TRUSTED).lower() in ("yes", "true", "1")
    has_sql_login = bool(str(getattr(settings, "MSSQL_USER", "") or "").strip()) and bool(
        str(getattr(settings, "MSSQL_PASSWORD", "") or "").strip()
    )

    # ✅ Windows Authentication
    if use_trusted and not has_sql_login:
        odbc_str = (
            f"DRIVER={{{settings.MSSQL_DRIVER}}};"
            f"SERVER={server};"
            f"DATABASE={settings.MSSQL_DB};"
            f"Trusted_Connection=yes;"
            f"Encrypt={encrypt};"
            f"TrustServerCertificate={trust};"
            f"Connection Timeout=5;"
        )
        return "mssql+pyodbc:///?odbc_connect=" + quote_plus(odbc_str)

    # ✅ SQL Login (nếu sau này cần)
    odbc_str = (
        f"DRIVER={{{settings.MSSQL_DRIVER}}};"
        f"SERVER={server};"
        f"DATABASE={settings.MSSQL_DB};"
        f"UID={settings.MSSQL_USER};"
        f"PWD={settings.MSSQL_PASSWORD};"
        f"Encrypt={encrypt};"
        f"TrustServerCertificate={trust};"
        f"Connection Timeout=5;"
    )
    return "mssql+pyodbc:///?odbc_connect=" + quote_plus(odbc_str)

_enable_sql = str(getattr(settings, "ENABLE_SQL", "yes")).lower() in ("yes", "true", "1")
engine = create_engine(build_mssql_url(), pool_pre_ping=True) if _enable_sql else None
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None

class Base(DeclarativeBase):
    pass

def get_db():
    if not _enable_sql or SessionLocal is None:
        raise HTTPException(status_code=503, detail="SQL is disabled (ENABLE_SQL=no)")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_optional_db():
    if not _enable_sql or SessionLocal is None:
        yield None
        return

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
