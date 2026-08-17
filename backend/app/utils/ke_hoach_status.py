from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional


def compute_plan_status(tu_ngay: Optional[date], den_ngay: Optional[date], now: Optional[datetime] = None) -> str:
    """Compute plan status from planned date range.

    Rules:
    - Default/status before start: 'Chưa sản xuất'
    - From start day (inclusive) until end day (inclusive): 'Đang sản xuất'
    - After end day: 'Đã sản xuất'

    Notes:
    - Uses local naive datetime (same convention as current app).
    """
    if now is None:
        now = datetime.now()

    if not tu_ngay and not den_ngay:
        return "Chưa sản xuất"

    if tu_ngay and not den_ngay:
        # treat as single-day plan
        den_ngay = tu_ngay
    if den_ngay and not tu_ngay:
        tu_ngay = den_ngay

    start_dt = datetime.combine(tu_ngay, time.min)  # 00:00:00
    end_dt = datetime.combine(den_ngay, time.max)   # 23:59:59.999999

    if now < start_dt:
        return "Chưa sản xuất"
    if now > end_dt:
        return "Đã sản xuất"
    return "Đang sản xuất"
