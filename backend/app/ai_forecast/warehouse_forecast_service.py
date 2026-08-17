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


def _split_values(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        vals = [str(x or "").strip() for x in v]
        return [x for x in vals if x]
    s = str(v).strip()
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x and str(x).strip()]


def _normalize_transaction_type(x: Any) -> str:
    if x is None:
        return ""
    t = str(x).strip().upper()
    if t in ("NHAP", "NHẬP", "IMPORT", "IN", "RECEIPT"):
        return "IMPORT"
    if t in ("XUAT", "XUẤT", "EXPORT", "OUT", "ISSUE"):
        return "EXPORT"
    return t


def _to_model_transaction_type(group: str) -> str:
    g = _normalize_transaction_type(group)
    if g == "IMPORT":
        return "Nhập"
    if g == "EXPORT":
        return "Xuất"
    return str(group or "")


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
        if abs(qty) <= 0:
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

    return out


def _infer_transaction_types(row: Dict[str, Any]) -> List[str]:
    txn_raw = _pick_first(row, ["transaction_type", "TransactionType", "LoaiGiaoDich", "LoaiNhapXuat"])
    txn_list = _split_values(txn_raw)
    explicit: List[str] = []
    for txn in txn_list:
        txn_norm = _normalize_transaction_type(txn)
        if txn_norm in ("IMPORT", "EXPORT"):
            explicit.append(txn_norm)
    if explicit:
        return explicit

    # Fallback: infer from DailyQty sign if transaction_type is not provided.
    daily = row.get("DailyQty") if isinstance(row.get("DailyQty"), list) else []
    has_pos = False
    has_neg = False
    for item in daily:
        if not isinstance(item, dict):
            continue
        qty_raw = _pick_first(item, ["Quantity", "Qty", "SoLuong", "quantity", "qty"])
        try:
            qty = float(qty_raw)
        except Exception:
            qty = 0.0
        if qty > 0:
            has_pos = True
        if qty < 0:
            has_neg = True

    if has_pos and has_neg:
        return ["IMPORT", "EXPORT"]
    if has_pos:
        return ["IMPORT"]
    if has_neg:
        return ["EXPORT"]

    # Strict mode: thiếu transaction type thì bỏ row, không tự sinh hướng giao dịch.
    return []


def _clip_non_negative(x: Any):
    return np.clip(x, 0, None)


@lru_cache(maxsize=4)
def _resolve_model_path(model_name: str) -> str:
    backend_dir = Path(__file__).resolve().parents[2]
    for folder in MODEL_DIR_CANDIDATES:
        p = backend_dir / folder / model_name
        if (p / "warehouse_autoreg.pkl").exists():
            return str(p)
    raise FileNotFoundError(f"Không tìm thấy model '{model_name}' dưới backend/{'|'.join(MODEL_DIR_CANDIDATES)}")


class _WarehouseAutoregPredictor:
    file_name = "warehouse_autoreg.pkl"

    def __init__(self, model_dir: str):
        try:
            from autogluon.core.utils.loaders import load_pkl
        except Exception as e:
            raise RuntimeError(f"Thiếu autogluon loader: {e}")

        meta = load_pkl.load(str(Path(model_dir) / self.file_name))

        self.p1_path = str(meta.get("p1") or "")
        self.p2_path = str(meta.get("p2") or "")
        if not self.p1_path or not self.p2_path:
            raise RuntimeError("warehouse_autoreg.pkl thiếu đường dẫn p1/p2")

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

        if self._p1 is None:
            self._p1 = _load_predictor(self.p1_path)
        if self._p2 is None:
            self._p2 = _load_predictor(self.p2_path)

        Xp = X.copy()
        opening_stock_pred = _clip_non_negative(self._p1.predict(Xp))
        Xp[WarehouseForecastService.LABEL_STOCK] = opening_stock_pred
        quantity_pred = _clip_non_negative(self._p2.predict(Xp))

        return pd.DataFrame(
            {
                WarehouseForecastService.LABEL_STOCK: opening_stock_pred,
                WarehouseForecastService.LABEL_QTY: quantity_pred,
            }
        )


@lru_cache(maxsize=2)
def _load_warehouse_model(model_name: str) -> _WarehouseAutoregPredictor:
    model_path = _resolve_model_path(model_name)
    return _WarehouseAutoregPredictor(model_path)


