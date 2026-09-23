"""Fast deterministic robust calibration helpers for tower–CIMIS relationships."""
from __future__ import annotations

import numpy as np


def _finite_xy(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    return x[valid], y[valid]


def robust_linear_parameters(x, y, max_points: int = 20000, iterations: int = 4) -> tuple[float, float]:
    """Return slope/intercept using deterministic MAD-clipped least squares.

    Evenly spaced subsampling limits runtime for multi-year half-hourly series,
    while all records remain represented across the full calibration period.
    """
    x, y = _finite_xy(x, y)
    if x.size < 2:
        raise ValueError("At least two finite calibration pairs are required.")
    if x.size > max_points:
        take = np.linspace(0, x.size - 1, max_points, dtype=int)
        x, y = x[take], y[take]
    keep = np.ones(x.size, dtype=bool)
    slope, intercept = 1.0, 0.0
    for _ in range(max(1, iterations)):
        if keep.sum() < 2:
            break
        design = np.column_stack([x[keep], np.ones(keep.sum())])
        slope, intercept = np.linalg.lstsq(design, y[keep], rcond=None)[0]
        residual = y - (slope * x + intercept)
        center = np.nanmedian(residual[keep])
        mad = np.nanmedian(np.abs(residual[keep] - center))
        scale = 1.4826 * mad
        if not np.isfinite(scale) or scale <= 1e-12:
            break
        new_keep = np.abs(residual - center) <= 4.5 * scale
        if new_keep.sum() < 2 or np.array_equal(new_keep, keep):
            break
        keep = new_keep
    return float(slope), float(intercept)


def robust_origin_slope(x, y, max_points: int = 20000, iterations: int = 4) -> float:
    """Return a non-negative robust slope constrained through the origin."""
    x, y = _finite_xy(x, y)
    valid = np.abs(x) > 1e-9
    x, y = x[valid], y[valid]
    if x.size < 2:
        raise ValueError("At least two non-zero calibration pairs are required.")
    if x.size > max_points:
        take = np.linspace(0, x.size - 1, max_points, dtype=int)
        x, y = x[take], y[take]
    keep = np.ones(x.size, dtype=bool)
    slope = 1.0
    for _ in range(max(1, iterations)):
        denominator = float(np.sum(x[keep] ** 2))
        if denominator <= 0:
            break
        slope = float(np.sum(x[keep] * y[keep]) / denominator)
        residual = y - slope * x
        center = np.nanmedian(residual[keep])
        mad = np.nanmedian(np.abs(residual[keep] - center))
        scale = 1.4826 * mad
        if not np.isfinite(scale) or scale <= 1e-12:
            break
        new_keep = np.abs(residual - center) <= 4.5 * scale
        if new_keep.sum() < 2 or np.array_equal(new_keep, keep):
            break
        keep = new_keep
    return max(float(slope), 0.0)


def regression_metrics(observed, predicted):
    """Return MAE, RMSE, bias, and coefficient of determination using NumPy."""
    y = np.asarray(observed, dtype=float)
    p = np.asarray(predicted, dtype=float)
    valid = np.isfinite(y) & np.isfinite(p)
    y = y[valid]
    p = p[valid]
    if y.size == 0:
        return {"mae": np.nan, "rmse": np.nan, "bias": np.nan, "r2": np.nan}
    residual = p - y
    mae = float(np.mean(np.abs(residual)))
    rmse = float(np.sqrt(np.mean(residual ** 2)))
    bias = float(np.mean(residual))
    denominator = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1.0 - np.sum((y - p) ** 2) / denominator) if y.size > 1 and denominator > 0 else np.nan
    return {"mae": mae, "rmse": rmse, "bias": bias, "r2": r2}
