from pathlib import Path
import os
import sys

import uvicorn


# =========================
# PROJECT ROOT
# =========================
PROJECT_ROOT = Path(__file__).resolve().parent

# Đảm bảo Python luôn import được package "backend"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Đảm bảo working directory luôn là thư mục gốc project
os.chdir(PROJECT_ROOT)


def main():
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8000"))

    print("=" * 60)
    print("AI GENEPA SERVER")
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Server       : http://{host}:{port}")
    print(f"Swagger      : http://{host}:{port}/docs")
    print("=" * 60)

    uvicorn.run(
        "backend.app.main:app",
        host=host,
        port=port,
        reload=True,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()