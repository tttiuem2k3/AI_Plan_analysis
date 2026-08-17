# backend/app/main.py
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import time
import logging
from typing import Optional

from .api.v1.health import router as health_router
from .api.v1.dinh_muc_router import router as dinh_muc_router
from .api.v1.nguon_luc_router import router as nguon_luc_router
from .api.v1.cong_doan_router import router as cong_doan_router
from .api.v1.don_hang_router import router as don_hang_router
from .api.v1.ke_hoach_router import router as ke_hoach_router
from .api.v1.ai_plan_analysis_router import router as ai_plan_analysis_router
from .api.v1.plan_ai_router import router as plan_ai_router


import warnings
from sqlalchemy.exc import SAWarning
warnings.filterwarnings('ignore', category=SAWarning)
from fastapi.responses import FileResponse

# --- Load .env early so os.getenv() works (OpenAI keys, etc.) ---
try:
    from dotenv import load_dotenv  # type: ignore

    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(dotenv_path=str(PROJECT_ROOT / ".env"), override=False)
except Exception:
    # If python-dotenv isn't installed, Settings(BaseSettings) may still load env for DB.
    # We keep best-effort behavior to avoid startup crash.
    pass

app = FastAPI(title="AI Production Planning API", version="1.0.0")

# Compress JSON/text responses to reduce ngrok bandwidth usage
app.add_middleware(GZipMiddleware, minimum_size=800)

@app.get("/favicon.ico")
def favicon():
    # Trả về rỗng, tránh lỗi 404 nếu không có file favicon.ico
    return Response(content=b"", media_type="image/x-icon")

@app.middleware("http")
async def force_utf8(request: Request, call_next):
    start = time.perf_counter()
    resp: Optional[Response] = None
    try:
        resp = await call_next(request)
    finally:
        dur_ms = (time.perf_counter() - start) * 1000.0
        status = getattr(resp, "status_code", "EXC")

        if dur_ms >= 800:
            # dùng uvicorn.error để chắc chắn hiện trong console uvicorn
            logging.getLogger("uvicorn.error").warning(
                "SLOW %s %s -> %s (%.0fms)",
                request.method,
                request.url.path,
                status,
                dur_ms,
            )

    # resp None gần như không xảy ra; giữ fallback tối thiểu
    if resp is None:
        return Response(status_code=500, content=b"Internal Server Error", media_type="text/plain")

    # Ép charset UTF-8 cho mọi response text/json
    ct = resp.headers.get("content-type", "")
    if "charset" not in ct:
        resp.headers["content-type"] = f"{ct}; charset=utf-8" if ct else "application/json; charset=utf-8"

    # Basic caching for static content when served through this backend
    path = request.url.path or ""
    if path.startswith("/assets/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=3600")
    elif path.startswith("/pages/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=300")

    # expose server timing for debugging (seen in browser devtools)
    resp.headers.setdefault("Server-Timing", f"app;dur={dur_ms:.0f}")

    return resp

# CORS để dev (an toàn nhất vẫn là serve cùng host như bên dưới)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# API
# =========================

app.include_router(health_router, prefix="/api/v1")
app.include_router(dinh_muc_router, prefix="/api/v1")
app.include_router(nguon_luc_router, prefix="/api/v1")
app.include_router(cong_doan_router, prefix="/api/v1")
app.include_router(don_hang_router, prefix="/api/v1")
app.include_router(ke_hoach_router, prefix="/api/v1")
app.include_router(ai_plan_analysis_router, prefix="/api/v1")
app.include_router(plan_ai_router, prefix="/api/v1")
# =========================
# Serve frontend (chạy 1 server là đủ)
# =========================
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # .../DEMO
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# mount assets/pages để index.html và iframe load được
app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")
app.mount("/pages", StaticFiles(directory=str(FRONTEND_DIR / "pages")), name="pages")

@app.get("/")
def home():
    # Trả về trang index của frontend
    return FileResponse(str(FRONTEND_DIR / "index.html"))

