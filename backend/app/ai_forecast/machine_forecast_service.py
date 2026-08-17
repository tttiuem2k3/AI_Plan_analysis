from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import numpy as np
import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..repositories.ke_hoach_repo import KeHoachRepo
from .autogluon_quiet import suppress_autogluon_warnings


MODEL_DIR_CANDIDATES = [
    "models_AI_forecast",
]


def _parse_date_like(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _to_datetime_safe(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date) and not isinstance(v, datetime):
        return datetime.combine(v, datetime.min.time())
    s = str(v).strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19] if "T" in fmt or ":" in fmt else s[:10], fmt)
            except Exception:
                continue
    return None


def _list_plan_days(calendar: Dict[str, Any]) -> List[date]:
    days_raw = calendar.get("Days") if isinstance(calendar, dict) else None
    out: List[date] = []

    if isinstance(days_raw, list) and days_raw:
        for x in days_raw:
            d = _parse_date_like(x)
            if d is not None:
                out.append(d)
        if out:
            return out

    header = calendar.get("Header") if isinstance(calendar, dict) else {}
    tu = header.get("TuNgay") if isinstance(header, dict) else None
    den = header.get("DenNgay") if isinstance(header, dict) else None
    start = _parse_date_like(tu)
    end = _parse_date_like(den)
    if not start or not end:
        return []

    cur = start
    while cur <= end:
        out.append(cur)
        cur = cur + timedelta(days=1)
    return out


def _split_machine_values(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        vals = [str(x or "").strip() for x in v]
        return [x for x in vals if x]
    s = str(v).strip()
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x and str(x).strip()]


def _is_human_resource(resource_code: Any, resource_name: Any) -> bool:
    code = str(resource_code or "").strip().upper()
    name = str(resource_name or "").strip().lower()

    # Business rule: MaNguonLuc = M000 represents labor.
    if code == "M000":
        return True

    # Safety fallback for payloads missing code mapping.
    if "nhân công" in name or "nhan cong" in name:
        return True

    return False


