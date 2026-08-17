from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List

from .dtos import Operation, Plan
from .cp_sat_scheduler import cp_sat_reschedule


def local_reschedule(plan: Plan, affected_scope: Dict[str, Any]) -> Plan:
    """Local rebuild using CP-SAT when possible.

    Notes:
    - This is best-effort: if OR-Tools isn't installed or no feasible solution, we return the plan unchanged.
    - To get good results you should provide Segment.start/end in your PlanBuilder adapter.
    """

    import os

    try:
        tl = float(os.getenv("AI_CP_SAT_TIME_LIMIT_S", "2.0") or "2.0")
    except Exception:
        tl = 2.0
    tl = max(0.5, min(10.0, tl))

    patched, result = cp_sat_reschedule(plan, affected_scope, time_limit_s=tl)
    if not result.ok:
        return plan
    return patched
