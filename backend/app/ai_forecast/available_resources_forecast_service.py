from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class AvailableResourcesForecastResult:
    days: List[str]
    available_resources_pct: List[float | None]
    meta: Dict[str, Any]


class AvailableResourcesForecastService:
    @staticmethod
    def _to_day_key(v: Any) -> str:
        return str(v or "")[:10]

    @classmethod
    def forecast_from_hr_and_machine(
        cls,
        *,
        hr_days: List[Any],
        hr_avg_actual_staff_pct: List[Any],
        machine_days: List[Any],
        machine_avg_machine_productivity_pct: List[Any],
    ) -> AvailableResourcesForecastResult:
        hr_map: Dict[str, float | None] = {}
        mc_map: Dict[str, float | None] = {}

        if isinstance(hr_days, list) and isinstance(hr_avg_actual_staff_pct, list):
            for i in range(min(len(hr_days), len(hr_avg_actual_staff_pct))):
                day = cls._to_day_key(hr_days[i])
                raw = hr_avg_actual_staff_pct[i]
                try:
                    num = float(raw)
                    hr_map[day] = max(0.0, min(100.0, num))
                except Exception:
                    hr_map[day] = None

        if isinstance(machine_days, list) and isinstance(machine_avg_machine_productivity_pct, list):
            for i in range(min(len(machine_days), len(machine_avg_machine_productivity_pct))):
                day = cls._to_day_key(machine_days[i])
                raw = machine_avg_machine_productivity_pct[i]
                try:
                    num = float(raw)
                    mc_map[day] = max(0.0, min(100.0, num))
                except Exception:
                    mc_map[day] = None

        # Keep output axis aligned to HR days (same as chart axis in current UI flow).
        days_out = [cls._to_day_key(d) for d in (hr_days or [])]
        out_values: List[float | None] = []

        for d in days_out:
            hr_v = hr_map.get(d)
            mc_v = mc_map.get(d)
            if hr_v is None or mc_v is None:
                out_values.append(None)
                continue

            val = float(hr_v) * (100.0 - float(mc_v)) / 100.0
            out_values.append(max(0.0, min(100.0, val)))

        return AvailableResourcesForecastResult(
            days=days_out,
            available_resources_pct=out_values,
            meta={
                "formula": "avg_actual_staff_pct * (100 - avg_machine_productivity_pct) / 100",
                "source": "hr_forecast + machine_forecast",
            },
        )
