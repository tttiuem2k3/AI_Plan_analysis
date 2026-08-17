from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models.nguon_luc import DM_CongDoanLon
from ..repositories.ke_hoach_repo import KeHoachRepo


MODEL_DIR_CANDIDATES = [
    "models_AI_forecast",
]


def _day_of_week_vi_t2(d: date) -> str:
    # Monday=0 ... Sunday=6
    wd = int(d.weekday())
    if wd == 6:
        return "CN"
    return f"T{wd + 2}"


def _to_numeric_safe(series: pd.Series) -> pd.Series:
    if series.dtype == "object":
        series = series.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(series, errors="coerce")


@lru_cache(maxsize=4)
def _resolve_model_path(model_name: str) -> str:
    # .../backend/app/ai/hr_forecast_service.py -> .../backend
    backend_dir = Path(__file__).resolve().parents[2]
    for folder in MODEL_DIR_CANDIDATES:
        p = backend_dir / folder / model_name
        if (p / "multilabel_predictor.pkl").exists():
            return str(p)
    raise FileNotFoundError(f"Không tìm thấy model '{model_name}' dưới backend/{'|'.join(MODEL_DIR_CANDIDATES)}")


@lru_cache(maxsize=2)
def _load_hr_model(model_name: str):
    try:
        from .hr_multilabel_predictor import MultilabelPredictor

        model_path = _resolve_model_path(model_name)
        return MultilabelPredictor.load(model_path)
    except FileNotFoundError:
        raise
    except Exception as e:
        raise RuntimeError(f"Không thể load model {model_name}: {e}")


def _list_plan_days(calendar: Dict[str, Any]) -> List[date]:
    days_raw = calendar.get("Days") if isinstance(calendar, dict) else None
    out: List[date] = []

    # Prefer explicit Days list from calendar view.
    if isinstance(days_raw, list) and days_raw:
        for x in days_raw:
            try:
                if isinstance(x, date) and not isinstance(x, datetime):
                    out.append(x)
                else:
                    out.append(date.fromisoformat(str(x)[:10]))
            except Exception:
                continue
        if out:
            return out

    header = calendar.get("Header") if isinstance(calendar, dict) else {}
    tu = header.get("TuNgay") if isinstance(header, dict) else None
    den = header.get("DenNgay") if isinstance(header, dict) else None
    if not tu or not den:
        return []

    try:
        start = tu if isinstance(tu, date) else date.fromisoformat(str(tu)[:10])
        end = den if isinstance(den, date) else date.fromisoformat(str(den)[:10])
    except Exception:
        return []

    cur = start
    while cur <= end:
        out.append(cur)
        cur = cur + timedelta(days=1)
    return out


def _get_major_process_staff(db: Session) -> List[Tuple[str, int]]:
    rows = db.query(DM_CongDoanLon).order_by(DM_CongDoanLon.MaCongDoanLon.asc()).all()
    out: List[Tuple[str, int]] = []
    for r in rows:
        try:
            name = str(r.TenCongDoanLon or "").strip()
            staff = int(float(r.SoNhanSu or 0))
            if not name or staff <= 0:
                continue
            out.append((name, staff))
        except Exception:
            continue
    return out


@dataclass
class HrForecastResult:
    days: List[str]
    avg_actual_staff_pct: List[float | None]
    meta: Dict[str, Any]


