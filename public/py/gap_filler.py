"""Marginal Distribution Sampling (MDS) gap filling.

The ordered LUT / mean-diurnal-course hierarchy follows the approach described
by Reichstein et al. (2005) and implemented in REddyProc (Wutzler et al., 2018).
Only original, QC-accepted target observations are donors. Filled values are
never recycled as donors. Contiguous target gaps longer than the configured
limit are left entirely unfilled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import time

from data_processor import infer_timestep
from calibration_utils import regression_metrics

DEFAULT_TOLERANCES = {"sr": 50.0, "vpd": 0.5, "at": 2.5}
ProgressCallback = Optional[Callable[[float, str], None]]


@dataclass(frozen=True)
class FillResult:
    value: float
    method: str
    qc: int
    n_donors: int
    uncertainty: float
    window_days: float


def gap_run_information(mask: pd.Series, step: pd.Timedelta) -> pd.DataFrame:
    """Return per-row contiguous-gap ID, length (steps), and length (days)."""
    missing = pd.Series(mask, copy=False).fillna(False).astype(bool).reset_index(drop=True).to_numpy()
    n = missing.size
    if n == 0:
        return pd.DataFrame({"gap_id": [], "gap_length_steps": [], "gap_length_days": []})
    # Contiguous-run labels: a boundary starts wherever the missing flag changes.
    prev = np.empty(n, dtype=bool)
    prev[0] = False
    prev[1:] = missing[:-1]
    boundary = missing != prev
    group = np.cumsum(boundary)
    # Run length for every row; only meaningful (kept) for missing rows.
    counts = np.bincount(group)
    lengths = np.where(missing, counts[group], 0).astype(int)
    # Gap IDs 1..k in order of appearance. Group labels increase with position,
    # so a sorted-unique dense rank reproduces the original sequential numbering.
    ids = np.zeros(n, dtype=int)
    if missing.any():
        miss_groups = group[missing]
        _, inverse = np.unique(miss_groups, return_inverse=True)
        ids[missing] = inverse.astype(int) + 1
    days = lengths.astype(float) * float(step / pd.Timedelta(days=1))
    return pd.DataFrame({"gap_id": ids, "gap_length_steps": lengths, "gap_length_days": days})


def _driver_column(df: pd.DataFrame, name: str) -> Optional[str]:
    for candidate in (f"{name}_filled", f"{name}_final", name):
        if candidate in df.columns and pd.to_numeric(df[candidate], errors="coerce").notna().any():
            return candidate
    return None


def _driver_qc(df: pd.DataFrame, name: str, idx: int) -> int:
    for candidate in (f"{name}_fill_qc", f"{name}_qc"):
        if candidate in df.columns and pd.notna(df.at[idx, candidate]):
            return int(df.at[idx, candidate])
    column = _driver_column(df, name)
    return 0 if column and pd.notna(df.at[idx, column]) else 9


def _time_bounds(times_ns: np.ndarray, center_ns: int, days: float) -> Tuple[int, int]:
    width = pd.Timedelta(days=float(days)).value
    return (
        int(np.searchsorted(times_ns, center_ns - width, side="left")),
        int(np.searchsorted(times_ns, center_ns + width, side="right")),
    )


def _lut_result(
    original: np.ndarray,
    measured: np.ndarray,
    times_ns: np.ndarray,
    drivers: Mapping[str, np.ndarray],
    idx: int,
    half_window_days: int,
    driver_names: Sequence[str],
    tolerances: Mapping[str, float],
    driver_quality: int,
) -> Optional[FillResult]:
    left, right = _time_bounds(times_ns, int(times_ns[idx]), half_window_days)
    if right - left < 2:
        return None
    local_measured = measured[left:right].copy()
    for name in driver_names:
        arr = drivers.get(name)
        if arr is None or not np.isfinite(arr[idx]):
            return None
        tolerance = float(tolerances[name])
        if name == "sr":
            # REddyProc radiation tolerance is bounded between 20 and 50 W m-2
            # and decreases near zero radiation.
            tolerance = max(min(tolerance, max(float(arr[idx]), 0.0)), 20.0)
        local = arr[left:right]
        local_measured &= np.isfinite(local) & (np.abs(local - arr[idx]) < tolerance)
    donors = original[left:right][local_measured]
    donors = donors[np.isfinite(donors)]
    if donors.size < 2:
        return None
    full_window = float(2 * half_window_days)
    if len(driver_names) == 3:
        method = "MDS_LUT_SWIN_VPD_TA"
        qc = 1 if full_window <= 14 else 2 if full_window <= 56 else 3
    else:
        method = "MDS_LUT_SWIN"
        qc = 1 if full_window <= 14 else 2 if full_window <= 28 else 3
    qc = max(qc, min(int(driver_quality), 3))
    return FillResult(
        value=float(np.mean(donors)),
        method=method,
        qc=qc,
        n_donors=int(donors.size),
        uncertainty=float(np.std(donors, ddof=1)),
        window_days=full_window,
    )


def _mdc_result(
    original: np.ndarray,
    measured: np.ndarray,
    times_ns: np.ndarray,
    slots: np.ndarray,
    idx: int,
    slots_per_day: int,
    half_window_days: int,
    tolerance_slots: int,
) -> Optional[FillResult]:
    left, right = _time_bounds(times_ns, int(times_ns[idx]), max(0.5, half_window_days))
    local_slots = slots[left:right]
    distance = np.abs(local_slots - slots[idx])
    distance = np.minimum(distance, slots_per_day - distance)
    donor_mask = measured[left:right] & (distance <= tolerance_slots)
    donors = original[left:right][donor_mask]
    donors = donors[np.isfinite(donors)]
    if donors.size < 2:
        return None
    full_window = float(2 * half_window_days + 1)
    qc = 1 if full_window <= 1 else 2 if full_window <= 5 else 3
    return FillResult(
        value=float(np.mean(donors)),
        method="MDS_MDC",
        qc=qc,
        n_donors=int(donors.size),
        uncertainty=float(np.std(donors, ddof=1)),
        window_days=full_window,
    )


def mds_gap_fill(
    data: pd.DataFrame,
    target: str,
    predictors: Sequence[str] = ("sr", "vpd", "at"),
    tolerances: Optional[Mapping[str, float]] = None,
    max_gap_days: float = 90.0,
    progress_callback: ProgressCallback = None,
) -> pd.DataFrame:
    """Fill eligible missing target records using MDS only.

    A contiguous gap is eligible when its total duration is less than or equal
    to ``max_gap_days``. If a run exceeds the limit, no part of that run is
    filled. The target donor pool is frozen before filling.
    """
    if target not in data.columns:
        raise ValueError(f"Target {target!r} is absent.")
    if max_gap_days <= 0:
        raise ValueError("max_gap_days must be positive.")

    # Work on a narrow frame. Returning/copying the full multi-year provenance
    # table for every target causes severe memory pressure after earlier target
    # columns have been added.
    source = data
    keep = ["datetime", target]
    for name in ("sr", "vpd", "at"):
        for candidate in (f"{name}_filled", f"{name}_final", name, f"{name}_fill_qc", f"{name}_qc"):
            if candidate in source.columns and candidate not in keep:
                keep.append(candidate)
    df = source.loc[:, keep].reset_index(drop=True).copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    if df["datetime"].isna().any() or not df["datetime"].is_monotonic_increasing:
        raise ValueError("MDS requires valid, increasing timestamps.")
    step = infer_timestep(df["datetime"])
    original_series = pd.to_numeric(df[target], errors="coerce")
    original = original_series.to_numpy(dtype=float)
    measured = np.isfinite(original)
    missing = ~measured
    gap_info = gap_run_information(pd.Series(missing), step)
    gap_days_arr = gap_info["gap_length_days"].to_numpy(dtype=float)

    tolerance = dict(DEFAULT_TOLERANCES)
    if tolerances:
        tolerance.update({k: float(v) for k, v in tolerances.items()})
    requested = [name for name in ("sr", "vpd", "at") if name in predictors]
    drivers: Dict[str, np.ndarray] = {}
    finite_masks: Dict[str, np.ndarray] = {}
    for name in requested:
        col = _driver_column(df, name)
        if col:
            arr = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
            drivers[name] = arr
            finite_masks[name] = np.isfinite(arr)

    n = len(df)

    # Vectorised equivalent of _driver_qc, evaluated once per driver instead of
    # per missing row (this scalar pandas access was the dominant cost before).
    def _driver_qc_array(name: str) -> np.ndarray:
        col = _driver_column(df, name)
        if col:
            col_finite = np.isfinite(pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float))
        else:
            col_finite = np.zeros(n, dtype=bool)
        res = np.where(col_finite, 0, 9).astype(np.int64)
        # Lower priority first, higher priority overrides (fill_qc beats qc).
        for candidate in (f"{name}_qc", f"{name}_fill_qc"):
            if candidate in df.columns:
                vals = pd.to_numeric(df[candidate], errors="coerce").to_numpy(dtype=float)
                m = np.isfinite(vals)
                res = np.where(m, vals.astype(np.int64, copy=False), res)
        return res.astype(np.int64)

    drv_qc: Dict[str, np.ndarray] = {name: _driver_qc_array(name) for name in drivers}
    if drivers:
        driver_quality_arr = np.maximum.reduce([drv_qc[name] for name in drivers])
    else:
        driver_quality_arr = np.zeros(n, dtype=np.int64)

    empty_bool = np.zeros(n, dtype=bool)
    fin_sr = finite_masks.get("sr", empty_bool)
    fin_vpd = finite_masks.get("vpd", empty_bool)
    fin_at = finite_masks.get("at", empty_bool)
    all_avail_arr = fin_sr & fin_vpd & fin_at
    sr_in = "sr" in drivers
    sr_qc_arr = drv_qc.get("sr")

    times_ns = df["datetime"].to_numpy(dtype="datetime64[ns]").astype("int64", copy=False)
    slots_per_day = max(1, int(round(pd.Timedelta(days=1) / step)))
    slots = np.arange(n, dtype=np.int64) % slots_per_day
    tolerance_slots = max(1, int(round(pd.Timedelta(hours=1) / step)))
    day_ns = pd.Timedelta(days=1).value

    # Output buffers held as NumPy arrays; assembled into a frame once at the end.
    filled = original.copy()
    method = np.where(measured, "measured", "unfilled").astype(object)
    qc = np.where(measured, 0, 9).astype("int8")
    n_donors = np.where(measured, 1, 0).astype(np.int64)
    uncertainty = np.full(n, np.nan)
    window = np.full(n, np.nan)

    missing_indices = np.flatnonzero(missing)
    over_limit = missing & (gap_days_arr > float(max_gap_days) + 1e-12)
    if over_limit.any():
        method[over_limit] = "unfilled_gap_over_limit"
    candidate_indices = np.flatnonzero(missing & ~over_limit)

    last_report_time = 0.0

    def _report(processed: int, total: int, force: bool = False) -> None:
        nonlocal last_report_time
        if not progress_callback:
            return
        now = time.monotonic()
        if not force and processed < total and (now - last_report_time) < 1.5:
            return
        last_report_time = now
        pct = 100.0 if total <= 0 else 100.0 * min(max(processed, 0), total) / total
        progress_callback(pct, f"Applying MDS to {target}: {processed:,}/{total:,} eligible missing records")

    _report(0, int(candidate_indices.size), force=True)

    # If there are fewer than two original donors, no MDS method can produce a
    # valid mean. Return immediately instead of scanning every missing row for
    # minutes and leaving the browser stuck at one progress stage.
    if int(measured.sum()) < 2:
        no_donor = missing & ~over_limit
        if no_donor.any():
            method[no_donor] = "unfilled_no_mds_donors"
        _report(int(candidate_indices.size), int(candidate_indices.size), force=True)
        additions = pd.DataFrame({
            f"{target}_original": original,
            f"{target}_filled": filled,
            f"{target}_fill_method": method,
            f"{target}_fill_qc": qc,
            f"{target}_fill_n": n_donors,
            f"{target}_fill_uncertainty": uncertainty,
            f"{target}_fill_lower95": filled - 1.96 * uncertainty,
            f"{target}_fill_upper95": filled + 1.96 * uncertainty,
            f"{target}_fill_window_days": window,
            f"{target}_gap_id": gap_info["gap_id"].to_numpy(),
            f"{target}_gap_length_steps": gap_info["gap_length_steps"].to_numpy(),
            f"{target}_gap_length_days": gap_info["gap_length_days"].to_numpy(),
            f"{target}_source": np.where(np.isfinite(original), "measured",
                                         np.where(np.isfinite(filled), "MDS_gapfilled", "unfilled")),
        })
        return pd.concat([df.reset_index(drop=True), additions], axis=1).copy()

    def _bounds(center_ns: int, days: float) -> Tuple[int, int]:
        width = int(day_ns * days)
        return (
            int(np.searchsorted(times_ns, center_ns - width, side="left")),
            int(np.searchsorted(times_ns, center_ns + width, side="right")),
        )

    def _lut(idx: int, half_window: float, names: Sequence[str], dq: int):
        left, right = _bounds(int(times_ns[idx]), half_window)
        if right - left < 2:
            return None
        sl = slice(left, right)
        local_measured = None
        for name in names:
            arr = drivers.get(name)
            if arr is None:
                return None
            ai = arr[idx]
            if not np.isfinite(ai):
                return None
            tol = tolerance[name]
            if name == "sr":
                # REddyProc radiation tolerance: bounded 20..50 W m-2, shrinking near zero.
                tol = max(min(tol, ai if ai > 0.0 else 0.0), 20.0)
            cond = finite_masks[name][sl] & (np.abs(arr[sl] - ai) < tol)
            local_measured = cond if local_measured is None else (local_measured & cond)
        local_measured &= measured[sl]
        donors = original[sl][local_measured]
        if donors.size < 2:
            return None
        full_window = float(2 * half_window)
        if len(names) == 3:
            meth = "MDS_LUT_SWIN_VPD_TA"
            q = 1 if full_window <= 14 else 2 if full_window <= 56 else 3
        else:
            meth = "MDS_LUT_SWIN"
            q = 1 if full_window <= 14 else 2 if full_window <= 28 else 3
        q = max(q, min(int(dq), 3))
        return (float(np.mean(donors)), meth, q, int(donors.size),
                float(np.std(donors, ddof=1)), full_window)

    def _mdc(idx: int, half_window: float):
        left, right = _bounds(int(times_ns[idx]), max(0.5, half_window))
        sl = slice(left, right)
        distance = np.abs(slots[sl] - slots[idx])
        distance = np.minimum(distance, slots_per_day - distance)
        donor_mask = measured[sl] & (distance <= tolerance_slots)
        donors = original[sl][donor_mask]
        if donors.size < 2:
            return None
        full_window = float(2 * half_window + 1)
        q = 1 if full_window <= 1 else 2 if full_window <= 5 else 3
        return (float(np.mean(donors)), "MDS_MDC", q, int(donors.size),
                float(np.std(donors, ddof=1)), full_window)

    total_candidates = int(candidate_indices.size)
    for processed_count, idx in enumerate(candidate_indices, start=1):
        gap_days = gap_days_arr[idx]
        _report(processed_count - 1, total_candidates)

        all_available = bool(all_avail_arr[idx])
        sr_available = sr_in and bool(fin_sr[idx])
        driver_quality = int(driver_quality_arr[idx])
        result = None

        # Ordered hierarchy used by REddyProc-style MDS.
        if all_available:
            for half_window in (7, 14):
                result = _lut(idx, half_window, ("sr", "vpd", "at"), driver_quality)
                if result:
                    break
        if result is None and sr_available:
            result = _lut(idx, 7, ("sr",), int(sr_qc_arr[idx]))
        if result is None:
            for half_window in (0, 1, 2):
                result = _mdc(idx, half_window)
                if result:
                    break
        if result is None and all_available:
            for half_window in range(21, 71, 7):
                result = _lut(idx, half_window, ("sr", "vpd", "at"), driver_quality)
                if result:
                    break
        if result is None and sr_available:
            for half_window in range(14, 71, 7):
                result = _lut(idx, half_window, ("sr",), int(sr_qc_arr[idx]))
                if result:
                    break
        if result is None:
            for half_window in range(7, 211, 7):
                result = _mdc(idx, half_window)
                if result:
                    break

        if result is None:
            method[idx] = "unfilled_no_mds_donors"
            _report(processed_count, total_candidates)
            continue

        value, meth, q, ndon, unc, win = result
        gap_qc = 1 if gap_days <= 1 else 2 if gap_days <= 14 else 3
        filled[idx] = value
        method[idx] = meth
        qc[idx] = max(q, gap_qc)
        n_donors[idx] = ndon
        uncertainty[idx] = unc
        window[idx] = win
        _report(processed_count, total_candidates)

    _report(total_candidates, total_candidates, force=True)

    additions = pd.DataFrame({
        f"{target}_original": original,
        f"{target}_filled": filled,
        f"{target}_fill_method": method,
        f"{target}_fill_qc": qc,
        f"{target}_fill_n": n_donors,
        f"{target}_fill_uncertainty": uncertainty,
        f"{target}_fill_lower95": filled - 1.96 * uncertainty,
        f"{target}_fill_upper95": filled + 1.96 * uncertainty,
        f"{target}_fill_window_days": window,
        f"{target}_gap_id": gap_info["gap_id"].to_numpy(),
        f"{target}_gap_length_steps": gap_info["gap_length_steps"].to_numpy(),
        f"{target}_gap_length_days": gap_info["gap_length_days"].to_numpy(),
        f"{target}_source": np.where(np.isfinite(original), "measured",
                                     np.where(np.isfinite(filled), "MDS_gapfilled", "unfilled")),
    })
    return pd.concat([df.reset_index(drop=True), additions], axis=1).copy()



RF_DEFAULT_PREDICTORS = ("sr", "vpd", "at", "rh", "ws", "rain", "pa")


def _rf_feature_frame(
    df: pd.DataFrame,
    predictors: Sequence[str],
) -> Tuple[pd.DataFrame, List[str], Dict[str, str]]:
    """Build RF predictors from filled meteorology plus cyclic time features."""
    dt = pd.to_datetime(df["datetime"], errors="coerce")
    if dt.isna().any():
        raise ValueError("RF requires valid timestamps.")

    feature_data: Dict[str, pd.Series] = {}
    driver_columns: Dict[str, str] = {}
    for name in predictors:
        column = _driver_column(df, name)
        if not column:
            continue
        values = pd.to_numeric(df[column], errors="coerce")
        if values.notna().sum() < 2 or values.nunique(dropna=True) < 2:
            continue
        feature_data[name] = values
        driver_columns[name] = column

    # Cyclic features let the model represent diurnal and seasonal behavior
    # without imposing discontinuities at midnight or New Year.
    hour = dt.dt.hour + dt.dt.minute / 60.0 + dt.dt.second / 3600.0
    doy = dt.dt.dayofyear + hour / 24.0
    feature_data["hour_sin"] = pd.Series(np.sin(2.0 * np.pi * hour / 24.0), index=df.index)
    feature_data["hour_cos"] = pd.Series(np.cos(2.0 * np.pi * hour / 24.0), index=df.index)
    feature_data["doy_sin"] = pd.Series(np.sin(2.0 * np.pi * doy / 365.25), index=df.index)
    feature_data["doy_cos"] = pd.Series(np.cos(2.0 * np.pi * doy / 365.25), index=df.index)

    frame = pd.DataFrame(feature_data, index=df.index)
    return frame, list(frame.columns), driver_columns


def rf_gap_fill(
    data: pd.DataFrame,
    target: str,
    predictors: Sequence[str] = RF_DEFAULT_PREDICTORS,
    max_gap_days: float = 90.0,
    random_state: int = 42,
    n_estimators: Optional[int] = None,
    max_depth: Optional[int] = None,
    min_samples_leaf: int = 5,
    max_train_samples: int = 60000,
    progress_callback: ProgressCallback = None,
) -> pd.DataFrame:
    """Fill eligible target gaps with Random Forest regression.

    Long gaps are predicted from continuous CIMIS/tower meteorology plus cyclic
    time features. Optional drivers that disappear inside a gap no longer block
    prediction: usable drivers are selected from the actual training/prediction
    coverage, remaining missing feature values are median-imputed from measured
    training records, and missingness indicators are added to the model.

    Safeguards:
      * only original QC-accepted target observations are training responses;
      * measured target values are never overwritten;
      * gaps longer than ``max_gap_days`` remain unfilled;
      * rows with weak/no meteorological support are still predicted but flagged
        QC=3 and labelled ``RF_limited_drivers``;
      * no generic nighttime zeroing or sign clipping is applied.
    """
    if target not in data.columns:
        raise ValueError(f"Target {target!r} is absent.")
    if max_gap_days <= 0:
        raise ValueError("max_gap_days must be positive.")

    try:
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.metrics import mean_absolute_error, mean_squared_error
    except ImportError as exc:
        raise ImportError(
            "Random Forest gap filling requires scikit-learn. Install the packages "
            "from requirements.txt and restart the web app."
        ) from exc

    source = data
    keep = ["datetime", target]
    for name in predictors:
        for candidate in (f"{name}_filled", f"{name}_final", name):
            if candidate in source.columns and candidate not in keep:
                keep.append(candidate)
    df = source.loc[:, keep].reset_index(drop=True).copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    if df["datetime"].isna().any() or not df["datetime"].is_monotonic_increasing:
        raise ValueError("RF requires valid, increasing timestamps.")

    step = infer_timestep(df["datetime"])
    original = pd.to_numeric(df[target], errors="coerce").to_numpy(dtype=float)
    measured = np.isfinite(original)
    missing = ~measured
    gap_info = gap_run_information(pd.Series(missing), step)
    gap_days = gap_info["gap_length_days"].to_numpy(dtype=float)
    over_limit = missing & (gap_days > float(max_gap_days) + 1e-12)
    eligible = missing & ~over_limit

    X_raw, all_feature_names, driver_columns = _rf_feature_frame(df, predictors)
    time_features = [name for name in ("hour_sin", "hour_cos", "doy_sin", "doy_cos") if name in X_raw]
    candidate_environmental = [name for name in all_feature_names if name in driver_columns]

    # Retain drivers that are actually represented in measured target records and
    # have at least some support in the rows requiring prediction. This drops an
    # optional tower-only driver such as pressure when it is absent for an entire
    # long outage, instead of causing every row in that outage to fail the strict
    # complete-case test used by the previous implementation.
    minimum_driver_training = max(50, min(200, int(max(1, measured.sum()) * 0.02)))
    environmental_features: List[str] = []
    dropped_features: List[str] = []
    coverage_rows = eligible if eligible.any() else missing
    for name in candidate_environmental:
        train_count = int(X_raw.loc[measured, name].notna().sum())
        prediction_count = int(X_raw.loc[coverage_rows, name].notna().sum()) if coverage_rows.any() else 0
        if train_count >= minimum_driver_training and prediction_count > 0:
            environmental_features.append(name)
        else:
            dropped_features.append(name)

    selected_features = environmental_features + time_features
    X_selected = X_raw[selected_features].copy() if selected_features else pd.DataFrame(index=df.index)

    filled = original.copy()
    method = np.where(measured, "measured", "unfilled").astype(object)
    qc = np.where(measured, 0, 9).astype("int8")
    n_support = np.where(measured, 1, 0).astype(np.int64)
    uncertainty = np.full(len(df), np.nan)
    lower95 = np.full(len(df), np.nan)
    upper95 = np.full(len(df), np.nan)
    window = np.full(len(df), np.nan)
    method[over_limit] = "unfilled_gap_over_limit"

    diagnostics: Dict[str, object] = {
        "algorithm": "RF",
        "target": target,
        "features": ", ".join(selected_features),
        "environmental_features": ", ".join(environmental_features),
        "dropped_predictors": ", ".join(dropped_features),
        "n_features_before_missing_indicators": int(len(selected_features)),
        "n_original_measured": int(measured.sum()),
        "n_eligible_missing": int(eligible.sum()),
        "n_over_gap_limit": int(over_limit.sum()),
        "n_filled": 0,
        "n_training": 0,
        "n_training_available_before_cap": int(measured.sum()),
        "oob_mae": np.nan,
        "oob_rmse": np.nan,
        "extrapolation_count": 0,
        "limited_driver_count": 0,
        "imputed_prediction_cells": 0,
    }

    def _assemble() -> pd.DataFrame:
        additions = pd.DataFrame({
            f"{target}_original": original,
            f"{target}_filled": filled,
            f"{target}_fill_method": method,
            f"{target}_fill_qc": qc,
            f"{target}_fill_n": n_support,
            f"{target}_fill_uncertainty": uncertainty,
            f"{target}_fill_lower95": lower95,
            f"{target}_fill_upper95": upper95,
            f"{target}_fill_window_days": window,
            f"{target}_gap_id": gap_info["gap_id"].to_numpy(),
            f"{target}_gap_length_steps": gap_info["gap_length_steps"].to_numpy(),
            f"{target}_gap_length_days": gap_days,
            f"{target}_source": np.where(
                measured, "measured", np.where(np.isfinite(filled), "RF_gapfilled", "unfilled")
            ),
        })
        result = pd.concat([df.reset_index(drop=True), additions], axis=1).copy()
        result.attrs["rf_diagnostics"] = diagnostics.copy()
        return result

    if len(environmental_features) < 2:
        method[eligible] = "unfilled_insufficient_rf_predictors"
        diagnostics["status"] = "insufficient_predictors"
        diagnostics["minimum_environmental_predictors_required"] = 2
        return _assemble()

    train_indices_all = np.flatnonzero(measured)
    minimum_training = max(200, 25 * len(selected_features))
    if train_indices_all.size < minimum_training:
        method[eligible] = "unfilled_insufficient_rf_training"
        diagnostics["status"] = "insufficient_training"
        diagnostics["minimum_training_required"] = int(minimum_training)
        return _assemble()

    rng = np.random.default_rng(random_state)
    if train_indices_all.size > int(max_train_samples):
        train_indices = np.sort(rng.choice(train_indices_all, size=int(max_train_samples), replace=False))
    else:
        train_indices = train_indices_all

    # Fit medians only from measured target records. Missing-indicator features
    # preserve information about which drivers were unavailable rather than
    # silently pretending the imputed values were observations.
    medians: Dict[str, float] = {}
    indicator_features: List[str] = []
    X_model = pd.DataFrame(index=df.index)
    for name in selected_features:
        values = pd.to_numeric(X_selected[name], errors="coerce")
        train_values = values.iloc[train_indices]
        median = float(train_values.median()) if train_values.notna().any() else 0.0
        medians[name] = median
        X_model[name] = values.fillna(median)
        if values.isna().any():
            indicator_name = f"{name}_missing"
            X_model[indicator_name] = values.isna().astype("int8")
            indicator_features.append(indicator_name)

    model_feature_names = list(X_model.columns)
    diagnostics["features"] = ", ".join(model_feature_names)
    diagnostics["n_features"] = int(len(model_feature_names))
    diagnostics["imputed_features"] = ", ".join(name for name in selected_features if X_selected[name].isna().any())

    X_train = X_model.iloc[train_indices]
    y_train = original[train_indices]
    n_train = int(len(train_indices))
    diagnostics["n_training"] = n_train
    diagnostics["training_start"] = str(df.loc[train_indices, "datetime"].min())
    diagnostics["training_end"] = str(df.loc[train_indices, "datetime"].max())

    if n_estimators is None:
        n_estimators = 160 if n_train < 20000 else 100 if n_train < 50000 else 80
    if max_depth is None:
        max_depth = 22 if n_train < 20000 else 18 if n_train < 50000 else 16

    model = RandomForestRegressor(
        n_estimators=int(n_estimators),
        max_depth=max_depth,
        min_samples_leaf=max(1, int(min_samples_leaf)),
        max_features=0.8,
        bootstrap=True,
        oob_score=True,
        random_state=random_state,
        n_jobs=1,
    )
    if progress_callback:
        progress_callback(5.0, f"Training RF for {target} with {n_train:,} accepted observations")
    model.fit(X_train, y_train)

    oob = np.asarray(getattr(model, "oob_prediction_", np.full(n_train, np.nan)), dtype=float)
    valid_oob = np.isfinite(oob) & np.isfinite(y_train)
    if valid_oob.any():
        diagnostics["oob_mae"] = float(mean_absolute_error(y_train[valid_oob], oob[valid_oob]))
        diagnostics["oob_rmse"] = float(np.sqrt(mean_squared_error(y_train[valid_oob], oob[valid_oob])))
    residual_rmse = float(diagnostics["oob_rmse"]) if np.isfinite(diagnostics["oob_rmse"]) else 0.0
    ranked_importance = sorted(
        zip(model_feature_names, model.feature_importances_), key=lambda item: item[1], reverse=True
    )
    diagnostics["feature_importance"] = "; ".join(
        f"{name}={importance:.4f}" for name, importance in ranked_importance
    )

    prediction_indices = np.flatnonzero(eligible)
    if prediction_indices.size == 0:
        diagnostics["status"] = "no_eligible_missing_rows"
        if progress_callback:
            progress_callback(100.0, f"RF for {target}: no eligible missing rows")
        return _assemble()

    X_domain = X_selected.iloc[train_indices_all]
    driver_low = X_domain[environmental_features].quantile(0.01)
    driver_high = X_domain[environmental_features].quantile(0.99)
    response_min = float(np.nanmin(original[train_indices_all]))
    response_max = float(np.nanmax(original[train_indices_all]))
    diagnostics.update({
        "n_estimators": int(n_estimators),
        "max_depth": int(max_depth) if max_depth is not None else None,
        "min_samples_leaf": int(min_samples_leaf),
        "response_min": response_min,
        "response_max": response_max,
    })

    batch_size = 5000
    total = int(prediction_indices.size)
    extrapolation_total = 0
    limited_driver_total = 0
    imputed_cells_total = 0
    for start in range(0, total, batch_size):
        batch_idx = prediction_indices[start:start + batch_size]
        X_batch = X_model.iloc[batch_idx]
        batch_values = X_batch.to_numpy(dtype=float, copy=False)
        tree_predictions = np.vstack([tree.predict(batch_values) for tree in model.estimators_])
        prediction = np.mean(tree_predictions, axis=0)
        prediction = np.clip(prediction, response_min, response_max)
        tree_sd = np.std(tree_predictions, axis=0, ddof=1) if len(model.estimators_) > 1 else np.zeros(len(batch_idx))
        combined_uncertainty = np.sqrt(np.square(tree_sd) + residual_rmse ** 2)

        raw_driver_batch = X_selected.iloc[batch_idx][environmental_features]
        observed_driver_count = raw_driver_batch.notna().sum(axis=1).to_numpy()
        limited = observed_driver_count < 2
        imputed_cells_total += int(raw_driver_batch.isna().sum().sum())

        outside_matrix = (
            raw_driver_batch.lt(driver_low, axis=1) | raw_driver_batch.gt(driver_high, axis=1)
        ) & raw_driver_batch.notna()
        outside = outside_matrix.any(axis=1).to_numpy()
        extrapolation_total += int(outside.sum())
        limited_driver_total += int(limited.sum())

        local_gap = gap_days[batch_idx]
        local_qc = np.where(local_gap <= 1.0, 1, np.where(local_gap <= 14.0, 2, 3)).astype(np.int8)
        local_qc[outside | limited] = 3

        labels = np.full(len(batch_idx), "RF", dtype=object)
        labels[outside] = "RF_extrapolation"
        labels[limited] = "RF_limited_drivers"

        filled[batch_idx] = prediction
        uncertainty[batch_idx] = combined_uncertainty
        lower95[batch_idx] = prediction - 1.96 * combined_uncertainty
        upper95[batch_idx] = prediction + 1.96 * combined_uncertainty
        method[batch_idx] = labels
        qc[batch_idx] = local_qc
        n_support[batch_idx] = n_train

        if progress_callback:
            done = min(start + len(batch_idx), total)
            progress_callback(10.0 + 90.0 * done / total, f"Applying RF to {target}: {done:,}/{total:,} eligible missing records")

    diagnostics["n_filled"] = total
    diagnostics["extrapolation_count"] = int(extrapolation_total)
    diagnostics["limited_driver_count"] = int(limited_driver_total)
    diagnostics["imputed_prediction_cells"] = int(imputed_cells_total)
    diagnostics["status"] = "complete"
    return _assemble()

def _metrics(truth: np.ndarray, prediction: np.ndarray) -> Dict[str, float]:
    valid = np.isfinite(truth) & np.isfinite(prediction)
    if valid.sum() == 0:
        return {"n": 0, "coverage": 0.0, "mae": np.nan, "rmse": np.nan, "bias": np.nan, "r2": np.nan}
    y = truth[valid]
    p = prediction[valid]
    metrics = regression_metrics(y, p)
    return {"n": int(valid.sum()), "coverage": float(valid.mean()), **metrics}


def _candidate_blocks(measured: np.ndarray, block_steps: int) -> np.ndarray:
    if block_steps <= 0 or block_steps > measured.size:
        return np.array([], dtype=int)
    # Convolution identifies fully measured contiguous blocks efficiently.
    counts = np.convolve(measured.astype(np.int16), np.ones(block_steps, dtype=np.int16), mode="valid")
    return np.flatnonzero(counts == block_steps)


def validate_gap_fill_blocked(
    data: pd.DataFrame,
    target: str,
    algorithm: str = "MDS",
    max_gap_days: float = 90.0,
    durations_days: Sequence[float] = (1, 7, 14, 30, 60, 90),
    blocks_per_duration: int = 1,
    repeats: int = 1,
    random_state: int = 42,
) -> pd.DataFrame:
    """Validate MDS or RF with artificial contiguous gaps.

    This blocked design is appropriate for long-gap evaluation. Durations that
    lack a fully observed block are reported as unavailable rather than being
    replaced by optimistic random-point masking.
    """
    algorithm = str(algorithm).strip().upper()
    if algorithm not in {"MDS", "RF"}:
        raise ValueError("algorithm must be 'MDS' or 'RF'.")
    if target not in data.columns:
        return pd.DataFrame()
    dt = pd.to_datetime(data["datetime"], errors="coerce")
    step = infer_timestep(dt)
    original = pd.to_numeric(data[target], errors="coerce")
    measured = original.notna().to_numpy()
    rng = np.random.default_rng(random_state)
    rows: List[dict] = []

    for duration in durations_days:
        if duration > max_gap_days + 1e-12:
            continue
        block_steps = max(1, int(round(pd.Timedelta(days=float(duration)) / step)))
        starts = _candidate_blocks(measured, block_steps)
        if starts.size == 0:
            rows.append({
                "algorithm": algorithm, "variable": target, "repeat": 0,
                "gap_days": float(duration), "block_start": pd.NaT,
                "block_end": pd.NaT, "available": False, "n": 0,
                "coverage": np.nan, "mae": np.nan, "rmse": np.nan,
                "bias": np.nan, "r2": np.nan,
            })
            continue
        for repeat in range(1, repeats + 1):
            shuffled = starts.copy()
            rng.shuffle(shuffled)
            selected: List[int] = []
            for block_start in shuffled:
                if all(abs(int(block_start) - chosen) >= block_steps for chosen in selected):
                    selected.append(int(block_start))
                if len(selected) >= blocks_per_duration:
                    break
            for block_start in selected:
                idx = np.arange(block_start, block_start + block_steps)
                block_start_time = dt.iloc[block_start]
                block_end_time = dt.iloc[block_start + block_steps - 1]
                work = data.copy()
                work.loc[idx, target] = np.nan
                if algorithm == "RF":
                    result = rf_gap_fill(
                        work, target, max_gap_days=max_gap_days,
                        random_state=random_state + repeat + int(duration * 10),
                    )
                else:
                    # MDS only needs a +/-211-day donor neighborhood and can be
                    # evaluated more cheaply on that local slice.
                    local_mask = dt.between(
                        block_start_time - pd.Timedelta(days=211),
                        block_end_time + pd.Timedelta(days=211),
                    )
                    local = work.loc[local_mask].copy().reset_index().rename(columns={"index": "_global_index"})
                    hide = local["_global_index"].isin(idx)
                    result = mds_gap_fill(local.drop(columns=["_global_index"]), target, max_gap_days=max_gap_days)
                    predicted = result.loc[hide.to_numpy(), f"{target}_filled"].to_numpy(dtype=float)
                    metrics = _metrics(original.iloc[idx].to_numpy(dtype=float), predicted)
                    rows.append({
                        "algorithm": algorithm, "variable": target, "repeat": repeat,
                        "gap_days": float(duration), "block_start": block_start_time,
                        "block_end": block_end_time, "available": True, **metrics,
                    })
                    continue
                predicted = result.iloc[idx][f"{target}_filled"].to_numpy(dtype=float)
                metrics = _metrics(original.iloc[idx].to_numpy(dtype=float), predicted)
                rows.append({
                    "algorithm": algorithm, "variable": target, "repeat": repeat,
                    "gap_days": float(duration), "block_start": block_start_time,
                    "block_end": block_end_time, "available": True, **metrics,
                })
    return pd.DataFrame(rows)


def validate_mds_blocked(data: pd.DataFrame, target: str, **kwargs) -> pd.DataFrame:
    return validate_gap_fill_blocked(data, target, algorithm="MDS", **kwargs)


def validate_rf_blocked(data: pd.DataFrame, target: str, **kwargs) -> pd.DataFrame:
    return validate_gap_fill_blocked(data, target, algorithm="RF", **kwargs)


def compare_models_blocked(data: pd.DataFrame, target: str, **kwargs) -> pd.DataFrame:
    """Evaluate MDS and RF on the same contiguous artificial gaps."""
    durations_hours = kwargs.pop("durations_hours", None)
    durations_days = tuple(float(x) / 24.0 for x in durations_hours) if durations_hours else kwargs.pop("durations_days", (1, 7, 14, 30, 60, 90))
    common = dict(
        max_gap_days=float(kwargs.pop("max_gap_days", 90.0)),
        durations_days=durations_days,
        blocks_per_duration=int(kwargs.pop("blocks_per_duration", 1)),
        repeats=int(kwargs.pop("repeats", 1)),
        random_state=int(kwargs.pop("random_state", 42)),
    )
    mds = validate_gap_fill_blocked(data, target, algorithm="MDS", **common)
    rf = validate_gap_fill_blocked(data, target, algorithm="RF", **common)
    return pd.concat([mds, rf], ignore_index=True)