def _extract_machine_names_from_row(row: Dict[str, Any]) -> List[str]:
    names = _split_machine_values(_pick_first(row, ["TenNguonLuc", "ResourceNames", "NguonLucText", "machine_name"]))
    codes = _split_machine_values(_pick_first(row, ["MaNguonLuc", "ResourceIDs"]))

    out: List[str] = []

    if names and codes:
        n = max(len(names), len(codes))
        for i in range(n):
            name = names[i] if i < len(names) else ""
            code = codes[i] if i < len(codes) else ""
            if _is_human_resource(code, name):
                continue
            machine_name = str(name or code or "").strip()
            if machine_name:
                out.append(machine_name)
    elif names:
        for name in names:
            if _is_human_resource("", name):
                continue
            out.append(str(name).strip())
    else:
        for code in codes:
            if _is_human_resource(code, ""):
                continue
            out.append(str(code).strip())

    # de-dup while preserving order
    dedup: List[str] = []
    seen: Set[str] = set()
    for v in out:
        k = str(v or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        dedup.append(k)

    return dedup


def _pick_first(src: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    if not isinstance(src, dict):
        return default
    for k in keys:
        if src.get(k) not in (None, ""):
            return src.get(k)
    return default


def _extract_daily_dates(row: Dict[str, Any], *, day_set: Set[date]) -> Set[date]:
    out: Set[date] = set()

    daily = row.get("DailyQty") if isinstance(row.get("DailyQty"), list) else []
    for item in daily:
        if not isinstance(item, dict):
            continue
        qty_raw = _pick_first(item, ["Quantity", "Qty", "SoLuong", "quantity", "qty"])
        try:
            qty = float(qty_raw)
        except Exception:
            qty = 0.0
        if qty <= 0:
            continue

        d = _parse_date_like(
            _pick_first(item, ["ProductionDate", "Date", "Ngay", "production_date", "date"])
        )
        if d and d in day_set:
            out.add(d)

    if out:
        return out

    st = _to_datetime_safe(
        _pick_first(row, ["StartDate", "Start", "StartDT", "PlanWindowStart", "TuNgay", "start_date"])
    )
    en = _to_datetime_safe(
        _pick_first(row, ["EndDate", "End", "EndDT", "PlanWindowEnd", "DenNgay", "end_date"])
    )
    if st and en:
        d0 = st.date()
        d1 = en.date()
        if d1 < d0:
            d0, d1 = d1, d0
        cur = d0
        while cur <= d1:
            if cur in day_set:
                out.add(cur)
            cur = cur + timedelta(days=1)

    if out:
        return out

    return out


def _normalize_machine_feature_rows(calendar: Dict[str, Any]) -> tuple[List[date], pd.DataFrame]:
    plan_days = _list_plan_days(calendar)
    if not plan_days:
        raise HTTPException(status_code=400, detail="Kế hoạch chưa có dải ngày hợp lệ để dự báo máy")

    day_set = set(plan_days)
    rows = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
    if not rows:
        raise HTTPException(status_code=400, detail="Kế hoạch chưa có dữ liệu công đoạn/máy để dự báo")

    out_records: List[Dict[str, Any]] = []
    seen: Set[tuple[str, str, str, str]] = set()

    for r in rows:
        if not isinstance(r, dict):
            continue

        major_process = str(_pick_first(r, ["TenBoPhan", "PhaseGroupName", "MaCongDoanLon"], "") or "").strip()
        process = str(_pick_first(r, ["TenCongDoan", "PhaseName", "MaCongDoan"], "") or "").strip()

        machine_names = _extract_machine_names_from_row(r)

        if not major_process or not process or not machine_names:
            continue

        active_days = _extract_daily_dates(r, day_set=day_set)
        if not active_days:
            continue

        for d in sorted(active_days):
            dow = d.strftime("%A")
            for machine_name in machine_names:
                mname = str(machine_name or "").strip()
                if not mname:
                    continue
                dedup_key = (d.isoformat(), major_process, process, mname)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                out_records.append(
                    {
                        "work_date": pd.to_datetime(d),
                        "day_of_week": dow,
                        "major_process": major_process,
                        "process": process,
                        "machine_name": mname,
                    }
                )

    if not out_records:
        raise HTTPException(status_code=400, detail="Không trích xuất được tổ hợp ngày/công đoạn/máy để dự báo")

    return plan_days, pd.DataFrame(out_records)


def _add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["year"] = out[MachineForecastService.COL_DATE].dt.year
    out["month"] = out[MachineForecastService.COL_DATE].dt.month
    out["day"] = out[MachineForecastService.COL_DATE].dt.day
    out["dow_num"] = out[MachineForecastService.COL_DATE].dt.dayofweek
    out["weekofyear"] = out[MachineForecastService.COL_DATE].dt.isocalendar().week.astype("Int64").astype(float)
    out["is_weekend"] = out["dow_num"].isin([5, 6]).astype(int)
    return out


def _clip_usage(x: Any):
    return np.clip(x, 0, 100)


def _clip_productivity(x: Any):
    return np.clip(x, 0, None)


@lru_cache(maxsize=4)
def _resolve_model_path(model_name: str) -> str:
    backend_dir = Path(__file__).resolve().parents[2]
    for folder in MODEL_DIR_CANDIDATES:
        p = backend_dir / folder / model_name
        if (p / "machine_autoreg.pkl").exists():
            return str(p)
    raise FileNotFoundError(f"Không tìm thấy model '{model_name}' dưới backend/{'|'.join(MODEL_DIR_CANDIDATES)}")


class _MachineAutoregPredictor:
    file_name = "machine_autoreg.pkl"

    def __init__(self, model_dir: str):
        try:
            from autogluon.core.utils.loaders import load_pkl
        except Exception as e:
            raise RuntimeError(f"Thiếu autogluon loader: {e}")

        meta = load_pkl.load(str(Path(model_dir) / self.file_name))

        self.p1_path = str(meta.get("p1") or "")
        self.p2_path = str(meta.get("p2") or "")
        if not self.p1_path or not self.p2_path:
            raise RuntimeError("machine_autoreg.pkl thiếu đường dẫn p1/p2")

        # Normalize old absolute paths (e.g. kaggle/working) to local model dir.
        p1_name = Path(self.p1_path).name
        p2_name = Path(self.p2_path).name
        p1_candidate = Path(model_dir) / p1_name
        p2_candidate = Path(model_dir) / p2_name
        if p1_candidate.exists():
            self.p1_path = str(p1_candidate)
        if p2_candidate.exists():
            self.p2_path = str(p2_candidate)

        self._p1 = None
        self._p2 = None

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        try:
            from autogluon.tabular import TabularPredictor
        except Exception as e:
            raise RuntimeError(f"Thiếu autogluon.tabular.TabularPredictor: {e}")

        def _load_predictor(path: str):
            def _load_quiet(**kwargs):
                with suppress_autogluon_warnings():
                    return TabularPredictor.load(path, **kwargs)

            try:
                return _load_quiet()
            except Exception as ex:
                msg = str(ex)
                if (
                    "require_version_match" not in msg
                    and "require_py_version_match" not in msg
                    and "version" not in msg.lower()
                    and "python" not in msg.lower()
                ):
                    raise

                trial_kwargs = [
                    {"require_version_match": False},
                    {"require_py_version_match": False},
                    {"require_version_match": False, "require_py_version_match": False},
                ]

                last_type_error = None
                for kwargs in trial_kwargs:
                    try:
                        return _load_quiet(**kwargs)
                    except TypeError as te:
                        # Some versions may not support one/both kwargs.
                        last_type_error = te
                        continue
                    except Exception:
                        continue

                if last_type_error is not None:
                    raise ex

                try:
                    return _load_quiet(require_version_match=False)
                except TypeError:
                    # Older/newer API without this kwarg -> keep original exception context.
                    raise ex

        if self._p1 is None:
            self._p1 = _load_predictor(self.p1_path)
        if self._p2 is None:
            self._p2 = _load_predictor(self.p2_path)

        Xp = X.copy()
        usage_pred = _clip_usage(self._p1.predict(Xp))
        Xp[MachineForecastService.LABEL_USAGE] = usage_pred
        prod_pred = _clip_productivity(self._p2.predict(Xp))

        return pd.DataFrame(
            {
                MachineForecastService.LABEL_USAGE: usage_pred,
                MachineForecastService.LABEL_PRODUCTIVITY: prod_pred,
            }
        )


@lru_cache(maxsize=2)
def _load_machine_model(model_name: str) -> _MachineAutoregPredictor:
    model_path = _resolve_model_path(model_name)
    return _MachineAutoregPredictor(model_path)


@dataclass
class MachineForecastResult:
    days: List[str]
    avg_machine_productivity_pct: List[float | None]
    avg_machine_usage_pct: List[float | None]
    meta: Dict[str, Any]


class MachineForecastService:
    MODEL_NAME = "GENEPA_MC_Model"

    COL_DATE = "work_date"
    COL_DOW = "day_of_week"
    COL_MAJOR = "major_process"
    COL_PROC = "process"
    COL_MACH = "machine_name"

    LABEL_USAGE = "machine_usage_percent"
    LABEL_PRODUCTIVITY = "avg_machine_productivity"

    @classmethod
    def _preprocess_input_data(cls, df: pd.DataFrame) -> pd.DataFrame:
        x = df.copy()
        x[cls.COL_DATE] = pd.to_datetime(x[cls.COL_DATE], errors="coerce")
        x = _add_time_features(x)

        for c in [cls.COL_DOW, cls.COL_MAJOR, cls.COL_PROC, cls.COL_MACH]:
            x[c] = x[c].astype("category")

        x = x.dropna(subset=[cls.COL_DATE, cls.COL_DOW, cls.COL_MAJOR, cls.COL_PROC, cls.COL_MACH]).reset_index(drop=True)
        if x.empty:
            raise HTTPException(status_code=400, detail="Dữ liệu đầu vào dự báo máy không hợp lệ sau tiền xử lý")
        return x

    @classmethod
    def _predict_from_calendar(cls, calendar: Dict[str, Any]) -> MachineForecastResult:
        plan_days, raw = _normalize_machine_feature_rows(calendar)
        X = cls._preprocess_input_data(raw)

        try:
            model = _load_machine_model(cls.MODEL_NAME)
        except FileNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Không thể load model máy: {e}")

        try:
            pred = model.predict(X)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Lỗi dự đoán model máy: {e}")

        if cls.LABEL_USAGE not in pred.columns or cls.LABEL_PRODUCTIVITY not in pred.columns:
            raise HTTPException(
                status_code=500,
                detail=f"Model không trả về đủ nhãn '{cls.LABEL_USAGE}' và '{cls.LABEL_PRODUCTIVITY}'",
            )

        df = X[[cls.COL_DATE]].copy()
        df["usage"] = pd.to_numeric(pred[cls.LABEL_USAGE], errors="coerce").clip(lower=0, upper=100)
        df["prod"] = pd.to_numeric(pred[cls.LABEL_PRODUCTIVITY], errors="coerce").clip(lower=0, upper=100)
        df["day"] = df[cls.COL_DATE].dt.date.astype(str)

        grouped = df.groupby("day", as_index=False)[["usage", "prod"]].mean(numeric_only=True)
        usage_by_day = {str(r["day"]): (float(r["usage"]) if pd.notna(r["usage"]) else None) for _, r in grouped.iterrows()}
        prod_by_day = {str(r["day"]): (float(r["prod"]) if pd.notna(r["prod"]) else None) for _, r in grouped.iterrows()}

        days_iso = [d.isoformat() for d in plan_days]
        avg_usage_series = [usage_by_day.get(di) for di in days_iso]
        avg_prod_series = [prod_by_day.get(di) for di in days_iso]

        return MachineForecastResult(
            days=days_iso,
            avg_machine_productivity_pct=avg_prod_series,
            avg_machine_usage_pct=avg_usage_series,
            meta={
                "model": cls.MODEL_NAME,
                "input_rows": int(len(X)),
                "feature_groups": int(len(raw)),
                "label_usage": cls.LABEL_USAGE,
                "label_productivity": cls.LABEL_PRODUCTIVITY,
            },
        )

    @classmethod
    def forecast_avg_machine_productivity_pct_for_plan(cls, db: Session, ke_hoach_id: int) -> MachineForecastResult:
        calendar = KeHoachRepo.get_calendar_view(db, int(ke_hoach_id))
        if not calendar:
            raise HTTPException(status_code=404, detail="Không tìm thấy kế hoạch")
        return cls._predict_from_calendar(calendar)

    @classmethod
    def forecast_avg_machine_productivity_pct_from_calendar(cls, *, calendar: Dict[str, Any]) -> MachineForecastResult:
        if not isinstance(calendar, dict):
            raise HTTPException(status_code=400, detail="calendar phải là object")
        return cls._predict_from_calendar(calendar)
