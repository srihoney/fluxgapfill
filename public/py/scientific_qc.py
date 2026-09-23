"""Input-unit normalization, generic QA/QC screening, and post-fill diagnostics.

The routines are deliberately transparent: raw values are preserved, every
rejected value receives a reason flag, and no value is silently clipped into an
acceptable range.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

EXPECTED_UNITS: Dict[str, str] = {
    "LE": "W m-2",
    "H": "W m-2",
    "ET": "mm interval-1",
    "sr": "W m-2",
    "rn": "W m-2",
    "g": "W m-2",
    "lw_in": "W m-2",
    "at": "degC",
    "rh": "%",
    "vpd": "kPa",
    "ws": "m s-1",
    "rain": "mm interval-1",
    "pa": "kPa",
    "swc": "m3 m-3",
}

# Broad instrument/physical plausibility limits. These are screening limits,
# not ecological acceptance limits. Values outside are retained in *_raw.
PHYSICAL_LIMITS: Dict[str, Tuple[float, float]] = {
    "LE": (-1000.0, 1500.0),
    "H": (-1000.0, 1500.0),
    "ET": (-2.0, 5.0),
    "sr": (-20.0, 1500.0),
    "rn": (-600.0, 1200.0),
    "g": (-500.0, 800.0),
    "lw_in": (100.0, 800.0),
    "at": (-60.0, 65.0),
    "rh": (0.0, 105.0),
    "vpd": (-0.05, 12.0),
    "ws": (0.0, 75.0),
    "rain": (0.0, 500.0),
    "pa": (50.0, 110.0),
    "swc": (0.0, 0.85),
}


def _unit_key(unit: str) -> str:
    text = str(unit or "").strip().lower()
    for token in ("²", "^2", "−", "·", "_", " ", "(", ")", "/"):
        text = text.replace(token, "")
    text = text.replace("per", "")
    return text


def _identity(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce")


def convert_units(values: pd.Series, variable: str, unit: Optional[str]) -> Tuple[pd.Series, str, str]:
    """Convert common source units to the canonical units used by FluxGap.

    Returns converted values, canonical unit, and a human-readable conversion
    description. Missing unit declarations are treated as canonical assumptions
    and are explicitly reported in the QA/QC audit.
    """
    v = pd.to_numeric(values, errors="coerce")
    expected = EXPECTED_UNITS.get(variable, "unknown")
    if not unit:
        return v, expected, "assumed canonical unit (unit not declared)"

    key = _unit_key(unit)
    canonical_keys = {
        "LE": {"wm-2", "wm2", "wattm-2", "wattsm-2"},
        "H": {"wm-2", "wm2", "wattm-2", "wattsm-2"},
        "sr": {"wm-2", "wm2", "wattm-2", "wattsm-2"},
        "rn": {"wm-2", "wm2", "wattm-2", "wattsm-2"},
        "g": {"wm-2", "wm2", "wattm-2", "wattsm-2"},
        "lw_in": {"wm-2", "wm2", "wattm-2", "wattsm-2"},
        "at": {"degc", "c", "celsius", "°c"},
        "rh": {"%", "percent", "pct"},
        "vpd": {"kpa"},
        "ws": {"ms-1", "ms", "msec-1", "m/s"},
        "rain": {"mm", "mminterval-1", "mminterval"},
        "pa": {"kpa"},
        "swc": {"m3m-3", "m3/m3", "fraction", "vv"},
        "ET": {"mm", "mminterval-1", "mminterval"},
    }
    if key in canonical_keys.get(variable, set()) or str(unit).strip() == expected:
        return v, expected, "no conversion"

    # Temperature.
    if variable == "at":
        if key in {"f", "degf", "fahrenheit", "°f"}:
            return (v - 32.0) * 5.0 / 9.0, expected, "degF to degC"
        if key in {"k", "kelvin"}:
            return v - 273.15, expected, "K to degC"

    # Wind speed.
    if variable == "ws":
        if key in {"mph", "milehour-1", "mileshour-1"}:
            return v * 0.44704, expected, "mph to m s-1"
        if key in {"kmh-1", "kmhr-1", "kmph", "km/h"}:
            return v / 3.6, expected, "km h-1 to m s-1"

    # Pressure.
    if variable == "pa":
        if key in {"pa", "pascal", "pascals"}:
            return v / 1000.0, expected, "Pa to kPa"
        if key in {"hpa", "mbar", "mb"}:
            return v / 10.0, expected, "hPa/mbar to kPa"

    # Water amounts.
    if variable in {"rain", "ET"} and key in {"in", "inch", "inches"}:
        return v * 25.4, expected, "inch to mm"

    # Soil water content.
    if variable == "swc" and key in {"%", "percent", "pct"}:
        return v / 100.0, expected, "% to m3 m-3"

    # Energy-flux alternatives.
    if variable in {"LE", "H", "sr", "rn", "g", "lw_in"}:
        if key in {"kwm-2", "kwm2"}:
            return v * 1000.0, expected, "kW m-2 to W m-2"

    raise ValueError(
        f"Unsupported unit {unit!r} for {variable}. Expected {expected}. "
        "Declare a supported unit in the optional Metadata sheet or convert the input before upload."
    )


def find_quality_column(frame: pd.DataFrame, variable: str) -> Optional[str]:
    lookup = {str(c).strip().lower(): c for c in frame.columns}
    candidates = [
        f"{variable.lower()}_qc",
        f"{variable.lower()}qc",
        f"qc_{variable.lower()}",
        f"{variable.lower()}_flag",
        f"{variable.lower()}flag",
        f"{variable.lower()}_quality",
    ]
    for name in candidates:
        if name in lookup:
            return lookup[name]
    return None


def _quality_accept_mask(series: pd.Series, maximum_accepted_qc: float) -> pd.Series:
    """Interpret common numeric and text QA/QC conventions conservatively."""
    numeric = pd.to_numeric(series, errors="coerce")
    out = pd.Series(False, index=series.index)
    numeric_mask = numeric.notna()
    out.loc[numeric_mask] = numeric.loc[numeric_mask] <= maximum_accepted_qc

    text = series.astype(str).str.strip().str.lower()
    # Blank/NA means no supplied rejection flag. Common good labels are allowed.
    good_text = text.isin({"", "nan", "none", "good", "ok", "accepted", "true", "pass", "0"})
    out.loc[~numeric_mask] = good_text.loc[~numeric_mask]
    return out


def screen_input_frame(
    frame: pd.DataFrame,
    variables: Iterable[str],
    units: Optional[Mapping[str, str]] = None,
    maximum_accepted_qc: float = 1.0,
    strict_units: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Normalize units and reject flagged/physically implausible values.

    The processed variable retains the canonical name. Raw values and screening
    flags are added as `<var>_raw`, `<var>_input_qc`, and `<var>_screen_flag`.
    """
    out = frame.copy()
    units = {str(k): str(v) for k, v in (units or {}).items()}
    audit_rows = []

    for variable in variables:
        if variable not in out.columns:
            continue
        raw = pd.to_numeric(out[variable], errors="coerce")
        out[f"{variable}_raw"] = raw
        declared = units.get(variable)
        if strict_units and not declared:
            raise ValueError(
                f"Strict unit validation is enabled, but no unit was declared for {variable}. "
                "Add a Metadata sheet with columns variable and unit."
            )
        converted, canonical_unit, conversion = convert_units(raw, variable, declared)

        qc_col = find_quality_column(out, variable)
        if qc_col:
            input_qc = out[qc_col]
            accepted_qc = _quality_accept_mask(input_qc, maximum_accepted_qc)
        else:
            input_qc = pd.Series(np.nan, index=out.index)
            accepted_qc = pd.Series(True, index=out.index)
        out[f"{variable}_input_qc"] = input_qc

        lower, upper = PHYSICAL_LIMITS.get(variable, (-np.inf, np.inf))
        physical = converted.between(lower, upper, inclusive="both") | converted.isna()
        accepted = accepted_qc & physical

        flag = pd.Series("accepted", index=out.index, dtype=object)
        flag.loc[converted.isna()] = "missing_or_non_numeric"
        flag.loc[converted.notna() & ~physical] = "outside_physical_screen"
        flag.loc[converted.notna() & physical & ~accepted_qc] = "rejected_by_input_qc"
        out[f"{variable}_screen_flag"] = flag
        out[variable] = converted.where(accepted)

        audit_rows.append(
            {
                "variable": variable,
                "declared_unit": declared or "not declared",
                "canonical_unit": canonical_unit,
                "conversion": conversion,
                "quality_column": qc_col or "none",
                "maximum_accepted_qc": maximum_accepted_qc,
                "n_raw_numeric": int(raw.notna().sum()),
                "n_accepted": int(out[variable].notna().sum()),
                "n_rejected_qc": int((flag == "rejected_by_input_qc").sum()),
                "n_rejected_physical": int((flag == "outside_physical_screen").sum()),
                "n_missing_or_non_numeric": int((flag == "missing_or_non_numeric").sum()),
            }
        )
    return out, pd.DataFrame(audit_rows)



