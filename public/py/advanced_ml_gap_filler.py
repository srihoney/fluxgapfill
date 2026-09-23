"""Advanced RF/XGBoost candidates for retrospective eddy-covariance gap filling.

This module is deliberately separate from the production MDS/RF implementation.
It is intended for scientific benchmarking before the web application is changed.

Design principles
-----------------
* Accepted measured target values are never overwritten.
* Candidate models use only target observations outside the artificial/real gap.
* External-only, tower-enhanced, and cross-flux profiles are explicit.
* Cross-flux predictors are optional: when the complementary flux is unavailable,
  the model falls back through missing-value indicators rather than inventing it.
* Validation should use contiguous calendar gaps, not random-point CV/OOB metrics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from data_processor import infer_timestep
from gap_filler import gap_run_information


CURRENT_RF_FEATURES = ("sr", "vpd", "at", "rh", "ws", "pa")
TOWER_FEATURES = (
    "sr", "vpd", "at", "rh", "ws", "pa", "rn", "g", "swc",
    "soil_temperature_representative", "wind_dir",
)
EXTERNAL_REF_FEATURES = (
    "eto_ref", "rain_ref", "sr_ref", "rn_ref", "vp_ref", "at_ref", "rh_ref",
    "dew_ref", "ws_ref", "wind_dir_ref", "soil_temp_ref",
)


@dataclass
class ModelBundle:
    algorithm: str
    profile: str
    target: str
    model: object
    feature_names: List[str]
    medians: Dict[str, float]
    training_rows: int
    diagnostics: Dict[str, object]


def _numeric_series(data: pd.DataFrame, name: str) -> Optional[pd.Series]:
    """Resolve a science variable from preferred filled/final/raw columns."""
    candidates = (
        f"{name}_filled", f"{name}_final", name,
        # explicit external reference fields
        f"{name}_ref",
    )
    for col in candidates:
        if col in data:
            s = pd.to_numeric(data[col], errors="coerce")
            if s.notna().sum() >= 2:
                return s
    return None


def _add_cyclic_time_features(frame: pd.DataFrame, dt: pd.Series) -> None:
    hour = dt.dt.hour + dt.dt.minute / 60.0 + dt.dt.second / 3600.0
    doy = dt.dt.dayofyear + hour / 24.0
    for harmonic in (1, 2):
        frame[f"hour_sin{harmonic}"] = np.sin(2 * np.pi * harmonic * hour / 24.0)
        frame[f"hour_cos{harmonic}"] = np.cos(2 * np.pi * harmonic * hour / 24.0)
        frame[f"doy_sin{harmonic}"] = np.sin(2 * np.pi * harmonic * doy / 365.25)
        frame[f"doy_cos{harmonic}"] = np.cos(2 * np.pi * harmonic * doy / 365.25)
    frame["trend_days"] = (dt - dt.min()).dt.total_seconds() / 86400.0


def build_advanced_features(
    data: pd.DataFrame,
    target: str,
    profile: str = "tower",
) -> pd.DataFrame:
    """Build candidate ML predictors.

    Profiles
    --------
    current:
        Reproduces the original RF predictor concept plus cyclic time.
    external:
        CIMIS/external-reference drivers + time. Appropriate when the tower is
        completely unavailable during a gap.
    tower:
        Tower drivers + external reference drivers + derived energy predictors.
    cross:
        ``tower`` plus the complementary turbulent flux (H for LE, LE for H).
        This profile is useful only when that complementary flux is available.
    """
    profile = str(profile).strip().lower()
    if profile not in {"current", "external", "tower", "cross"}:
        raise ValueError("profile must be current, external, tower, or cross")
    if "datetime" not in data:
        raise ValueError("datetime column is required")
    dt = pd.to_datetime(data["datetime"], errors="coerce")
    if dt.isna().any():
        raise ValueError("invalid datetime values are not allowed")

    f = pd.DataFrame(index=data.index)
    _add_cyclic_time_features(f, dt)

    if profile == "current":
        for name in CURRENT_RF_FEATURES:
            s = _numeric_series(data, name)
            if s is not None:
                f[name] = s
        return f

    # External drivers.  These are raw/aligned reference fields intentionally;
    # the tree models learn the tower/reference relationship without requiring
    # a single fixed linear calibration for every target.
    for name in EXTERNAL_REF_FEATURES:
        if name in data:
            s = pd.to_numeric(data[name], errors="coerce")
            if s.notna().sum() >= 2:
                f[name] = s

    # Derived external VPD when reference temperature and vapour pressure exist.
    if "at_ref" in f and "vp_ref" in f:
        es = 0.6108 * np.exp(17.27 * f["at_ref"] / (f["at_ref"] + 237.3))
        f["vpd_ref_derived"] = (es - f["vp_ref"]).clip(lower=0)
    if "wind_dir_ref" in f:
        angle = np.deg2rad(f["wind_dir_ref"])
        f["wind_dir_ref_sin"] = np.sin(angle)
        f["wind_dir_ref_cos"] = np.cos(angle)
        f = f.drop(columns=["wind_dir_ref"])

    if profile in {"tower", "cross"}:
        for name in TOWER_FEATURES:
            s = _numeric_series(data, name)
            if s is not None:
                f[name] = s
        if "wind_dir" in f:
            angle = np.deg2rad(f["wind_dir"])
            f["wind_dir_sin"] = np.sin(angle)
            f["wind_dir_cos"] = np.cos(angle)
            f = f.drop(columns=["wind_dir"])
        if "rn" in f and "g" in f:
            f["available_energy"] = f["rn"] - f["g"]
        if "sr" in f and "vpd" in f:
            f["sr_x_vpd"] = f["sr"] * f["vpd"]

    if "sr_ref" in f and "vpd_ref_derived" in f:
        f["sr_ref_x_vpd"] = f["sr_ref"] * f["vpd_ref_derived"]

    if profile == "cross":
        other = "H" if target.upper() == "LE" else "LE" if target.upper() == "H" else None
        if other and other in data:
            cross = pd.to_numeric(data[other], errors="coerce")
            f[f"cross_{other}"] = cross
            if "available_energy" in f:
                f["available_energy_minus_cross"] = f["available_energy"] - cross

    # Missing-indicator columns are added later after training medians are known.
    return f


def _prepare_model_matrix(
    raw: pd.DataFrame,
    train_mask: pd.Series,
    *,
    medians: Optional[Mapping[str, float]] = None,
) -> Tuple[pd.DataFrame, Dict[str, float]]:
    out = pd.DataFrame(index=raw.index)
    learned: Dict[str, float] = {} if medians is None else dict(medians)
    for name in raw.columns:
        values = pd.to_numeric(raw[name], errors="coerce")
        if medians is None:
            tr = values.loc[train_mask]
            median = float(tr.median()) if tr.notna().any() else 0.0
            learned[name] = median
        else:
            median = float(learned.get(name, 0.0))
        out[name] = values.fillna(median)
        if values.isna().any():
            out[f"{name}__missing"] = values.isna().astype("int8")
    return out, learned


def fit_candidate_model(
    data: pd.DataFrame,
    target: str,
    algorithm: str = "XGB",
    profile: str = "tower",
    train_mask: Optional[pd.Series] = None,
    random_state: int = 42,
) -> ModelBundle:
    """Fit an advanced RF/XGB candidate on accepted target observations."""
    algorithm = str(algorithm).upper()
    if algorithm not in {"RF", "XGB"}:
        raise ValueError("algorithm must be RF or XGB")
    y = pd.to_numeric(data[target], errors="coerce")
    if train_mask is None:
        train_mask = y.notna()
    else:
        train_mask = pd.Series(train_mask, index=data.index).astype(bool) & y.notna()

    raw = build_advanced_features(data, target, profile)
    # Remove unusable/constant predictors based only on training rows.
    keep = []
    for col in raw.columns:
        s = pd.to_numeric(raw.loc[train_mask, col], errors="coerce")
        if s.notna().sum() >= 50 and s.nunique(dropna=True) >= 2:
            keep.append(col)
    raw = raw[keep]
    if len(keep) < 3:
        raise ValueError(f"Insufficient usable predictors for {algorithm}/{profile}")
    X, medians = _prepare_model_matrix(raw, train_mask)

    if algorithm == "RF":
        from sklearn.ensemble import RandomForestRegressor
        if profile == "current":
            model = RandomForestRegressor(
                n_estimators=160, max_depth=22, min_samples_leaf=5,
                max_features=0.8, bootstrap=True, n_jobs=1,
                random_state=random_state,
            )
        else:
            model = RandomForestRegressor(
                n_estimators=300, max_depth=24, min_samples_leaf=2,
                max_features=0.70, max_samples=0.90, bootstrap=True,
                n_jobs=1, random_state=random_state,
            )
    else:
        try:
            from xgboost import XGBRegressor
        except ImportError as exc:
            raise ImportError("XGBoost candidate requires the xgboost package") from exc
        model = XGBRegressor(
            n_estimators=650, max_depth=6, learning_rate=0.035,
            min_child_weight=4, subsample=0.85, colsample_bytree=0.85,
            reg_lambda=7.0, reg_alpha=0.05, objective="reg:squarederror",
            tree_method="hist", n_jobs=1, random_state=random_state,
        )

    model.fit(X.loc[train_mask], y.loc[train_mask])
    diagnostics = {
        "algorithm": algorithm,
        "profile": profile,
        "target": target,
        "training_rows": int(train_mask.sum()),
        "features": ", ".join(X.columns),
    }
    if hasattr(model, "feature_importances_"):
        ranked = sorted(zip(X.columns, model.feature_importances_), key=lambda z: z[1], reverse=True)
        diagnostics["feature_importance"] = "; ".join(f"{n}={v:.4f}" for n, v in ranked[:20])
    return ModelBundle(algorithm, profile, target, model, list(X.columns), medians, int(train_mask.sum()), diagnostics)


def predict_candidate(bundle: ModelBundle, data: pd.DataFrame) -> np.ndarray:
    raw = build_advanced_features(data, bundle.target, bundle.profile)
    base_names = [n for n in bundle.medians]
    raw = raw.reindex(columns=base_names)
    dummy_train = pd.Series(True, index=data.index)
    X, _ = _prepare_model_matrix(raw, dummy_train, medians=bundle.medians)
    X = X.reindex(columns=bundle.feature_names, fill_value=0.0)
    return np.asarray(bundle.model.predict(X), dtype=float)


def advanced_gap_fill(
    data: pd.DataFrame,
    target: str,
    algorithm: str = "XGB",
    profile: str = "tower",
    max_gap_days: float = 90.0,
    random_state: int = 42,
) -> pd.DataFrame:
    """Fill eligible missing target records with one explicit ML candidate."""
    df = data.reset_index(drop=True).copy()
    y = pd.to_numeric(df[target], errors="coerce")
    dt = pd.to_datetime(df["datetime"], errors="coerce")
    step = infer_timestep(dt)
    missing = y.isna()
    gap = gap_run_information(missing, step)
    eligible = missing & gap["gap_length_days"].le(float(max_gap_days) + 1e-12)
    bundle = fit_candidate_model(df, target, algorithm, profile, y.notna(), random_state)
    prediction = predict_candidate(bundle, df)
    filled = y.to_numpy(dtype=float)
    filled[eligible.to_numpy()] = prediction[eligible.to_numpy()]
    source = np.where(y.notna(), "measured", np.where(eligible, f"{algorithm}_{profile}", "unfilled"))
    out = pd.DataFrame({
        "datetime": dt,
        f"{target}_original": y,
        f"{target}_filled": filled,
        f"{target}_source": source,
        f"{target}_gap_id": gap["gap_id"],
        f"{target}_gap_length_days": gap["gap_length_days"],
    })
    out.attrs["model_diagnostics"] = bundle.diagnostics
    return out


def calendar_validation_windows(
    data: pd.DataFrame,
    target: str,
    duration_days: float,
    n_windows: int = 3,
    minimum_observed_fraction: float = 0.35,
) -> List[pd.Series]:
    """Choose spread-out calendar windows without requiring 100% target coverage.

    Accuracy is scored only against target observations that were available before
    masking.  This is more realistic than requiring a perfectly complete block.
    """
    dt = pd.to_datetime(data["datetime"], errors="coerce")
    y = pd.to_numeric(data[target], errors="coerce")
    start, end = dt.min(), dt.max()
    candidates: List[Tuple[float, pd.Series]] = []
    for q in np.linspace(0.10, 0.90, 33):
        center = start + (end - start) * q
        left = center - pd.Timedelta(days=duration_days / 2)
        right = center + pd.Timedelta(days=duration_days / 2)
        mask = dt.between(left, right, inclusive="left")
        total = int(mask.sum())
        observed = int((mask & y.notna()).sum())
        if total and observed >= 20 and observed / total >= minimum_observed_fraction:
            candidates.append((observed / total, mask))
    if not candidates:
        return []
    # Spread selected centers through the candidate list; coverage threshold has
    # already screened unusable windows.
    idx = np.linspace(0, len(candidates) - 1, min(n_windows, len(candidates))).round().astype(int)
    return [candidates[i][1] for i in idx]


def score_prediction(truth: np.ndarray, pred: np.ndarray) -> Dict[str, float]:
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    ok = np.isfinite(truth) & np.isfinite(pred)
    if not ok.any():
        return {"n": 0, "rmse": np.nan, "mae": np.nan, "bias": np.nan, "r2": np.nan}
    y, p = truth[ok], pred[ok]
    return {
        "n": int(len(y)),
        "rmse": float(np.sqrt(mean_squared_error(y, p))),
        "mae": float(mean_absolute_error(y, p)),
        "bias": float(np.mean(p - y)),
        "r2": float(r2_score(y, p)),
    }
