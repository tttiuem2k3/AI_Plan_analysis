from __future__ import annotations

import contextlib
import io
import logging
import os
import warnings
from dataclasses import dataclass
from typing import Any, Dict, List


@contextlib.contextmanager
def _suppress_autogluon_noise():
    """Suppress noisy stdout/stderr + warnings from AutoGluon model load.

    AutoGluon sometimes prints environment metadata mismatch warnings during
    `TabularPredictor.load(...)` (e.g., trained on Linux/Python 3.12, served on
    Windows/Python 3.11). These messages are expected in our demo deployment and
    should not be surfaced to end users.
    """

    buf_out, buf_err = io.StringIO(), io.StringIO()

    logger_names = (
        "autogluon",
        "autogluon.common",
        "autogluon.core",
        "autogluon.features",
        "autogluon.tabular",
    )
    prev_levels = {}
    for name in logger_names:
        try:
            lg = logging.getLogger(name)
            prev_levels[name] = lg.level
            lg.setLevel(logging.ERROR)
        except Exception:
            pass

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                yield
    finally:
        for name, lvl in prev_levels.items():
            try:
                logging.getLogger(name).setLevel(lvl)
            except Exception:
                pass


@dataclass
class MultilabelPredictor:
    """Thin loader/wrapper compatible with the saved AutoGluon multilabel bundle.

    The model is expected to be saved as `multilabel_predictor.pkl` which contains:
      - labels: List[str]
      - predictors: Dict[label, (TabularPredictor | str path)]

    We intentionally keep this wrapper minimal so it can be reused for other similar models.
    """

    # Filename used by the training script.
    multi_predictor_file: str = "multilabel_predictor.pkl"

    # Loaded attributes (populated when loading the pickle)
    labels: List[str] | None = None
    predictors: Dict[str, Any] | None = None

    # Directory which contains `multilabel_predictor.pkl` and `Predictor_*` folders.
    # This is not guaranteed to be present in the pickled object (depends on training code).
    model_dir: str | None = None

    @staticmethod
    def _rebase_predictor_path(predictor_path: str, model_dir: str) -> str:
        """Rebase predictor path saved during training to the local model directory.

        Some training scripts pickle absolute paths (e.g. `D:\\kaggle\\working\\...`).
        In production we only have the folder under `model_dir`.
        """

        if not predictor_path:
            return predictor_path

        # If it's already valid on disk, keep it.
        if os.path.exists(predictor_path):
            return predictor_path

        # Otherwise, try to load from the local model folder.
        folder_name = os.path.basename(os.path.normpath(predictor_path))
        rebased = os.path.join(model_dir, folder_name)
        return rebased

    @classmethod
    def load(cls, path: str):
        # Lazy import to keep API responsive when AutoGluon isn't installed.
        from autogluon.core.utils.loaders import load_pkl  # type: ignore

        # Compatibility: the saved pickle was created from a training script where
        # MultilabelPredictor lived in the __main__ module.
        # When loading in the backend, ensure __main__.MultilabelPredictor exists.
        try:
            import __main__  # type: ignore

            setattr(__main__, "MultilabelPredictor", cls)
        except Exception:
            pass

        model_dir = os.path.abspath(path)
        obj = load_pkl.load(path=os.path.join(model_dir, cls.multi_predictor_file))

        # Attach model_dir for future loads and rebase any predictor paths.
        try:
            setattr(obj, "model_dir", model_dir)
        except Exception:
            pass

        predictors = getattr(obj, "predictors", None)
        if isinstance(predictors, dict):
            rebased_predictors: Dict[str, Any] = {}
            for label, predictor in predictors.items():
                if isinstance(predictor, str):
                    rebased_predictors[label] = cls._rebase_predictor_path(
                        predictor_path=predictor,
                        model_dir=model_dir,
                    )
                else:
                    rebased_predictors[label] = predictor
            try:
                setattr(obj, "predictors", rebased_predictors)
            except Exception:
                pass

        return obj

    def get_predictor(self, label: str):
        from autogluon.tabular import TabularPredictor  # type: ignore

        predictor = (self.predictors or {}).get(label)
        if isinstance(predictor, str):
            model_dir = self.model_dir
            if model_dir:
                predictor = self._rebase_predictor_path(predictor_path=predictor, model_dir=model_dir)
            # The saved bundle may have been trained with a newer AutoGluon / Python version.
            # Prefer continuing with best-effort loading rather than hard-failing.
            with _suppress_autogluon_noise():
                loaded = TabularPredictor.load(
                    predictor,
                    require_version_match=False,
                    require_py_version_match=False,
                )

            # Cache the loaded predictor to avoid repeated disk loads + repeated warnings.
            try:
                if isinstance(self.predictors, dict):
                    self.predictors[label] = loaded
            except Exception:
                pass
            return loaded
        return predictor

    def predict(self, data, **kwargs):
        """Sequentially predict labels to preserve autoregressive ordering."""

        import pandas as pd
        from autogluon.tabular import TabularDataset  # type: ignore

        if isinstance(data, str):
            data = TabularDataset(data)
        df = data.copy() if hasattr(data, "copy") else pd.DataFrame(data)

        for label in (self.labels or []):
            predictor = self.get_predictor(label)
            df[label] = predictor.predict(df, **kwargs)

        return df[list(self.labels or [])]
