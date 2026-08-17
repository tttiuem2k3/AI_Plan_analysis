from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


_SEPARATOR = "----------------------------------------------"
_LOG_LOCK = threading.Lock()


def _resolve_logs_dir() -> Path:
    try:
        # .../backend/app/utils/ai_flow_log.py -> backend
        backend_root = Path(__file__).resolve().parents[2]
        logs_dir = backend_root / "logs"
    except Exception:
        logs_dir = Path.cwd() / "backend" / "logs"

    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def _to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(content)


def _append_ai_flow_log_unlocked(file_name: str, content: Any, *, title: str | None = None) -> None:
    """Append log content into backend/logs/<file_name> with separator.

    The file is append-only and each record is split by a fixed separator line.
    """

    path = _resolve_logs_dir() / str(file_name)
    ts = datetime.now().isoformat()
    label = str(title or "entry")

    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{_SEPARATOR}\n")
        f.write(f"[{ts}] {label}\n")
        f.write(_to_text(content))
        f.write("\n")


def append_ai_flow_log(file_name: str, content: Any, *, title: str | None = None) -> None:
    try:
        with _LOG_LOCK:
            _append_ai_flow_log_unlocked(file_name, content, title=title)
    except Exception:
        # Logging must never break application flow.
        pass


def _next_log_number(file_name: str) -> int:
    path = _resolve_logs_dir() / str(file_name)
    if not path.exists():
        return 1

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 1

    numbers = [
        int(match.group(1))
        for match in re.finditer(r"^\[[^\]]+\]\s+.+\s+\#(\d+)\b", text, flags=re.MULTILINE)
    ]
    if numbers:
        return max(numbers) + 1

    old_records = text.count(_SEPARATOR)
    return old_records + 1


def append_paired_ai_flow_logs(
    *,
    input_file_name: str,
    input_content: Any,
    output_file_name: str,
    output_content: Any,
    input_title: str = "api_input",
    output_title: str = "api_output",
) -> int:
    """Append matching input/output log records with the same sequence number."""

    try:
        with _LOG_LOCK:
            log_no = _next_log_number(input_file_name)
            _append_ai_flow_log_unlocked(
                input_file_name,
                input_content,
                title=f"{input_title} #{log_no}",
            )
            _append_ai_flow_log_unlocked(
                output_file_name,
                output_content,
                title=f"{output_title} #{log_no}",
            )
            return log_no
    except Exception:
        # Logging must never break application flow.
        return 0


def append_numbered_ai_flow_log(
    file_name: str,
    content: Any,
    *,
    title: str,
    log_no: int | None = None,
) -> int:
    """Append one log record and return the sequence number used."""

    try:
        with _LOG_LOCK:
            final_log_no = int(log_no) if log_no is not None and int(log_no) > 0 else _next_log_number(file_name)
            _append_ai_flow_log_unlocked(
                file_name,
                content,
                title=f"{title} #{final_log_no}",
            )
            return final_log_no
    except Exception:
        # Logging must never break application flow.
        return 0
