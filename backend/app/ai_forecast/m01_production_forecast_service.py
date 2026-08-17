from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Set

import numpy as np
import pandas as pd
from fastapi import HTTPException

from .autogluon_quiet import suppress_autogluon_warnings


MODEL_DIR_CANDIDATES = [
    "models_AI_forecast",
]


TARGET_ORDER = [
    "actual_staff_qty",
    "machine_qty",
    "ot_staff_qty",
    "outsourced_staff_qty",
    "actual_qty",
    "defect_qty",
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


def _pick_first(src: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    if not isinstance(src, dict):
        return default
    for k in keys:
        if src.get(k) not in (None, ""):
            return src.get(k)
    return default


def _to_numeric_safe(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def _day_of_week_vi(d: date) -> str:
    wd = int(d.weekday())  # Monday=0
    if wd == 6:
        return "Chủ Nhật"
    names = {
        0: "Thứ Hai",
        1: "Thứ Ba",
        2: "Thứ Tư",
        3: "Thứ Năm",
        4: "Thứ Sáu",
        5: "Thứ Bảy",
    }
    return names.get(wd, "")


def _extract_active_days(row: Dict[str, Any], *, day_set: Set[date]) -> Set[date]:
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

        d = _parse_date_like(_pick_first(item, ["ProductionDate", "Date", "Ngay", "production_date", "date"]))
        if d and d in day_set:
            out.add(d)

    if out:
        return out

    st = _to_datetime_safe(_pick_first(row, ["StartDate", "Start", "StartDT", "PlanWindowStart", "TuNgay", "start_date"]))
    en = _to_datetime_safe(_pick_first(row, ["EndDate", "End", "EndDT", "PlanWindowEnd", "DenNgay", "end_date"]))
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

    if day_set:
        return set(day_set)

    return out


def _daily_qty_for_day(row: Dict[str, Any], day: date) -> float | None:
    daily = row.get("DailyQty") if isinstance(row.get("DailyQty"), list) else []
    if not daily:
        planned_fallback = _pick_first(
            row,
            [
                "planned_qty",
                "PlannedQty",
                "plannedQty",
                "PlannedTotal",
                "TotalQty",
                "SoLuongKeHoach",
                "KeHoachQty",
            ],
        )
        qty = max(0.0, _to_float(planned_fallback, 0.0))
        return qty if qty > 0 else None

    total = 0.0
    hit = False
    for item in daily:
        if not isinstance(item, dict):
            continue
        d = _parse_date_like(_pick_first(item, ["ProductionDate", "Date", "Ngay", "production_date", "date"]))
        if d != day:
            continue
        qty_raw = _pick_first(item, ["Quantity", "Qty", "SoLuong", "quantity", "qty"])
        try:
            qty = float(qty_raw)
        except Exception:
            qty = 0.0
        if qty <= 0:
            continue
        total += qty
        hit = True

    if not hit:
        planned_fallback = _pick_first(
            row,
            [
                "planned_qty",
                "PlannedQty",
                "plannedQty",
                "PlannedTotal",
                "TotalQty",
                "SoLuongKeHoach",
                "KeHoachQty",
            ],
        )
        qty = max(0.0, _to_float(planned_fallback, 0.0))
        return qty if qty > 0 else None
    return max(total, 0.0)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if out != out:
        return float(default)
    return out


def _add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["year"] = out["work_date"].dt.year
    out["month"] = out["work_date"].dt.month
    out["day"] = out["work_date"].dt.day
    out["dow_num"] = out["work_date"].dt.dayofweek
    out["is_weekend"] = out["dow_num"].isin([5, 6]).astype(int)
    return out


@lru_cache(maxsize=4)
def _resolve_model_path(model_name: str) -> str:
    backend_dir = Path(__file__).resolve().parents[2]
    for folder in MODEL_DIR_CANDIDATES:
        p = backend_dir / folder / model_name
        if (p / "production_system.pkl").exists():
            return str(p)
    raise FileNotFoundError(f"Không tìm thấy model '{model_name}' dưới backend/{'|'.join(MODEL_DIR_CANDIDATES)}")


class _M01ModelSystem:
    file_name = "production_system.pkl"

    def __init__(self, model_dir: str):
        try:
            from autogluon.core.utils.loaders import load_pkl
            from autogluon.tabular import TabularPredictor
        except Exception as e:
            raise RuntimeError(f"Thiếu AutoGluon dependency: {e}")

        metadata = load_pkl.load(str(Path(model_dir) / self.file_name))

        raw_paths = metadata.get("model_paths") if isinstance(metadata.get("model_paths"), dict) else {}
        if not raw_paths:
            raise RuntimeError("production_system.pkl thiếu model_paths")

        self.feature_map = metadata.get("feature_map") if isinstance(metadata.get("feature_map"), dict) else {}
        self.targets = metadata.get("targets") if isinstance(metadata.get("targets"), list) else list(raw_paths.keys())

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
                        last_type_error = te
                        continue
                    except Exception:
                        continue
                if last_type_error is not None:
                    raise ex

                try:
                    return _load_quiet(require_version_match=False)
                except TypeError:
                    raise ex

        self.models: Dict[str, Any] = {}
        for target, path in raw_paths.items():
            p = str(path or "")
            if not p:
                continue
            p_name = Path(p).name
            candidate = Path(model_dir) / p_name
            if candidate.exists():
                p = str(candidate)
            self.models[str(target)] = _load_predictor(p)


@lru_cache(maxsize=2)
def _load_m01_model(model_name: str) -> _M01ModelSystem:
    model_path = _resolve_model_path(model_name)
    return _M01ModelSystem(model_path)


@dataclass
class M01ProductionForecastResult:
    days: List[str]
    total_actual_staff_qty: List[int | None]
    total_machine_qty: List[int | None]
    total_ot_staff_qty: List[int | None]
    total_outsourced_staff_qty: List[int | None]
    total_actual_qty: List[int | None]
    total_defect_qty: List[int | None]
    meta: Dict[str, Any]


class M01ProductionForecastService:
    MODEL_NAME = "GENEPA_M01_model"

    COL_DATE = "work_date"
    COL_DOW = "day_of_week"
    COL_PRIORITY = "production_priority"
    COL_ORDER_ID = "production_order_id"
    COL_PO = "po_number"
    COL_ITEM_CODE = "item_code"
    COL_ITEM_NAME = "item_name"
    COL_PLANNED = "planned_qty"
    COL_MAJOR = "major_process"
    COL_STD_STAFF = "standard_staff_qty"
    COL_PROCESS = "process"
    COL_RESOURCE = "resource"
    COL_TIME_NORM = "time_norm"

    @classmethod
    def _normalize_input_rows(cls, calendar: Dict[str, Any]) -> tuple[List[date], pd.DataFrame]:
        plan_days = _list_plan_days(calendar)
        if not plan_days:
            raise HTTPException(status_code=400, detail="Kế hoạch chưa có dải ngày hợp lệ để dự báo M01")

        day_set = set(plan_days)
        rows = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
        if not rows:
            raise HTTPException(status_code=400, detail="Kế hoạch chưa có dữ liệu chi tiết để dự báo M01")

        out: List[Dict[str, Any]] = []
        seen: Set[tuple[str, str, str, str, str]] = set()

        for r in rows:
            if not isinstance(r, dict):
                continue

            process = str(_pick_first(r, ["TenCongDoan", "PhaseName", "MaCongDoan", "process", "Process"], "") or "").strip()
            major_process = str(_pick_first(r, ["TenBoPhan", "TenCongDoanLon", "PhaseGroupName", "MaCongDoanLon", "major_process"], "") or "").strip()
            resource = str(_pick_first(r, ["TenNguonLuc", "ResourceNames", "NguonLucText", "MaNguonLuc", "ResourceIDs", "resource"], "") or "").strip()

            item_code = str(
                _pick_first(r, ["item_code", "ItemCode", "item_id", "ItemID", "MaBTP", "MaTP", "BTP_DinhMucID", "TP_DinhMucID"], "")
                or ""
            ).strip()
            item_name = str(
                _pick_first(
                    r,
                    [
                        "item_name",
                        "ItemName",
                        "SemiFinishedProductName",
                        "FinishedProductName",
                        "TenBanThanhPham",
                        "TenThanhPham",
                    ],
                    "",
                )
                or ""
            ).strip()

            order_id = str(_pick_first(r, ["production_order_id", "DonHangID", "LineKey"], "") or "").strip()
            po_number = str(_pick_first(r, ["po_number", "SoChungTu", "SoDonHang"], "") or "").strip()

            priority = _pick_first(r, ["production_priority", "ThuTuSX"])
            std_staff = _pick_first(r, ["standard_staff_qty", "SoNhanSuBoPhan", "SoNhanSu", "staff"])
            time_norm = _pick_first(r, ["time_norm", "DinhMucThoiGian", "TimeNorm", "TimeLimit", "ThoiGianThietLapMay"])

            if not process or not major_process:
                continue
            if not item_code or not item_name or not resource:
                continue

            if std_staff is None or time_norm is None:
                continue

            active_days = _extract_active_days(r, day_set=day_set)
            if not active_days:
                continue

            active_days_sorted = sorted(active_days)
            for d in active_days_sorted:
                day_key = d.isoformat()
                planned_qty = _daily_qty_for_day(r, d)
                if planned_qty is None:
                    continue
                planned_qty = max(0.0, _to_float(planned_qty, 0.0))

                dedup_key = (day_key, item_code, major_process, process, resource)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                out.append(
                    {
                        cls.COL_DATE: pd.to_datetime(d),
                        cls.COL_DOW: _day_of_week_vi(d),
                        cls.COL_PRIORITY: priority,
                        cls.COL_ORDER_ID: order_id,
                        cls.COL_PO: po_number,
                        cls.COL_ITEM_CODE: item_code,
                        cls.COL_ITEM_NAME: item_name,
                        cls.COL_PLANNED: planned_qty,
                        cls.COL_MAJOR: major_process,
                        cls.COL_STD_STAFF: std_staff,
                        cls.COL_PROCESS: process,
                        cls.COL_RESOURCE: resource,
                        cls.COL_TIME_NORM: time_norm,
                    }
                )

        if not out:
            raise HTTPException(status_code=400, detail="Không trích xuất được input hợp lệ cho dự báo M01")

        return plan_days, pd.DataFrame(out)

    @classmethod
    def _preprocess_input_data(cls, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out[cls.COL_DATE] = pd.to_datetime(out[cls.COL_DATE], errors="coerce")

        num_cols = [cls.COL_PRIORITY, cls.COL_PLANNED, cls.COL_STD_STAFF, cls.COL_TIME_NORM]
        for c in num_cols:
            if c in out.columns:
                out[c] = _to_numeric_safe(out[c])

        cat_cols = [cls.COL_DOW, cls.COL_ORDER_ID, cls.COL_PO, cls.COL_ITEM_CODE, cls.COL_MAJOR, cls.COL_PROCESS, cls.COL_RESOURCE]
        for c in cat_cols:
            if c in out.columns:
                out[c] = out[c].astype("category")

        if cls.COL_ITEM_NAME in out.columns:
            out[cls.COL_ITEM_NAME] = out[cls.COL_ITEM_NAME].astype(str)

        out = _add_time_features(out)

        out = out.dropna(
            subset=[
                cls.COL_DATE,
                cls.COL_DOW,
                cls.COL_ITEM_CODE,
                cls.COL_MAJOR,
                cls.COL_PROCESS,
                cls.COL_RESOURCE,
                cls.COL_PLANNED,
                cls.COL_STD_STAFF,
                cls.COL_TIME_NORM,
            ]
        ).reset_index(drop=True)
        if out.empty:
            raise HTTPException(status_code=400, detail="Dữ liệu đầu vào M01 không hợp lệ sau tiền xử lý")

        return out

    @classmethod
    def _predict_from_calendar(cls, calendar: Dict[str, Any]) -> M01ProductionForecastResult:
        plan_days, raw = cls._normalize_input_rows(calendar)
        X = cls._preprocess_input_data(raw)

        try:
            system = _load_m01_model(cls.MODEL_NAME)
        except FileNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Không thể load model M01: {e}")

        df_roll = X.copy()
        results = X.copy()

        targets_used: List[str] = []

        for target in TARGET_ORDER:
            model = system.models.get(target)
            if model is None:
                continue

            features = system.feature_map.get(target)
            if not isinstance(features, list) or not features:
                continue

            for feat in features:
                if feat in df_roll.columns:
                    continue
                if feat in results.columns:
                    df_roll[feat] = results[feat]
                    continue
                raise HTTPException(
                    status_code=400,
                    detail=f"Thiếu feature bắt buộc '{feat}' cho target '{target}' của model M01",
                )

            try:
                pred = model.predict(df_roll[features])
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Lỗi dự đoán target '{target}' của M01: {e}")

            pred = np.clip(pd.to_numeric(pred, errors="coerce"), 0, None)
            pred = pred.round().fillna(0).astype(int)

            results[f"pred_{target}"] = pred.values
            df_roll[target] = pred.values
            targets_used.append(target)

        if not targets_used:
            raise HTTPException(status_code=500, detail="Không có target nào được dự đoán từ GENEPA_M01_model")

        df = results[[cls.COL_DATE]].copy()
        df["day"] = df[cls.COL_DATE].dt.date.astype(str)

        for t in TARGET_ORDER:
            col = f"pred_{t}"
            df[col] = pd.to_numeric(results.get(col), errors="coerce").fillna(0)

        # Business guard-rail: actual production cannot be negative.
        if "pred_actual_qty" in df.columns:
            df["pred_actual_qty"] = np.clip(pd.to_numeric(df["pred_actual_qty"], errors="coerce").fillna(0), 0, None)

        grouped = df.groupby("day", as_index=False).sum(numeric_only=True)

        by_day: Dict[str, Dict[str, int]] = {}
        for _, r in grouped.iterrows():
            day = str(r.get("day"))
            by_day[day] = {
                "actual_staff_qty": int(round(float(r.get("pred_actual_staff_qty", 0) or 0))),
                "machine_qty": int(round(float(r.get("pred_machine_qty", 0) or 0))),
                "ot_staff_qty": int(round(float(r.get("pred_ot_staff_qty", 0) or 0))),
                "outsourced_staff_qty": int(round(float(r.get("pred_outsourced_staff_qty", 0) or 0))),
                "actual_qty": max(0, int(round(float(r.get("pred_actual_qty", 0) or 0)))),
                "defect_qty": int(round(float(r.get("pred_defect_qty", 0) or 0))),
            }

        days_iso = [d.isoformat() for d in plan_days]

        return M01ProductionForecastResult(
            days=days_iso,
            total_actual_staff_qty=[by_day.get(di, {}).get("actual_staff_qty", 0) for di in days_iso],
            total_machine_qty=[by_day.get(di, {}).get("machine_qty", 0) for di in days_iso],
            total_ot_staff_qty=[by_day.get(di, {}).get("ot_staff_qty", 0) for di in days_iso],
            total_outsourced_staff_qty=[by_day.get(di, {}).get("outsourced_staff_qty", 0) for di in days_iso],
            total_actual_qty=[by_day.get(di, {}).get("actual_qty", 0) for di in days_iso],
            total_defect_qty=[by_day.get(di, {}).get("defect_qty", 0) for di in days_iso],
            meta={
                "model": cls.MODEL_NAME,
                "days_source": "plan_days",
                "input_rows": int(len(X)),
                "feature_groups": int(len(raw)),
                "targets_used": targets_used,
            },
        )

    @classmethod
    def forecast_daily_production_from_calendar(cls, *, calendar: Dict[str, Any]) -> M01ProductionForecastResult:
        if not isinstance(calendar, dict):
            raise HTTPException(status_code=400, detail="calendar phải là object")
        return cls._predict_from_calendar(calendar)