class HrForecastService:
    MODEL_NAME = "GENEPA_HR_Model"

    # Expected feature column names (must match training)
    COL_DATE = "work_date"
    COL_DOW = "day_of_week"
    COL_STAGE = "major_process"
    COL_BASE = "standard_staff_qty"

    # Model labels (as trained in GENEPA_HR_Model)
    LABEL_PERCENT_ACTUAL_STAFF = "percent_actual_staff"

    @classmethod
    def forecast_avg_actual_staff_pct_for_plan(cls, db: Session, ke_hoach_id: int) -> HrForecastResult:
        # 1) Load plan days (source of truth: calendar view)
        calendar = KeHoachRepo.get_calendar_view(db, int(ke_hoach_id))
        if not calendar:
            raise HTTPException(status_code=404, detail="Không tìm thấy kế hoạch")

        plan_days = _list_plan_days(calendar)
        if not plan_days:
            raise HTTPException(status_code=400, detail="Kế hoạch chưa có dải ngày hợp lệ để dự báo")

        # 2) Load major processes + standard staff
        stages = _get_major_process_staff(db)
        if not stages:
            raise HTTPException(status_code=400, detail="Không có dữ liệu công đoạn lớn/nhân sự chuẩn (DM_CongDoanLon.SoNhanSu)")

        # 3) Build input DataFrame: day x stage
        rows: List[Dict[str, Any]] = []
        for d in plan_days:
            dow = _day_of_week_vi_t2(d)
            for stage_name, base_staff in stages:
                rows.append(
                    {
                        cls.COL_DATE: pd.to_datetime(d),
                        cls.COL_DOW: dow,
                        cls.COL_STAGE: stage_name,
                        cls.COL_BASE: base_staff,
                    }
                )

        X = pd.DataFrame(rows)

        # 4) Preprocess (must mirror training)
        X[cls.COL_BASE] = _to_numeric_safe(X[cls.COL_BASE])
        X[cls.COL_DATE] = pd.to_datetime(X[cls.COL_DATE], errors="coerce")

        X["year"] = X[cls.COL_DATE].dt.year
        X["month"] = X[cls.COL_DATE].dt.month
        X["day"] = X[cls.COL_DATE].dt.day
        X["dayofweek_num"] = X[cls.COL_DATE].dt.dayofweek
        # Keep compatibility with older training notebooks which used `dow_num`.
        X["dow_num"] = X["dayofweek_num"]
        X["weekofyear"] = X[cls.COL_DATE].dt.isocalendar().week.astype("Int64").astype(float)
        X["is_weekend"] = X["dayofweek_num"].isin([5, 6]).astype(int)

        for c in [cls.COL_DOW, cls.COL_STAGE]:
            X[c] = X[c].astype("category")

        X = X.dropna(subset=[cls.COL_DATE, cls.COL_DOW, cls.COL_STAGE, cls.COL_BASE]).reset_index(drop=True)
        if X.empty:
            raise HTTPException(status_code=400, detail="Dữ liệu đầu vào dự báo không hợp lệ sau tiền xử lý")

        # 5) Predict
        try:
            multi = _load_hr_model(cls.MODEL_NAME)
        except FileNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        try:
            pred = multi.predict(X)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Lỗi dự đoán HR model: {e}")

        if cls.LABEL_PERCENT_ACTUAL_STAFF not in pred.columns:
            raise HTTPException(status_code=500, detail=f"Model không trả về nhãn '{cls.LABEL_PERCENT_ACTUAL_STAFF}'")

        # 6) Average percent_actual_staff by day across all major processes
        df = X[[cls.COL_DATE]].copy()
        df["pct"] = pd.to_numeric(pred[cls.LABEL_PERCENT_ACTUAL_STAFF], errors="coerce")
        df["pct"] = df["pct"].clip(lower=0, upper=100)

        df["day"] = df[cls.COL_DATE].dt.date.astype(str)
        grouped = df.groupby("day", as_index=False)["pct"].mean(numeric_only=True)
        pct_by_day = {str(r["day"]): float(r["pct"]) if pd.notna(r["pct"]) else None for _, r in grouped.iterrows()}

        days_iso = [d.isoformat() for d in plan_days]
        series = [pct_by_day.get(di) for di in days_iso]

        return HrForecastResult(
            days=days_iso,
            avg_actual_staff_pct=series,
            meta={
                "model": cls.MODEL_NAME,
                "major_process_count": len(stages),
                "input_rows": int(len(X)),
                "dow_format": "T2..CN",
                "label": cls.LABEL_PERCENT_ACTUAL_STAFF,
            },
        )

    @classmethod
    def forecast_avg_actual_staff_pct_from_calendar(
        cls,
        *,
        calendar: Dict[str, Any],
        stages: List[Dict[str, Any]] | List[Tuple[str, int]],
    ) -> HrForecastResult:
        """Forecast avg attendance % from a client-provided calendar + stage staffing list.

        This is meant for integrations where the API server must NOT read SQL.

        `stages` accepted shapes:
        - List[Tuple[str,int]]: (major_process_name, standard_staff_qty)
        - List[Dict]: {"major_process": "...", "standard_staff_qty": 10} (key aliases supported)
        """

        plan_days = _list_plan_days(calendar)
        if not plan_days:
            raise HTTPException(status_code=400, detail="Kế hoạch chưa có dải ngày hợp lệ để dự báo")

        stage_pairs: List[Tuple[str, int]] = []
        if isinstance(stages, list):
            for it in stages:
                if isinstance(it, tuple) and len(it) == 2:
                    try:
                        name = str(it[0] or "").strip()
                        staff = int(float(it[1] or 0))
                        if name and staff > 0:
                            stage_pairs.append((name, staff))
                    except Exception:
                        continue
                elif isinstance(it, dict):
                    try:
                        name = str(
                            it.get("major_process")
                            or it.get("major_process_name")
                            or it.get("stage")
                            or it.get("TenCongDoanLon")
                            or ""
                        ).strip()
                        staff_raw = it.get("standard_staff_qty")
                        if staff_raw is None:
                            staff_raw = it.get("SoNhanSu")
                        if staff_raw is None:
                            staff_raw = it.get("staff")
                        staff = int(float(staff_raw or 0))
                        if name and staff > 0:
                            stage_pairs.append((name, staff))
                    except Exception:
                        continue

        # de-dup by name
        dedup: Dict[str, int] = {}
        for name, staff in stage_pairs:
            if name not in dedup:
                dedup[name] = staff
        stage_pairs = [(k, int(v)) for k, v in dedup.items() if k and int(v) > 0]

        if not stage_pairs:
            raise HTTPException(status_code=400, detail="Thiếu danh sách công đoạn lớn/nhân sự chuẩn để dự báo (stages)")

        # Build input DataFrame: day x stage
        rows: List[Dict[str, Any]] = []
        for d in plan_days:
            dow = _day_of_week_vi_t2(d)
            for stage_name, base_staff in stage_pairs:
                rows.append(
                    {
                        cls.COL_DATE: pd.to_datetime(d),
                        cls.COL_DOW: dow,
                        cls.COL_STAGE: stage_name,
                        cls.COL_BASE: base_staff,
                    }
                )

        X = pd.DataFrame(rows)

        # Preprocess (mirror training)
        X[cls.COL_BASE] = _to_numeric_safe(X[cls.COL_BASE])
        X[cls.COL_DATE] = pd.to_datetime(X[cls.COL_DATE], errors="coerce")

        X["year"] = X[cls.COL_DATE].dt.year
        X["month"] = X[cls.COL_DATE].dt.month
        X["day"] = X[cls.COL_DATE].dt.day
        X["dayofweek_num"] = X[cls.COL_DATE].dt.dayofweek
        X["dow_num"] = X["dayofweek_num"]
        X["weekofyear"] = X[cls.COL_DATE].dt.isocalendar().week.astype("Int64").astype(float)
        X["is_weekend"] = X["dayofweek_num"].isin([5, 6]).astype(int)

        for c in [cls.COL_DOW, cls.COL_STAGE]:
            X[c] = X[c].astype("category")

        X = X.dropna(subset=[cls.COL_DATE, cls.COL_DOW, cls.COL_STAGE, cls.COL_BASE]).reset_index(drop=True)
        if X.empty:
            raise HTTPException(status_code=400, detail="Dữ liệu đầu vào dự báo không hợp lệ sau tiền xử lý")

        # Predict
        try:
            multi = _load_hr_model(cls.MODEL_NAME)
        except FileNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        try:
            pred = multi.predict(X)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Lỗi dự đoán HR model: {e}")

        if cls.LABEL_PERCENT_ACTUAL_STAFF not in pred.columns:
            raise HTTPException(status_code=500, detail=f"Model không trả về nhãn '{cls.LABEL_PERCENT_ACTUAL_STAFF}'")

        # Average percent_actual_staff by day across all major processes
        df = X[[cls.COL_DATE]].copy()
        df["pct"] = pd.to_numeric(pred[cls.LABEL_PERCENT_ACTUAL_STAFF], errors="coerce")
        df["pct"] = df["pct"].clip(lower=0, upper=100)

        df["day"] = df[cls.COL_DATE].dt.date.astype(str)
        grouped = df.groupby("day", as_index=False)["pct"].mean(numeric_only=True)
        pct_by_day = {str(r["day"]): float(r["pct"]) if pd.notna(r["pct"]) else None for _, r in grouped.iterrows()}

        days_iso = [d.isoformat() for d in plan_days]
        series = [pct_by_day.get(di) for di in days_iso]

        return HrForecastResult(
            days=days_iso,
            avg_actual_staff_pct=series,
            meta={
                "model": cls.MODEL_NAME,
                "major_process_count": len(stage_pairs),
                "input_rows": int(len(X)),
                "dow_format": "T2..CN",
                "label": cls.LABEL_PERCENT_ACTUAL_STAFF,
                "source": "calendar_payload",
            },
        )