# GET /api/v1/health/db

# GET /api/v1/dinh-muc/tree

# POST /api/v1/dinh-muc/product

# PUT /api/v1/dinh-muc/product/{product_id}

# DELETE /api/v1/dinh-muc/product/{product_id}

# DELETE /api/v1/dinh-muc/tp/{tp_id}/btp/{btp_id}

# POST /api/v1/dinh-muc/tp/{tp_id}/btp/link

# POST /api/v1/dinh-muc/tp/{tp_id}/btp/create

# PATCH /api/v1/dinh-muc/tp/{tp_id}/btp/{btp_id}

# GET /api/v1/dinh-muc/btp/list

# GET /api/v1/nguon-luc/congdoanlon

# POST /api/v1/nguon-luc/congdoanlon

# PUT /api/v1/nguon-luc/congdoanlon/{ma}

# DELETE /api/v1/nguon-luc/congdoanlon/{ma}

# GET /api/v1/nguon-luc/congdoan

# POST /api/v1/nguon-luc/congdoan

# PUT /api/v1/nguon-luc/congdoan/{ma}

# DELETE /api/v1/nguon-luc/congdoan/{ma}

# POST /api/v1/nguon-luc/congdoan/attach

# DELETE /api/v1/nguon-luc/congdoan/detach/{ma_cong_doan}

# GET /api/v1/nguon-luc/cong-doan/list

# GET /api/v1/nguon-luc/nguonluc

# GET /api/v1/nguon-luc/nguonluc/{ma}

# POST /api/v1/nguon-luc/nguonluc

# PUT /api/v1/nguon-luc/nguonluc/{ma}

# DELETE /api/v1/nguon-luc/nguonluc/{ma}

# GET /api/v1/nguon-luc/mapping
# POST /api/v1/nguon-luc/mapping
# DELETE /api/v1/nguon-luc/mapping/{ma_cong_doan}/{ma_nguon_luc}
# GET /api/v1/nguon-luc/tree
# GET /api/v1/cong-doan/list
# GET /api/v1/don-hang/tree
# POST /api/v1/don-hang/order
# PUT /api/v1/don-hang/order/{don_hang_id}
# DELETE /api/v1/don-hang/order/{don_hang_id}
# GET /api/v1/don-hang/product/list
# GET /api/v1/don-hang/order-for-plan
# GET /api/v1/don-hang/chua-lap-ke-hoach
# GET /api/v1/kehoach/
# GET /api/v1/kehoach/{ke_hoach_id}
# POST /api/v1/kehoach/
# PUT /api/v1/kehoach/{ke_hoach_id}
# DELETE /api/v1/kehoach/{ke_hoach_id}
# GET /api/v1/kehoach/{ke_hoach_id}/chitiet
# GET /api/v1/kehoach/{ke_hoach_id}/btp
# POST /api/v1/kehoach/plan-from-order/{don_hang_id}
# POST /api/v1/kehoach/plan-from-orders
# GET /api/v1/kehoach/{ke_hoach_id}/calendar
# GET /api/v1/kehoach/alerts/late
# POST /api/v1/kehoach/ai-analysis-payload-batch
# POST /api/v1/kehoach/model-forecast-payload
# POST /api/v1/kehoach/{ke_hoach_id}/ai-apply
# GET /api/v1/kehoach/{ke_hoach_id}/hr-forecast
# GET /api/v1/kehoach/{ke_hoach_id}/machine-forecast
# GET /api/v1/kehoach/{ke_hoach_id}/model-forecast
# POST /api/v1/plans/{plan_id}/ai-optimize        


# => POST /api/v1/kehoach/{ke_hoach_id}/ai-analysis
# => POST /api/v1/kehoach/ai-analysis-payload
'''
Run: uvicorn backend.app.main:app --host 127.0.0.1 --port 4123 --reload

Ngrok: ngrok http http://192.168.0.137:4123

'''
