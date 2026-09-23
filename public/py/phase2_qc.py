"""Phase-2 configurable quality-control layer for standardized flux projects.

This layer is intentionally conservative. It never reconstructs rejected input
records and never silently clips values. It operates on the standardized data
produced by Phase 1, preserves the pre-QC value in <var>_pre_phase2_qc, and
writes explicit per-record reason flags plus a compact audit table.
"""
from __future__ import annotations
from typing import Dict, Mapping, Tuple
import numpy as np
import pandas as pd

DEFAULT_RANGES: Dict[str, Tuple[float, float]] = {
    "LE": (-1000.0, 1500.0),
    "H": (-1000.0, 1500.0),
    "NEE": (-200.0, 200.0),
    "sr": (-20.0, 1500.0),
    "rn": (-600.0, 1200.0),
    "g": (-500.0, 800.0),
    "at": (-60.0, 65.0),
    "rh": (0.0, 105.0),
    "vpd": (-0.05, 12.0),
    "ws": (0.0, 75.0),
    "pa": (50.0, 110.0),
    "swc": (0.0, 0.85),
    "soil_temperature_representative": (-30.0, 80.0),
    "ustar": (0.0, 10.0),
}


def _rolling_hampel(values: pd.Series, window_steps: int, sigma: float,
                    min_abs_deviation: float) -> pd.Series:
    """Return a conservative Hampel-style spike mask.

    The local scale uses rolling MAD converted to a Gaussian-equivalent sigma.
    A minimum absolute-deviation guard prevents near-constant nighttime periods
    from being over-flagged when MAD approaches zero.
    """
    y = pd.to_numeric(values, errors="coerce")
    if window_steps < 5 or y.notna().sum() < max(20, window_steps // 2):
        return pd.Series(False, index=y.index)
    min_periods = max(5, int(round(window_steps * 0.25)))
    med = y.rolling(window_steps, center=True, min_periods=min_periods).median()
    abs_dev = (y - med).abs()
    mad = abs_dev.rolling(window_steps, center=True, min_periods=min_periods).median()
    robust_sigma = 1.4826 * mad
    threshold = np.maximum(sigma * robust_sigma.to_numpy(dtype=float), float(min_abs_deviation))
    flag = y.notna().to_numpy() & med.notna().to_numpy() & (abs_dev.to_numpy(dtype=float) > threshold)
    return pd.Series(flag, index=y.index)


def _infer_step_minutes(data: pd.DataFrame) -> float:
    dt = pd.to_datetime(data["datetime"], errors="coerce")
    dif = dt.diff().dropna().dt.total_seconds() / 60.0
    dif = dif[dif > 0]
    return float(dif.median()) if len(dif) else 30.0


def apply_phase2_qc(data: pd.DataFrame, config: Mapping | None = None):
    """Apply configurable post-import QC and return (screened, audit, summary).

    Config keys
    -----------
    variables: list of canonical variables to screen.
    ranges: mapping var -> [min,max]. Missing entries use DEFAULT_RANGES.
    spike_enabled: bool, default True for fluxes only.
    spike_variables: list, default [LE,H,NEE].
    spike_window_hours: float, default 48 h.
    spike_sigma: float, default 7.0.
    spike_min_abs: mapping of variable-specific absolute guards.
    """
    config = dict(config or {})
    out = data.copy()
    variables = list(config.get("variables") or [
        "LE", "H", "NEE", "sr", "rn", "g", "at", "rh", "vpd", "ws", "pa", "swc",
        "soil_temperature_representative", "ustar"
    ])
    ranges = {**DEFAULT_RANGES}
    for k, v in dict(config.get("ranges") or {}).items():
        try:
            ranges[str(k)] = (float(v[0]), float(v[1]))
        except Exception:
            pass
    spike_enabled = bool(config.get("spike_enabled", True))
    spike_variables = set(config.get("spike_variables") or ["LE", "H", "NEE"])
    spike_window_hours = float(config.get("spike_window_hours", 48.0))
    spike_sigma = float(config.get("spike_sigma", 7.0))
    guards = {"LE": 75.0, "H": 75.0, "NEE": 8.0}
    guards.update({str(k): float(v) for k, v in dict(config.get("spike_min_abs") or {}).items()})
    step_min = max(_infer_step_minutes(out), 1e-6)
    window_steps = max(5, int(round(spike_window_hours * 60.0 / step_min)))
    # odd window is easier to interpret around the center
    if window_steps % 2 == 0:
        window_steps += 1

    audit = []
    total_range = total_spike = 0
    for var in variables:
        if var not in out.columns:
            continue
        current = pd.to_numeric(out[var], errors="coerce")
        pre = current.copy()
        pre_col = f"{var}_pre_phase2_qc"
        if pre_col not in out:
            out[pre_col] = pre

        lo, hi = ranges.get(var, (-np.inf, np.inf))
        range_flag = pre.notna() & ~pre.between(lo, hi, inclusive="both")
        after_range = pre.mask(range_flag)
        spike_flag = pd.Series(False, index=out.index)
        if spike_enabled and var in spike_variables:
            spike_flag = _rolling_hampel(after_range, window_steps, spike_sigma, guards.get(var, 0.0))
        final = after_range.mask(spike_flag)

        reason = pd.Series("accepted", index=out.index, dtype=object)
        reason.loc[pre.isna()] = "already_missing_or_rejected_upstream"
        reason.loc[range_flag] = "phase2_range_reject"
        reason.loc[spike_flag] = "phase2_spike_reject"
        out[f"{var}_phase2_qc_flag"] = reason
        out[var] = final

        n_range = int(range_flag.sum()); n_spike = int(spike_flag.sum())
        total_range += n_range; total_spike += n_spike
        audit.append({
            "variable": var,
            "range_min": None if not np.isfinite(lo) else float(lo),
            "range_max": None if not np.isfinite(hi) else float(hi),
            "n_before": int(pre.notna().sum()),
            "n_range_rejected": n_range,
            "n_spike_rejected": n_spike,
            "n_after": int(final.notna().sum()),
            "missing_pct_after": float(100.0 * final.isna().mean()),
            "spike_screened": bool(spike_enabled and var in spike_variables),
            "spike_window_hours": spike_window_hours if spike_enabled and var in spike_variables else None,
            "spike_sigma": spike_sigma if spike_enabled and var in spike_variables else None,
        })

    audit_df = pd.DataFrame(audit)
    summary = {
        "step_minutes": step_min,
        "spike_enabled": spike_enabled,
        "spike_window_hours": spike_window_hours,
        "spike_sigma": spike_sigma,
        "range_rejections": int(total_range),
        "spike_rejections": int(total_spike),
        "variables_screened": int(len(audit_df)),
    }
    return out, audit_df, summary
