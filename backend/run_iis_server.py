from __future__ import annotations

import copy
import os
from pathlib import Path

import uvicorn
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG


def _build_log_config(log_file: Path) -> dict:
    cfg = copy.deepcopy(UVICORN_LOGGING_CONFIG)

    cfg.setdefault("formatters", {})
    cfg["formatters"]["server"] = {
        "format": "%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s",
        "datefmt": "%Y-%m-%d %H:%M:%S",
    }

    cfg.setdefault("handlers", {})
    cfg["handlers"]["server_file"] = {
        "class": "logging.handlers.TimedRotatingFileHandler",
        "formatter": "server",
        "filename": str(log_file),
        "when": "midnight",
        "interval": 1,
        "backupCount": 14,
        "encoding": "utf-8",
    }

    cfg.setdefault("loggers", {})
    cfg["loggers"]["uvicorn"] = {
        "handlers": ["server_file"],
        "level": "INFO",
        "propagate": False,
    }
    cfg["loggers"]["uvicorn.error"] = {
        "handlers": ["server_file"],
        "level": "INFO",
        "propagate": False,
    }
    cfg["loggers"]["uvicorn.access"] = {
        "handlers": ["server_file"],
        "level": "INFO",
        "propagate": False,
    }
    return cfg


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    log_dir = project_root / "backend" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "Server.log"

    port = int(os.environ.get("HTTP_PLATFORM_PORT", "8000"))

    uvicorn.run(
        "backend.app.main:app",
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=True,
        use_colors=False,
        log_config=_build_log_config(log_file),
    )


if __name__ == "__main__":
    main()
