from __future__ import annotations

import contextlib
import io
import logging
import warnings


@contextlib.contextmanager
def suppress_autogluon_warnings():
    """Suppress noisy AutoGluon warnings printed during model loading.

    This silences:
    - logger warnings from autogluon namespaces
    - stdout/stderr print blocks (version/python/system mismatch)
    - Python warnings emitted while loading predictors
    """

    buf_out, buf_err = io.StringIO(), io.StringIO()

    logger_names = (
        "autogluon",
        "autogluon.common",
        "autogluon.core",
        "autogluon.features",
        "autogluon.tabular",
    )
    prev_levels: dict[str, int] = {}

    for name in logger_names:
        try:
            lg = logging.getLogger(name)
            prev_levels[name] = lg.level
            lg.setLevel(logging.ERROR)
        except Exception:
            continue

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                yield
    finally:
        for name, level in prev_levels.items():
            try:
                logging.getLogger(name).setLevel(level)
            except Exception:
                continue