@dataclass
class WarehouseForecastResult:
    days: List[str]
    total_opening_stock: List[float | None]
    total_import_qty: List[int | None]
    total_export_qty: List[int | None]
    meta: Dict[str, Any]


class WarehouseForecastService:
    MODEL_NAME = "GENEPA_WH_Model"

    COL_DATE = "work_date"
    COL_DOW = "day_of_week"
    COL_TXN = "transaction_type"
    COL_WAREHOUSE = "warehouse_id"
    COL_ITEM = "item_id"
    COL_PROCESS = "process"

    LABEL_STOCK = "opening_stock"
    LABEL_QTY = "quantity"

    FEATURES_STOCK = [COL_DATE, COL_DOW, COL_TXN, COL_WAREHOUSE, COL_ITEM, COL_PROCESS]
    FEATURES_QTY = FEATURES_STOCK + [LABEL_STOCK]

    @classmethod
    def _normalize_input_rows(cls, calendar: Dict[str, Any]) -> tuple[List[date], pd.DataFrame]:
        plan_days = _list_plan_days(calendar)
        if not plan_days:
            raise HTTPException(status_code=400, detail="Kế hoạch chưa có dải ngày hợp lệ để dự báo kho")

        day_set = set(plan_days)

        rows = calendar.get("Rows") if isinstance(calendar.get("Rows"), list) else []
        if not rows:
            raise HTTPException(status_code=400, detail="Kế hoạch chưa có dữ liệu giao dịch kho để dự báo")

        out: List[Dict[str, Any]] = []
        seen: Set[tuple[str, str, str, str, str]] = set()

        for r in rows:
            if not isinstance(r, dict):
                continue

            process = str(_pick_first(r, ["process", "Process", "TenCongDoan", "PhaseName", "MaCongDoan"], "") or "").strip()
            item_id = str(_pick_first(r, ["item_id", "ItemID", "MaMatHang", "MaHang", "MaBTP", "MaTP", "BTP_DinhMucID", "TP_DinhMucID"], "") or "").strip()
            warehouse_id = str(_pick_first(r, ["warehouse_id", "WarehouseID", "MaKho", "Kho", "KhoID", "warehouse"], "") or "").strip()

            if not process:
                continue
            if not item_id or not warehouse_id:
                continue

            txn_types = _infer_transaction_types(r)
            if not txn_types:
                continue

            active_days = _extract_active_days(r, day_set=day_set)
            if not active_days:
                continue

            for d in sorted(active_days):
                day_key = d.isoformat()
                dow_name = d.strftime("%A")

                for txn_norm in txn_types:
                    txn_group = _normalize_transaction_type(txn_norm)
                    if txn_group not in ("IMPORT", "EXPORT"):
                        continue
                    txn_model = _to_model_transaction_type(txn_group)

                    k = (day_key, txn_group, warehouse_id, item_id, process)
                    if k in seen:
                        continue
                    seen.add(k)

                    out.append(
                        {
                            cls.COL_DATE: pd.to_datetime(d),
                            cls.COL_DOW: dow_name,
                            cls.COL_TXN: txn_model,
                            cls.COL_WAREHOUSE: warehouse_id,
                            cls.COL_ITEM: item_id,
                            cls.COL_PROCESS: process,
                        }
                    )

        if not out:
            raise HTTPException(status_code=400, detail="Không trích xuất được tổ hợp giao dịch kho để dự báo")

        return plan_days, pd.DataFrame(out)

    @classmethod
    def _preprocess_input_data(cls, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out[cls.COL_DATE] = pd.to_datetime(out[cls.COL_DATE], errors="coerce")

        for c in [cls.COL_DOW, cls.COL_TXN, cls.COL_WAREHOUSE, cls.COL_ITEM, cls.COL_PROCESS]:
            if c in out.columns:
                out[c] = out[c].astype(str)

        if cls.LABEL_STOCK in out.columns:
            out[cls.LABEL_STOCK] = pd.to_numeric(out[cls.LABEL_STOCK], errors="coerce")

        if cls.LABEL_QTY in out.columns:
            out[cls.LABEL_QTY] = pd.to_numeric(out[cls.LABEL_QTY], errors="coerce")

        out = out.dropna(subset=[cls.COL_DATE, cls.COL_DOW, cls.COL_TXN, cls.COL_WAREHOUSE, cls.COL_ITEM, cls.COL_PROCESS]).reset_index(drop=True)
        if out.empty:
            raise HTTPException(status_code=400, detail="Dữ liệu đầu vào dự báo kho không hợp lệ sau tiền xử lý")
        return out

    @classmethod
    def _predict_from_calendar(cls, calendar: Dict[str, Any]) -> WarehouseForecastResult:
        plan_days, raw = cls._normalize_input_rows(calendar)
        X = cls._preprocess_input_data(raw)

        try:
            model = _load_warehouse_model(cls.MODEL_NAME)
        except FileNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Không thể load model kho: {e}")

        try:
            pred = model.predict(X)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Lỗi dự đoán model kho: {e}")

        if cls.LABEL_STOCK not in pred.columns or cls.LABEL_QTY not in pred.columns:
            raise HTTPException(
                status_code=500,
                detail=f"Model không trả về đủ nhãn '{cls.LABEL_STOCK}' và '{cls.LABEL_QTY}'",
            )

        df = X[[cls.COL_DATE, cls.COL_TXN, cls.COL_WAREHOUSE, cls.COL_ITEM, cls.COL_PROCESS]].copy()
        df["opening_stock"] = pd.to_numeric(pred[cls.LABEL_STOCK], errors="coerce").clip(lower=0)
        df["qty"] = pd.to_numeric(pred[cls.LABEL_QTY], errors="coerce").clip(lower=0)
        df["day"] = df[cls.COL_DATE].dt.date.astype(str)
        df["txn_group"] = df[cls.COL_TXN].map(_normalize_transaction_type)

        days_iso = [d.isoformat() for d in plan_days]

        stock_base = (
            df.groupby(["day", cls.COL_WAREHOUSE, cls.COL_ITEM, cls.COL_PROCESS], as_index=False)["opening_stock"]
            .mean(numeric_only=True)
        )
        stock_sum = stock_base.groupby("day", as_index=False)["opening_stock"].sum(numeric_only=True)
        stock_by_day = {str(r["day"]): float(r["opening_stock"]) if pd.notna(r["opening_stock"]) else None for _, r in stock_sum.iterrows()}

        imp = df[df["txn_group"] == "IMPORT"].groupby("day", as_index=False)["qty"].sum(numeric_only=True)
        exp = df[df["txn_group"] == "EXPORT"].groupby("day", as_index=False)["qty"].sum(numeric_only=True)

        import_rows = int((df["txn_group"] == "IMPORT").sum())
        export_rows = int((df["txn_group"] == "EXPORT").sum())

        imp_by_day = {str(r["day"]): float(r["qty"]) if pd.notna(r["qty"]) else None for _, r in imp.iterrows()}
        exp_by_day = {str(r["day"]): float(r["qty"]) if pd.notna(r["qty"]) else None for _, r in exp.iterrows()}

        def _to_int_or_none(v: float | None) -> int | None:
            if v is None:
                return None
            try:
                return int(round(float(v)))
            except Exception:
                return None

        return WarehouseForecastResult(
            days=days_iso,
            total_opening_stock=[stock_by_day.get(di) for di in days_iso],
            total_import_qty=[_to_int_or_none(imp_by_day.get(di, 0.0)) for di in days_iso],
            total_export_qty=[_to_int_or_none(exp_by_day.get(di, 0.0)) for di in days_iso],
            meta={
                "model": cls.MODEL_NAME,
                "days_source": "plan_days",
                "missing_transaction_policy": "skip_when_unknown",
                "input_rows": int(len(X)),
                "feature_groups": int(len(raw)),
                "import_rows": import_rows,
                "export_rows": export_rows,
                "label_opening_stock": cls.LABEL_STOCK,
                "label_quantity": cls.LABEL_QTY,
            },
        )

    @classmethod
    def forecast_daily_transaction_qty_from_calendar(cls, *, calendar: Dict[str, Any]) -> WarehouseForecastResult:
        if not isinstance(calendar, dict):
            raise HTTPException(status_code=400, detail="calendar phải là object")
        return cls._predict_from_calendar(calendar)