def estimate_random_uncertainty(
    data: pd.DataFrame,
    targets: Iterable[str] = ("LE", "H"),
    window_days: int = 5,
    time_tolerance_hours: float = 1.0,
    minimum_donors: int = 5,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Estimate random flux uncertainty from measured analog observations.

    The direct estimate uses measured fluxes within ±5 days and ±1 hour under
    similar meteorological conditions (TA ±2.5 °C, VPD ±0.5 kPa, and SW_IN
    tolerance 20–50 W m⁻²). Where direct analogs are unavailable, the median
    direct uncertainty of measured fluxes within ±20% (minimum ±10 W m⁻²) is
    used. Gap-filling and random uncertainty are then combined in quadrature.
    """
    df = data.copy()
    dt = pd.DatetimeIndex(pd.to_datetime(df["datetime"], errors="coerce"))
    if dt.isna().any() or len(dt) < 3:
        return df, pd.DataFrame([{"variable": "all", "method": "unavailable", "reason": "invalid timestamps"}])
    ns = dt.view("i8")
    day_ns = pd.Timedelta(days=1).value
    half_window_ns = pd.Timedelta(days=window_days).value
    tod = np.mod(ns, day_ns)
    tod_tolerance = pd.Timedelta(hours=time_tolerance_hours).value

    def driver(name: str) -> np.ndarray:
        col = f"{name}_filled" if f"{name}_filled" in df else name if name in df else None
        return pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float) if col else np.full(len(df), np.nan)

    sr = driver("sr"); at = driver("at"); vpd = driver("vpd")
    summary = []
    for target in targets:
        source_col = target if target in df else f"{target}_original" if f"{target}_original" in df else None
        filled_col = f"{target}_filled" if f"{target}_filled" in df else source_col
        if not source_col or not filled_col:
            continue
        measured_y = pd.to_numeric(df[source_col], errors="coerce").to_numpy(dtype=float)
        final_y = pd.to_numeric(df[filled_col], errors="coerce").to_numpy(dtype=float)
        measured = np.isfinite(measured_y)
        direct = np.full(len(df), np.nan, dtype=float)
        donor_n = np.zeros(len(df), dtype=int)
        measured_positions = np.flatnonzero(measured)
        for i in measured_positions:
            left = np.searchsorted(ns, ns[i] - half_window_ns, side="left")
            right = np.searchsorted(ns, ns[i] + half_window_ns, side="right")
            pos = np.arange(left, right)
            if len(pos) <= 1:
                continue
            slot_diff = np.abs(tod[pos] - tod[i])
            slot_diff = np.minimum(slot_diff, day_ns - slot_diff)
            mask = measured[pos] & (slot_diff <= tod_tolerance) & (pos != i)
            if np.isfinite(at[i]):
                mask &= np.isfinite(at[pos]) & (np.abs(at[pos] - at[i]) <= 2.5)
            if np.isfinite(vpd[i]):
                mask &= np.isfinite(vpd[pos]) & (np.abs(vpd[pos] - vpd[i]) <= 0.5)
            if np.isfinite(sr[i]):
                sr_tol = max(20.0, min(50.0, abs(sr[i])))
                mask &= np.isfinite(sr[pos]) & (np.abs(sr[pos] - sr[i]) <= sr_tol)
            donors = measured_y[pos[mask]]
            if len(donors) >= minimum_donors:
                direct[i] = float(np.std(donors, ddof=1))
                donor_n[i] = int(len(donors))

        uncertainty = direct.copy()
        method = np.full(len(df), "unavailable", dtype=object)
        method[np.isfinite(direct)] = "analog_direct_sd"
        valid_direct = measured & np.isfinite(direct)
        global_median = float(np.nanmedian(direct[valid_direct])) if valid_direct.any() else np.nan
        # Fallback by similar flux magnitude. This fills both measured records
        # lacking enough meteorological analogs and reconstructed records.
        direct_flux = measured_y[valid_direct]
        direct_unc = direct[valid_direct]
        for i in np.flatnonzero(np.isfinite(final_y) & ~np.isfinite(uncertainty)):
            tolerance = max(10.0, 0.20 * abs(final_y[i]))
            similar = np.abs(direct_flux - final_y[i]) <= tolerance
            if similar.sum() >= minimum_donors:
                uncertainty[i] = float(np.nanmedian(direct_unc[similar])); method[i] = "flux_magnitude_median_sd"
            elif np.isfinite(global_median):
                uncertainty[i] = global_median; method[i] = "global_median_sd"

        df[f"{target}_random_uncertainty"] = uncertainty
        df[f"{target}_random_uncertainty_method"] = method
        df[f"{target}_random_uncertainty_n"] = donor_n
        fill_unc_col = f"{target}_fill_uncertainty"
        fill_unc = pd.to_numeric(df[fill_unc_col], errors="coerce").fillna(0).to_numpy(dtype=float) if fill_unc_col in df else np.zeros(len(df))
        df[f"{target}_joint_uncertainty"] = np.sqrt(np.square(np.nan_to_num(uncertainty, nan=0.0)) + np.square(fill_unc))
        df.loc[~np.isfinite(uncertainty), f"{target}_joint_uncertainty"] = np.nan
        summary.append({
            "variable": target,
            "method": "analog random uncertainty",
            "n_direct": int(np.isfinite(direct).sum()),
            "n_fallback": int(np.isfinite(uncertainty).sum() - np.isfinite(direct).sum()),
            "percent_available": float(100 * np.isfinite(uncertainty).mean()),
            "median_random_uncertainty": float(np.nanmedian(uncertainty)) if np.isfinite(uncertainty).any() else np.nan,
        })
    return df, pd.DataFrame(summary)


def postfill_physical_diagnostics(data: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Add row-level scientific warnings and return an aggregate diagnostic table."""
    df = data.copy()
    row_flags = pd.Series("", index=df.index, dtype=object)
    summary = []

    def add_flag(mask: pd.Series, label: str, variable: str, severity: str = "warning") -> None:
        nonlocal row_flags
        mask = mask.fillna(False)
        if not mask.any():
            return
        row_flags.loc[mask] = row_flags.loc[mask].apply(lambda x: f"{x};{label}".strip(";"))
        summary.append({"variable": variable, "diagnostic": label, "severity": severity, "n_rows": int(mask.sum())})

    for variable, (lower, upper) in PHYSICAL_LIMITS.items():
        col = f"{variable}_filled" if f"{variable}_filled" in df else variable if variable in df else None
        if not col:
            continue
        values = pd.to_numeric(df[col], errors="coerce")
        add_flag(values.notna() & ~values.between(lower, upper), "postfill_outside_physical_screen", variable, "error")

    if "sr_filled" in df:
        sr = pd.to_numeric(df["sr_filled"], errors="coerce")
        dt = pd.to_datetime(df["datetime"], errors="coerce")
        night_clock = (dt.dt.hour < 5) | (dt.dt.hour >= 21)
        add_flag(night_clock & (sr > 25), "high_shortwave_during_clock_night", "sr")

    if "at_filled" in df and "rh_filled" in df and "vpd_filled" in df:
        t = pd.to_numeric(df["at_filled"], errors="coerce")
        rh = pd.to_numeric(df["rh_filled"], errors="coerce").clip(0, 100)
        expected = 0.6108 * np.exp(17.27 * t / (t + 237.3)) * (1 - rh / 100)
        actual = pd.to_numeric(df["vpd_filled"], errors="coerce")
        add_flag((actual - expected).abs() > 0.5, "vpd_inconsistent_with_temperature_rh", "vpd")

    if all(c in df.columns for c in ("rn_filled", "g_filled", "H_filled", "LE_filled")):
        available = pd.to_numeric(df["rn_filled"], errors="coerce") - pd.to_numeric(df["g_filled"], errors="coerce")
        turbulent = pd.to_numeric(df["H_filled"], errors="coerce") + pd.to_numeric(df["LE_filled"], errors="coerce")
        residual = available - turbulent
        df["energy_balance_available"] = available
        df["energy_balance_turbulent"] = turbulent
        df["energy_balance_residual"] = residual
        ratio = turbulent / available.where(available.abs() >= 20)
        df["energy_balance_ratio"] = ratio
        add_flag(ratio.notna() & ~ratio.between(0.5, 1.5), "energy_balance_ratio_outside_0.5_1.5", "energy_balance")
        summary.append({
            "variable": "energy_balance",
            "diagnostic": "median_energy_balance_ratio",
            "severity": "information",
            "n_rows": int(ratio.notna().sum()),
            "value": float(ratio.median()) if ratio.notna().any() else np.nan,
        })

    df["scientific_warning"] = row_flags.replace("", "none")
    return df, pd.DataFrame(summary)


def apply_energy_balance_correction(data: pd.DataFrame, method: str = "none") -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Create separate rolling energy-balance-corrected LE/H products.

    The correction factor is the centered 5-day rolling median of
    ``(Rn-G)/(H+LE)`` from physically usable intervals. Pointwise factors are
    not used directly because they amplify half-hourly noise. Raw filled
    products remain unchanged. The 25th/75th factor percentiles are exported
    to characterize correction uncertainty.
    """
    df = data.copy()
    if method == "none":
        return df, pd.DataFrame([{"method": "none", "n_corrected": 0}])
    required = ("rn_filled", "g_filled", "H_filled", "LE_filled")
    if not all(c in df for c in required):
        return df, pd.DataFrame([{"method": method, "n_corrected": 0, "reason": "Rn, G, H, and LE are required"}])

    dt = pd.to_datetime(df["datetime"], errors="coerce")
    available = pd.to_numeric(df["rn_filled"], errors="coerce") - pd.to_numeric(df["g_filled"], errors="coerce")
    h = pd.to_numeric(df["H_filled"], errors="coerce")
    le = pd.to_numeric(df["LE_filled"], errors="coerce")
    turbulent = h + le
    instantaneous = available / turbulent.where(turbulent.abs() >= 20)
    usable = available.abs().ge(20) & instantaneous.between(0.2, 5.0) & h.notna() & le.notna()
    series = pd.Series(instantaneous.where(usable).to_numpy(), index=pd.DatetimeIndex(dt))
    rolling = series.rolling("5D", center=True, min_periods=5)
    factor50 = pd.Series(rolling.median().to_numpy(), index=df.index)
    factor25 = pd.Series(rolling.quantile(0.25).to_numpy(), index=df.index)
    factor75 = pd.Series(rolling.quantile(0.75).to_numpy(), index=df.index)
    valid = factor50.between(0.5, 2.0) & h.notna() & le.notna()

    df["energy_balance_instantaneous_factor"] = instantaneous
    df["energy_balance_correction_factor"] = factor50.where(valid)
    df["energy_balance_correction_factor25"] = factor25.where(valid)
    df["energy_balance_correction_factor75"] = factor75.where(valid)
    df["H_energy_balance_corrected"] = h.where(~valid, h * factor50)
    df["LE_energy_balance_corrected"] = le.where(~valid, le * factor50)
    df["H_energy_balance_corrected25"] = h.where(~valid, h * factor25)
    df["H_energy_balance_corrected75"] = h.where(~valid, h * factor75)
    df["LE_energy_balance_corrected25"] = le.where(~valid, le * factor25)
    df["LE_energy_balance_corrected75"] = le.where(~valid, le * factor75)
    df["energy_balance_correction_applied"] = valid

    factor_sigma = ((factor75 - factor25).abs() / 1.349).where(valid)
    for target, values in (("H", h), ("LE", le)):
        base_col = f"{target}_joint_uncertainty" if f"{target}_joint_uncertainty" in df else f"{target}_fill_uncertainty"
        base = pd.to_numeric(df[base_col], errors="coerce") if base_col in df else pd.Series(np.nan, index=df.index)
        correction_component = values.abs() * factor_sigma
        df[f"{target}_energy_balance_corrected_uncertainty"] = np.sqrt(np.square(base * factor50.where(valid, 1.0)) + np.square(correction_component.fillna(0)))

    summary = pd.DataFrame([{
        "method": "rolling_bowen_ratio_5day",
        "n_factor_observations": int(usable.sum()),
        "n_corrected": int(valid.sum()),
        "percent_corrected": float(100 * valid.mean()),
        "median_factor": float(factor50[valid].median()) if valid.any() else np.nan,
        "factor25": float(factor25[valid].median()) if valid.any() else np.nan,
        "factor75": float(factor75[valid].median()) if valid.any() else np.nan,
        "factor_min_allowed": 0.5,
        "factor_max_allowed": 2.0,
    }])
    return df, summary



@dataclass(frozen=True)
class RadiationQCConfig:
    """Conservative component-level screening for tower net radiation.

    These limits are intended to identify sensor/channel failures, not to force
    energy-balance closure. Raw values remain in ``rn_original_unfiltered``.
    """

    daytime_shortwave_threshold_w_m2: float = 20.0
    strong_daytime_shortwave_w_m2: float = 100.0
    daytime_min_rn_w_m2: float = -100.0
    strong_daytime_min_rn_w_m2: float = -20.0
    sustained_inversion_rn_w_m2: float = -50.0
    sustained_inversion_min_records: int = 3
    nighttime_shortwave_threshold_w_m2: float = 5.0
    nighttime_min_rn_w_m2: float = -250.0
    nighttime_max_rn_w_m2: float = 150.0
    daytime_rn_above_shortwave_allowance_w_m2: float = 150.0
    abrupt_step_min_w_m2: float = 350.0
    local_hampel_window_records: int = 13
    local_hampel_sigma: float = 8.0
    local_hampel_min_deviation_w_m2: float = 250.0


def _consecutive_true_mask(mask: pd.Series, minimum_length: int) -> pd.Series:
    mask = mask.fillna(False).astype(bool)
    if minimum_length <= 1:
        return mask
    groups = mask.ne(mask.shift(fill_value=False)).cumsum()
    lengths = mask.groupby(groups).transform("size")
    return mask & lengths.ge(int(minimum_length))


def apply_radiation_component_qc(
    frame: pd.DataFrame,
    config: Optional[RadiationQCConfig] = None,
    *,
    rn_column: str = "rn",
    shortwave_candidates: Iterable[str] = ("sr", "sr_original", "SWIN_1_1_1"),
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Reject clearly invalid tower net-radiation measurements before filling.

    The routine uses radiation-component consistency, sustained sign inversion,
    temporal discontinuities, and a conservative centered Hampel diagnostic.
    It never filters values based on energy-closure residuals.
    """
    out = frame.copy()
    cfg = config or RadiationQCConfig()
    if rn_column not in out:
        return out, pd.DataFrame([{"variable": rn_column, "status": "not_available"}])

    rn = pd.to_numeric(out[rn_column], errors="coerce")
    out["rn_before_component_qc"] = rn
    sr = pd.Series(np.nan, index=out.index, dtype=float)
    for candidate in shortwave_candidates:
        if candidate in out:
            candidate_values = pd.to_numeric(out[candidate], errors="coerce")
            if candidate_values.notna().any():
                sr = candidate_values
                break

    accepted_input = rn.notna()
    reason = pd.Series("accepted", index=out.index, dtype=object)
    reason.loc[~accepted_input] = "missing_before_component_qc"

    day = sr.ge(cfg.daytime_shortwave_threshold_w_m2)
    strong_day = sr.ge(cfg.strong_daytime_shortwave_w_m2)
    night = sr.le(cfg.nighttime_shortwave_threshold_w_m2)

    daytime_strong_negative = accepted_input & day & rn.lt(cfg.daytime_min_rn_w_m2)
    daytime_sign_inconsistent = accepted_input & strong_day & rn.lt(cfg.strong_daytime_min_rn_w_m2)
    inversion_seed = accepted_input & day & rn.lt(cfg.sustained_inversion_rn_w_m2)
    sustained_inversion = _consecutive_true_mask(inversion_seed, cfg.sustained_inversion_min_records)
    daytime_too_high = accepted_input & day & rn.gt(sr + cfg.daytime_rn_above_shortwave_allowance_w_m2)
    nighttime_implausible = accepted_input & night & (
        rn.lt(cfg.nighttime_min_rn_w_m2) | rn.gt(cfg.nighttime_max_rn_w_m2)
    )

    # Discontinuity screening is applied only across contiguous records and only
    # when the Rn jump is not plausibly explained by the simultaneous SW change.
    dt = pd.to_datetime(out.get("datetime", out.get("datetime_midpoint")), errors="coerce")
    dt_seconds = dt.diff().dt.total_seconds()
    typical_seconds = float(dt_seconds[dt_seconds.gt(0)].median()) if dt_seconds.notna().any() else np.nan
    contiguous = pd.Series(True, index=out.index)
    if np.isfinite(typical_seconds):
        contiguous = dt_seconds.gt(0) & dt_seconds.le(1.5 * typical_seconds)
    rn_jump = rn.diff().abs()
    sr_jump = sr.diff().abs().fillna(0.0)
    abrupt_step = (
        accepted_input
        & contiguous.fillna(False)
        & rn_jump.gt(cfg.abrupt_step_min_w_m2)
        & rn_jump.gt(2.0 * sr_jump + 100.0)
    )

    window = max(5, int(cfg.local_hampel_window_records))
    if window % 2 == 0:
        window += 1
    local_median = rn.rolling(window, center=True, min_periods=max(5, window // 2)).median()
    abs_dev = (rn - local_median).abs()
    local_mad = abs_dev.rolling(window, center=True, min_periods=max(5, window // 2)).median()
    robust_sigma = 1.4826 * local_mad
    local_outlier = (
        accepted_input
        & robust_sigma.notna()
        & abs_dev.gt(cfg.local_hampel_sigma * robust_sigma)
        & abs_dev.gt(cfg.local_hampel_min_deviation_w_m2)
    )

    # Specific component-failure reasons take precedence over generic temporal
    # flags so the audit remains interpretable.
    masks_and_labels = (
        (nighttime_implausible, "nighttime_rn_implausible"),
        (daytime_too_high, "daytime_rn_exceeds_shortwave"),
        (abrupt_step, "rn_abrupt_step"),
        (local_outlier, "rn_local_outlier"),
        (daytime_sign_inconsistent, "daytime_rn_sign_inconsistent"),
        (daytime_strong_negative, "daytime_rn_strongly_negative"),
        (sustained_inversion, "sustained_daytime_rn_inversion"),
    )
    rejected = pd.Series(False, index=out.index)
    for mask, label in masks_and_labels:
        applicable = mask.fillna(False) & accepted_input
        reason.loc[applicable] = label
        rejected |= applicable

    out["rn_component_qc_flag"] = reason
    out[rn_column] = rn.where(~rejected)
    if "rn_screen_flag" in out:
        screen_flag = out["rn_screen_flag"].astype(object).copy()
        screen_flag.loc[rejected] = reason.loc[rejected]
        out["rn_screen_flag"] = screen_flag
    else:
        out["rn_screen_flag"] = reason

    rows = [{
        "variable": "rn",
        "status": "screened",
        "n_input_accepted": int(accepted_input.sum()),
        "n_component_rejected": int(rejected.sum()),
        "n_remaining": int(out[rn_column].notna().sum()),
        "shortwave_column_available": bool(sr.notna().any()),
    }]
    for label, count in reason[rejected].value_counts().sort_index().items():
        rows.append({
            "variable": "rn",
            "status": "rejection_reason",
            "reason": str(label),
            "count": int(count),
        })
    return out, pd.DataFrame(rows)
