"""Input parsing, QA/QC screening, and temporal alignment.

All timestamps are converted to a canonical *local standard time midpoint* grid.
This avoids silent daylight-saving shifts when station data (such as CIMIS) are
reported in standard time. Raw timestamps and rejected values remain available
in the outputs for auditability.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from scientific_qc import PHYSICAL_LIMITS, screen_input_frame

logger = logging.getLogger(__name__)

FLUX_VARS = ("ET", "H", "LE", "NEE")
BIOMET_VARS = ("sr", "at", "vpd", "rh", "ws", "rain", "pa", "lw_in", "rn", "g", "swc")
CONTINUOUS_BIOMET = ("sr", "at", "vpd", "rh", "ws", "pa", "lw_in", "rn", "g", "swc")

ALIASES = {
    "sw_in": "sr", "swin": "sr", "rg": "sr", "solar_radiation": "sr", "shortwave": "sr",
    "ta": "at", "tair": "at", "air_temperature": "at", "relative_humidity": "rh",
    "wind_speed": "ws", "precip": "rain", "precipitation": "rain", "pressure": "pa",
    "air_pressure": "pa", "lw": "lw_in", "net_radiation": "rn", "netrad": "rn",
    "soil_heat_flux": "g", "soil_moisture": "swc", "vwc": "swc",
}


@dataclass(frozen=True)
class InputProcessingConfig:
    timezone: str = "America/Los_Angeles"
    time_basis: str = "local_standard"  # local_standard, local_civil, utc
    timestamp_convention: str = "midpoint"  # start, midpoint, end
    maximum_accepted_qc: float = 1.0
    strict_units: bool = False


@dataclass(frozen=True)
class ProcessingMetadata:
    timestep_minutes: float
    start_datetime: pd.Timestamp
    end_datetime: pd.Timestamp
    flux_rows_original: int
    rows_regularized: int
    duplicate_flux_timestamps: int
    duplicate_biomet_timestamps: int
    biomet_native_timestep_minutes: Optional[float]
    timezone: str = "America/Los_Angeles"
    time_basis: str = "local_standard"
    timestamp_convention: str = "midpoint"
    canonical_time_basis: str = "local_standard_midpoint"
    invalid_flux_timestamps: int = 0
    invalid_biomet_timestamps: int = 0
    dst_ambiguous_or_nonexistent: int = 0
    qc_audit_records: int = 0


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    rename: Dict[str, str] = {}
    for col in out.columns:
        stripped = str(col).strip()
        low = stripped.lower()
        if low in {"et", "h", "le"}:
            rename[col] = low.upper()
        elif low in ALIASES:
            rename[col] = ALIASES[low]
        elif low in BIOMET_VARS or low in {"date", "time", "hour", "datetime", "timestamp"}:
            rename[col] = low
        else:
            rename[col] = stripped
    return out.rename(columns=rename)


def _parse_numeric_time(value: float) -> Tuple[int, int, int]:
    if pd.isna(value):
        return -1, -1, 0
    value = float(value)
    if 0 <= value < 1 and not value.is_integer():
        total_seconds = int(round(value * 24 * 3600)) % (24 * 3600)
        return total_seconds // 3600, (total_seconds % 3600) // 60, total_seconds % 60
    ivalue = int(round(value))
    if 0 <= ivalue <= 23:
        return ivalue, 0, 0
    if 0 <= ivalue <= 2400:
        if ivalue == 2400:
            return 24, 0, 0
        hour, minute = divmod(ivalue, 100)
        if hour <= 23 and minute <= 59:
            return hour, minute, 0
    return -1, -1, 0


def build_datetime(df: pd.DataFrame, sheet_name: str) -> pd.Series:
    work = _normalize_columns(df)
    for direct in ("datetime", "timestamp"):
        if direct in work.columns:
            parsed = pd.to_datetime(work[direct], errors="coerce")
            if parsed.notna().any():
                return parsed
    if "date" not in work.columns:
        raise ValueError(f"{sheet_name} needs a datetime/timestamp column or a date column.")
    time_col = "time" if "time" in work.columns else "hour" if "hour" in work.columns else None
    if time_col is None:
        raise ValueError(f"{sheet_name} needs time or hour when date is supplied.")

    dates = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    result = []
    for date, value in zip(dates, work[time_col]):
        if pd.isna(date) or pd.isna(value):
            result.append(pd.NaT); continue
        if isinstance(value, (int, float, np.integer, np.floating)):
            hour, minute, second = _parse_numeric_time(value)
            day_offset = 1 if hour == 24 else 0
            hour = 0 if hour == 24 else hour
            result.append(pd.NaT if hour < 0 else date + pd.Timedelta(days=day_offset, hours=hour, minutes=minute, seconds=second))
        else:
            text = str(value).strip()
            if text in {"24:00", "24:00:00"}:
                result.append(date + pd.Timedelta(days=1)); continue
            parsed = pd.to_datetime(text, errors="coerce")
            result.append(pd.NaT if pd.isna(parsed) else date + pd.Timedelta(hours=parsed.hour, minutes=parsed.minute, seconds=parsed.second))
    return pd.Series(result, index=df.index, dtype="datetime64[ns]")


def infer_timestep(index_or_series: Iterable[pd.Timestamp]) -> pd.Timedelta:
    ts = pd.DatetimeIndex(pd.to_datetime(index_or_series, errors="coerce")).dropna().sort_values().unique()
    if len(ts) < 3:
        raise ValueError("At least three valid timestamps are required to infer the time step.")
    diffs = pd.Series(ts[1:] - ts[:-1]); diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        raise ValueError("Could not infer a positive time step.")
    mode = diffs.mode(); step = mode.iloc[0] if not mode.empty else diffs.median()
    if step < pd.Timedelta(minutes=5) or step > pd.Timedelta(hours=3):
        raise ValueError(f"Unsupported inferred time step: {step}. Expected 5 min to 3 h.")
    return step


def _standard_offset(tz_name: str) -> timezone:
    """Return the non-DST UTC offset for the selected zone."""
    zone = ZoneInfo(tz_name)
    offsets = []
    for month in (1, 4, 7, 10):
        value = datetime(2024, month, 1, 12, tzinfo=zone).utcoffset()
        if value is not None:
            offsets.append(value)
    # Standard time is normally the smallest (most negative) offset.
    offset = min(offsets) if offsets else timedelta(0)
    return timezone(offset)


def canonicalize_timestamps(
    values: Iterable,
    step: pd.Timedelta,
    config: InputProcessingConfig,
) -> Tuple[pd.DatetimeIndex, int]:
    """Convert timestamps to local-standard-time interval midpoints."""
    idx = pd.DatetimeIndex(pd.to_datetime(values, errors="coerce"))
    convention = config.timestamp_convention.lower()
    if convention == "start":
        idx = idx + step / 2
    elif convention == "end":
        idx = idx - step / 2
    elif convention != "midpoint":
        raise ValueError("Timestamp convention must be start, midpoint, or end.")

    basis = config.time_basis.lower()
    standard_tz = _standard_offset(config.timezone)
    invalid_before = int(idx.isna().sum())
    if idx.tz is not None:
        aware = idx
    elif basis == "utc":
        aware = idx.tz_localize("UTC")
    elif basis == "local_civil":
        aware = idx.tz_localize(config.timezone, ambiguous="NaT", nonexistent="NaT")
    elif basis == "local_standard":
        aware = idx.tz_localize(standard_tz)
    else:
        raise ValueError("Time basis must be local_standard, local_civil, or utc.")

    ambiguous_count = max(0, int(pd.isna(aware).sum()) - invalid_before)
    canonical = aware.tz_convert(standard_tz).tz_localize(None)
    return canonical, ambiguous_count


def _aggregate_duplicate_timestamps(df: pd.DataFrame, precipitation_columns=("rain",)) -> Tuple[pd.DataFrame, int]:
    duplicate_count = int(df["datetime"].duplicated(keep=False).sum())
    if duplicate_count == 0:
        return df.sort_values("datetime"), 0
    numeric = [c for c in df.select_dtypes(include=[np.number]).columns if c != "datetime"]
    aggregation = {c: ("sum" if c in precipitation_columns else "mean") for c in numeric}
    for c in df.columns:
        if c not in aggregation and c != "datetime":
            aggregation[c] = "first"
    return df.groupby("datetime", as_index=False).agg(aggregation).sort_values("datetime"), duplicate_count


def _safe_temporal_disaggregation(source: pd.Series, target_index: pd.DatetimeIndex, source_step: pd.Timedelta) -> Tuple[pd.Series, pd.Series]:
    src = source.dropna().sort_index(); out = pd.Series(np.nan, index=target_index, dtype=float); flag = pd.Series(9, index=target_index, dtype="int8")
    exact = target_index.intersection(src.index); out.loc[exact] = src.loc[exact].astype(float); flag.loc[exact] = 0
    if len(src) < 2: return out, flag
    src_times = src.index.view("i8"); target_times = target_index.view("i8"); positions = np.searchsorted(src_times, target_times)
    max_bridge_ns = int(source_step.value * 1.25)
    for i, pos in enumerate(positions):
        if flag.iat[i] == 0 or pos == 0 or pos >= len(src_times): continue
        left_t, right_t = src_times[pos - 1], src_times[pos]
        if right_t - left_t > max_bridge_ns: continue
        frac = (target_times[i] - left_t) / (right_t - left_t)
        out.iat[i] = float(src.iloc[pos - 1]) + frac * (float(src.iloc[pos]) - float(src.iloc[pos - 1])); flag.iat[i] = 1
    return out, flag



def _interval_hold_or_apportion(
    source: pd.Series,
    target_index: pd.DatetimeIndex,
    source_step: pd.Timedelta,
    target_step: pd.Timedelta,
    *,
    total: bool = False,
) -> Tuple[pd.Series, pd.Series]:
    """Align interval means/totals to a finer midpoint grid.

    Means are held across their native interval. Totals are apportioned across
    subintervals so the sum of the target records equals the source total.
    """
    src = pd.to_numeric(source, errors="coerce").dropna().sort_index()
    out = pd.Series(np.nan, index=target_index, dtype=float)
    flag = pd.Series(9, index=target_index, dtype="int8")
    if src.empty:
        return out, flag
    indexer = src.index.get_indexer(target_index, method="nearest", tolerance=source_step / 2)
    valid = indexer >= 0
    if valid.any():
        vals = src.to_numpy()[indexer[valid]].astype(float)
        if total:
            vals = vals * float(target_step / source_step)
        positions = np.flatnonzero(valid)
        out.iloc[positions] = vals
        flag.iloc[positions] = 1
        exact = np.isin(target_index[valid].view("i8"), src.index.view("i8"))
        flag.iloc[positions[exact]] = 0
    return out, flag


def saturation_vapor_pressure_kpa(temperature_c: pd.Series) -> pd.Series:
    return 0.6108 * np.exp((17.27 * temperature_c) / (temperature_c + 237.3))


def derive_vpd_kpa(temperature_c: pd.Series, relative_humidity_pct: pd.Series) -> pd.Series:
    return saturation_vapor_pressure_kpa(temperature_c) * (1.0 - relative_humidity_pct.clip(0, 100) / 100.0)


def _read_units_metadata(filepath: str) -> Dict[str, str]:
    try:
        xls = pd.ExcelFile(filepath)
        sheet = _choose_sheet(xls, ("Metadata", "Units", "VariableMetadata"), required=False)
        if not sheet: return {}
        meta = pd.read_excel(filepath, sheet_name=sheet)
        lookup = {str(c).strip().lower(): c for c in meta.columns}
        var_col = lookup.get("variable") or lookup.get("name") or lookup.get("column")
        unit_col = lookup.get("unit") or lookup.get("units")
        if not var_col or not unit_col: return {}
        units = {}
        for _, row in meta.iterrows():
            name = str(row[var_col]).strip(); unit = str(row[unit_col]).strip()
            normalized = _normalize_columns(pd.DataFrame(columns=[name])).columns[0]
            if name and unit and unit.lower() != "nan": units[normalized] = unit
        return units
    except Exception:
        logger.exception("Could not parse optional Metadata/Units sheet")
        return {}


def align_biomet_to_flux_grid(
    biomet: pd.DataFrame,
    target_index: pd.DatetimeIndex,
    target_step: pd.Timedelta,
    config: Optional[InputProcessingConfig] = None,
    units: Optional[Mapping[str, str]] = None,
) -> Tuple[pd.DataFrame, Optional[pd.Timedelta], int, pd.DataFrame, int]:
    if biomet.empty:
        return pd.DataFrame(index=target_index), None, 0, pd.DataFrame(), 0
    config = config or InputProcessingConfig()
    b = _normalize_columns(biomet)
    raw_datetime = build_datetime(b, "BiometData/Sheet2")
    invalid = int(raw_datetime.isna().sum())
    preliminary = raw_datetime.dropna().sort_values()
    source_step = infer_timestep(preliminary) if len(preliminary) >= 3 else target_step
    canonical, dst_invalid = canonicalize_timestamps(raw_datetime, source_step, config)
    b["datetime_input"] = raw_datetime; b["datetime"] = canonical
    b = b.dropna(subset=["datetime"])
    b = b.drop(columns=[c for c in ("date", "time", "hour", "timestamp") if c in b.columns], errors="ignore")
    b, qc_audit = screen_input_frame(b, [v for v in BIOMET_VARS if v in b], units, config.maximum_accepted_qc, config.strict_units)
    b, duplicate_count = _aggregate_duplicate_timestamps(b); b = b.set_index("datetime").sort_index()

    aligned = pd.DataFrame(index=target_index)
    for var in [v for v in BIOMET_VARS if v in b.columns]:
        series = pd.to_numeric(b[var], errors="coerce")
        if source_step > target_step:
            if var == "rain":
                values, flags = _interval_hold_or_apportion(series, target_index, source_step, target_step, total=True)
            elif var in {"sr", "ws", "rn", "g", "lw_in"}:
                values, flags = _interval_hold_or_apportion(series, target_index, source_step, target_step, total=False)
            else:
                values, flags = _safe_temporal_disaggregation(series, target_index, source_step)
            aligned[var] = values; aligned[f"{var}_align_flag"] = flags
        else:
            aligned[var] = series.reindex(target_index); aligned[f"{var}_align_flag"] = np.where(aligned[var].notna(), 0, 9).astype("int8")
        for suffix in ("raw", "input_qc", "screen_flag"):
            source_col = f"{var}_{suffix}"
            if source_col in b:
                aligned[source_col] = b[source_col].reindex(target_index)

    if "at" in aligned and "rh" in aligned:
        calculated = derive_vpd_kpa(aligned["at"], aligned["rh"])
        if "vpd" not in aligned: aligned["vpd"] = np.nan; aligned["vpd_align_flag"] = 9
        mask = aligned["vpd"].isna() & calculated.notna(); aligned.loc[mask, "vpd"] = calculated.loc[mask]; aligned.loc[mask, "vpd_align_flag"] = 2
    return aligned, source_step, duplicate_count, qc_audit, invalid + dst_invalid


def _choose_sheet(xls: pd.ExcelFile, candidates, required=True):
    lower = {name.lower(): name for name in xls.sheet_names}
    for candidate in candidates:
        if candidate.lower() in lower: return lower[candidate.lower()]
    if required: raise ValueError(f"Workbook is missing a required sheet. Expected one of: {', '.join(candidates)}.")
    return None


def read_reference_sheet(filepath: str, target_index: pd.DatetimeIndex, target_step: pd.Timedelta, config: Optional[InputProcessingConfig] = None, units: Optional[Mapping[str, str]] = None) -> pd.DataFrame:
    xls = pd.ExcelFile(filepath); sheet = _choose_sheet(xls, ("ReferenceData", "Reference", "Sheet3"), required=False)
    if sheet is None: return pd.DataFrame(index=target_index)
    raw = _normalize_columns(pd.read_excel(filepath, sheet_name=sheet))
    aligned, _, _, _, _ = align_biomet_to_flux_grid(raw, target_index, target_step, config=config, units=units)
    out = pd.DataFrame(index=target_index)
    for var in BIOMET_VARS:
        if var in aligned:
            out[f"{var}_ref"] = aligned[var]
            if f"{var}_align_flag" in aligned: out[f"{var}_ref_align_flag"] = aligned[f"{var}_align_flag"]
            out[f"{var}_ref_source"] = "uploaded_reference"
    return out


def read_xlsx_data(filepath: str, max_days: Optional[int] = None, processing_config: Optional[InputProcessingConfig] = None):
    if not os.path.exists(filepath): raise FileNotFoundError(filepath)
    config = processing_config or InputProcessingConfig()
    xls = pd.ExcelFile(filepath)
    flux_sheet = _choose_sheet(xls, ("FluxData", "Sheet1")); biomet_sheet = _choose_sheet(xls, ("BiometData", "Sheet2"))
    flux = _normalize_columns(pd.read_excel(filepath, sheet_name=flux_sheet)); biomet = _normalize_columns(pd.read_excel(filepath, sheet_name=biomet_sheet))
    units = _read_units_metadata(filepath)

    raw_flux_dt = build_datetime(flux, flux_sheet); invalid_flux = int(raw_flux_dt.isna().sum())
    preliminary = raw_flux_dt.dropna().sort_values(); step = infer_timestep(preliminary)
    canonical_flux, dst_invalid_flux = canonicalize_timestamps(raw_flux_dt, step, config)
    flux["datetime_input"] = raw_flux_dt; flux["datetime"] = canonical_flux; flux = flux.dropna(subset=["datetime"])
    flux = flux.drop(columns=[c for c in ("date", "time", "hour", "timestamp") if c in flux.columns], errors="ignore")
    original_rows = len(flux)
    flux, flux_qc_audit = screen_input_frame(flux, [v for v in FLUX_VARS if v in flux], units, config.maximum_accepted_qc, config.strict_units)
    flux, duplicate_flux = _aggregate_duplicate_timestamps(flux)
    full_index = pd.date_range(flux["datetime"].min(), flux["datetime"].max(), freq=step)
    flux = flux.set_index("datetime").reindex(full_index); flux.index.name = "datetime"
    if max_days is not None and (full_index[-1] - full_index[0]).days > max_days: raise ValueError(f"Dataset exceeds configured maximum of {max_days} days.")

    aligned, source_step, duplicate_biomet, biomet_qc_audit, invalid_biomet = align_biomet_to_flux_grid(biomet, full_index, step, config=config, units=units)
    reference = read_reference_sheet(filepath, full_index, step, config=config, units=units)
    merged = pd.concat([flux, aligned, reference], axis=1).reset_index()

    flux_vars = [v for v in FLUX_VARS if v in merged.columns and merged[v].notna().any()]
    biomet_vars = [v for v in BIOMET_VARS if v in merged.columns and merged[v].notna().any()]
    if not flux_vars: raise ValueError("No usable ET, H, or LE measurements remained after input QA/QC screening.")
    if not biomet_vars and reference.empty: raise ValueError("No usable biomet variables were found after input QA/QC screening.")

    qc_audit = pd.concat([flux_qc_audit.assign(sheet="FluxData"), biomet_qc_audit.assign(sheet="BiometData")], ignore_index=True)
    merged.attrs["input_qc_audit"] = qc_audit
    merged.attrs["declared_units"] = units
    metadata = ProcessingMetadata(
        timestep_minutes=step.total_seconds()/60, start_datetime=full_index[0], end_datetime=full_index[-1],
        flux_rows_original=original_rows, rows_regularized=len(full_index), duplicate_flux_timestamps=duplicate_flux,
        duplicate_biomet_timestamps=duplicate_biomet, biomet_native_timestep_minutes=(source_step.total_seconds()/60 if source_step else None),
        timezone=config.timezone, time_basis=config.time_basis, timestamp_convention=config.timestamp_convention,
        invalid_flux_timestamps=invalid_flux + dst_invalid_flux, invalid_biomet_timestamps=invalid_biomet,
        dst_ambiguous_or_nonexistent=dst_invalid_flux, qc_audit_records=len(qc_audit),
    )
    logger.info("Loaded %s rows at %.1f min resolution; canonical time=local standard midpoint", len(merged), metadata.timestep_minutes)
    return merged, flux_vars, biomet_vars, metadata


# ---------------------------------------------------------------------------
# EddyPro full-output text ingestion
# ---------------------------------------------------------------------------

EDDYPRO_COLUMN_CANDIDATES = {
    "LE": ("LE",),
    "H": ("H",),
    "ET": ("ET",),
    "NEE": ("co2_flux", "NEE", "FC", "FCO2"),
    "sr": ("SWIN_1_1_1", "RG_1_1_1", "SW_IN", "SWIN", "Rg"),
    "at": ("TA_1_1_1", "air_temperature", "TA", "Tair"),
    "rh": ("RH_1_1_1", "RH", "relative_humidity"),
    "vpd": ("VPD", "vpd"),
    "ws": ("wind_speed", "WS", "WindSpd"),
    "rain": ("P_RAIN_1_1_1", "rain", "precip"),
    "pa": ("air_pressure", "PA", "pressure"),
    "rn": ("RN_1_1_1", "Rn", "net_radiation"),
    "lw_in": ("LWIN_1_1_1", "LW_IN", "longwave_in"),
}


def _eddypro_unit_to_canonical(variable: str, unit: str) -> tuple[float, float, str]:
    """Return multiplicative factor, additive offset and canonical unit.

    EddyPro's DATAU row uses compact strings such as ``W+1m-2`` and
    ``mm+1hour-1``.  The mapping below is intentionally explicit so units are
    never guessed silently.
    """
    text = str(unit or "").strip().lower().replace("µ", "u")
    compact = text.replace(" ", "").replace("^", "")
    if variable in {"LE", "H", "sr", "rn", "g", "lw_in"}:
        if "w" in compact and "m-2" in compact or compact in {"w/m2", "wm-2", "wm2"}:
            return 1.0, 0.0, "W m-2"
    if variable == "at":
        if compact in {"k", "kelvin"} or text == "[k]":
            return 1.0, -273.15, "degC"
        if "c" in compact:
            return 1.0, 0.0, "degC"
    if variable == "rh":
        return 1.0, 0.0, "%"
    if variable == "vpd":
        if "pa" in compact and "kpa" not in compact:
            return 0.001, 0.0, "kPa"
        return 1.0, 0.0, "kPa"
    if variable == "ws":
        return 1.0, 0.0, "m s-1"
    if variable == "pa":
        if "pa" in compact and "kpa" not in compact:
            return 0.001, 0.0, "kPa"
        return 1.0, 0.0, "kPa"
    if variable == "rain":
        # The attached SmartFlux biomet precipitation field is declared [m].
        if compact in {"m", "[m]"} or text in {"m", "[m]"}:
            return 1000.0, 0.0, "mm interval-1"
        if "mm" in compact:
            return 1.0, 0.0, "mm interval-1"
    if variable == "NEE":
        if "umol" in compact and "m-2" in compact:
            return 1.0, 0.0, "umol m-2 s-1"
        if "mmol" in compact and "m-2" in compact:
            return 1000.0, 0.0, "umol m-2 s-1"
        return 1.0, 0.0, "umol m-2 s-1"
    if variable == "ET":
        # EddyPro ET is normally mm h-1. It is retained for comparison only;
        # final ET is derived from the final LE series.
        if "mm" in compact and "hour" in compact:
            return 1.0, 0.0, "mm h-1"
        return 1.0, 0.0, "mm h-1"
    if variable == "swc":
        return 1.0, 0.0, "m3 m-3"
    return 1.0, 0.0, str(unit or "unknown")


def _pick_existing_column(frame: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    lookup = {str(c).strip().lower(): c for c in frame.columns}
    for candidate in candidates:
        if candidate.lower() in lookup:
            return lookup[candidate.lower()]
    return None


def _pick_best_numeric_column(frame: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    """Choose the candidate containing the most usable numeric observations.

    EddyPro/SmartFlux files can contain both logger and analyzer versions of the
    same meteorological variable.  A logger column may exist but be entirely
    empty (for example ``TA_1_1_1`` in the Esparto input), while a later
    candidate such as ``air_temperature`` is complete.  Selecting only the first
    matching header silently discards the valid series and can prevent long-gap
    machine-learning predictions.
    """
    lookup = {str(c).strip().lower(): c for c in frame.columns}
    ranked = []
    for order, candidate in enumerate(candidates):
        column = lookup.get(str(candidate).strip().lower())
        if column is None:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        ranked.append((int(values.notna().sum()), int(values.nunique(dropna=True)), -order, column))
    if not ranked:
        return None
    return max(ranked)[-1]


def _slotwise_robust_outlier_mask(
    values: pd.Series,
    datetimes: pd.Series,
    variable: str,
    threshold: float = 8.0,
) -> pd.Series:
    """Conservative low-frequency spike screen using same-time-of-day donors.

    EddyPro already performs high-frequency despiking. This second-stage screen
    only identifies isolated, extreme half-hourly values relative to the same
    time of day in a moving 31-observation window. A generous MAD threshold and
    an absolute-deviation floor prevent normal diurnal and irrigation signals
    from being removed.
    """
    y = pd.to_numeric(values, errors="coerce")
    dt = pd.to_datetime(datetimes, errors="coerce")
    mask = pd.Series(False, index=y.index)
    if y.notna().sum() < 100:
        return mask
    floors = {"LE": 300.0, "H": 300.0, "rn": 250.0, "g": 150.0}
    floor = floors.get(variable, np.inf)
    if not np.isfinite(floor):
        return mask
    slot = dt.dt.hour * 60 + dt.dt.minute
    for _, idx in slot.groupby(slot).groups.items():
        s = y.loc[idx]
        if s.notna().sum() < 15:
            continue
        med = s.rolling(31, center=True, min_periods=11).median()
        abs_dev = (s - med).abs()
        mad = abs_dev.rolling(31, center=True, min_periods=11).median()
        robust_sigma = 1.4826 * mad
        local = (abs_dev > threshold * robust_sigma) & (abs_dev > floor) & robust_sigma.notna()
        mask.loc[idx] = local.fillna(False)
    return mask


def _convert_eddypro_series(frame: pd.DataFrame, source: str, variable: str, units: Mapping[str, str]) -> tuple[pd.Series, str, str]:
    raw = pd.to_numeric(frame[source], errors="coerce")
    factor, offset, canonical = _eddypro_unit_to_canonical(variable, units.get(source, ""))
    return raw * factor + offset, canonical, f"{source}: factor={factor:g}, offset={offset:g}"


def read_eddypro_txt(
    filepath: str,
    max_days: Optional[int] = None,
    processing_config: Optional[InputProcessingConfig] = None,
):
    """Read an EddyPro full-output tab-delimited text file directly.

    The first three record types are interpreted as DATAH (column names), DATAU
    (units), and DATA (observations). EddyPro output timestamps are treated as
    interval-end timestamps and converted to a regular local-standard-time
    midpoint grid while preserving both end and midpoint timestamps.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(filepath)
    config = processing_config or InputProcessingConfig(timestamp_convention="end")

    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as handle:
        header_line = handle.readline().rstrip("\r\n")
        units_line = handle.readline().rstrip("\r\n")
    headers = header_line.split("\t")
    unit_values = units_line.split("\t")
    if not headers or headers[0].strip().upper() != "DATAH":
        raise ValueError("The uploaded TXT is not an EddyPro full-output file: DATAH header was not found.")
    if not unit_values or unit_values[0].strip().upper() != "DATAU":
        raise ValueError("The uploaded TXT is not an EddyPro full-output file: DATAU unit row was not found.")
    headers[0] = "record_type"
    units = {name: (unit_values[i] if i < len(unit_values) else "") for i, name in enumerate(headers)}

    raw = pd.read_csv(
        filepath,
        sep="\t",
        header=0,
        skiprows=[1],
        low_memory=False,
        na_values=["NaN", "nan", "NAN", "-9999", "-9999.0", ""],
    )
    raw = raw.rename(columns={raw.columns[0]: "record_type"})
    raw = raw[raw["record_type"].astype(str).str.upper().eq("DATA")].copy()
    if raw.empty:
        raise ValueError("No DATA records were found in the EddyPro TXT file.")

    date_col = _pick_existing_column(raw, ("date",))
    time_col = _pick_existing_column(raw, ("time",))
    if date_col is None or time_col is None:
        raise ValueError("EddyPro TXT must contain date and time columns.")
    datetime_end = pd.to_datetime(
        raw[date_col].astype(str).str.strip() + " " + raw[time_col].astype(str).str.strip(),
        errors="coerce",
    )
    invalid_timestamps = int(datetime_end.isna().sum())
    raw["datetime_end_input"] = datetime_end
    preliminary = datetime_end.dropna().sort_values().drop_duplicates()
    step = infer_timestep(preliminary)
    # EddyPro full-output timestamps always refer to the end of each interval.
    eddy_config = InputProcessingConfig(
        timezone=config.timezone,
        time_basis=config.time_basis,
        timestamp_convention="end",
        maximum_accepted_qc=config.maximum_accepted_qc,
        strict_units=False,
    )
    canonical, dst_invalid = canonicalize_timestamps(datetime_end, step, eddy_config)
    raw["datetime"] = canonical
    raw = raw.dropna(subset=["datetime"]).copy()

    out = pd.DataFrame(index=raw.index)
    out["datetime"] = raw["datetime"]
    out["datetime_end_input"] = raw["datetime_end_input"]
    filename_col = _pick_existing_column(raw, ("filename",))
    if filename_col:
        out["eddypro_filename"] = raw[filename_col].astype(str)
        out["eddypro_not_enough_data"] = raw[filename_col].astype(str).str.lower().eq("not_enough_data")
    else:
        out["eddypro_filename"] = ""
        out["eddypro_not_enough_data"] = False

    audit_rows = []
    # Fluxes and standard single-source biomet variables.
    for variable, candidates in EDDYPRO_COLUMN_CANDIDATES.items():
        source = _pick_best_numeric_column(raw, candidates)
        if source is None:
            continue
        converted, canonical_unit, conversion = _convert_eddypro_series(raw, source, variable, units)
        if variable == "ET":
            converted = converted * float(step / pd.Timedelta(hours=1))
            canonical_unit = "mm interval-1"
            conversion += f"; hourly rate multiplied by {float(step / pd.Timedelta(hours=1)):.6g} h"
        out[f"{variable}_raw"] = pd.to_numeric(raw[source], errors="coerce")
        out[f"{variable}_source_column"] = source
        out[f"{variable}_original_unfiltered"] = converted

        if variable == "NEE":
            qc_col = _pick_existing_column(raw, ("qc_co2_flux", "qc_NEE", "NEE_qc", "FC_QC"))
        else:
            qc_col = _pick_existing_column(raw, (f"qc_{variable}", f"{variable}_qc")) if variable in {"LE", "H"} else None
        qc_values = pd.to_numeric(raw[qc_col], errors="coerce") if qc_col else pd.Series(np.nan, index=raw.index)
        out[f"{variable}_input_qc"] = qc_values
        qc_ok = qc_values.le(config.maximum_accepted_qc) | qc_values.isna()
        if variable in {"LE", "H", "NEE"} and qc_col:
            # EddyPro numeric quality flags are explicit; missing QC is not accepted.
            qc_ok = qc_values.notna() & qc_values.le(config.maximum_accepted_qc)

        lower, upper = ((-500.0, 500.0) if variable == "NEE" else PHYSICAL_LIMITS.get(variable, (-np.inf, np.inf)))
        physical_ok = converted.between(lower, upper, inclusive="both") | converted.isna()
        not_enough = out["eddypro_not_enough_data"].astype(bool)
        accepted = converted.notna() & qc_ok & physical_ok & ~not_enough
        robust_outlier = _slotwise_robust_outlier_mask(converted.where(accepted), out["datetime"], variable)
        accepted &= ~robust_outlier

        flag = pd.Series("accepted", index=raw.index, dtype=object)
        flag.loc[converted.isna()] = "raw_missing"
        flag.loc[not_enough] = "not_enough_data"
        flag.loc[converted.notna() & ~physical_ok] = "outside_physical_screen"
        flag.loc[converted.notna() & physical_ok & ~qc_ok] = "rejected_by_eddypro_qc"
        flag.loc[robust_outlier] = "robust_outlier"
        out[f"{variable}_screen_flag"] = flag
        out[variable] = converted.where(accepted)
        audit_rows.append({
            "variable": variable,
            "source_column": source,
            "declared_unit": units.get(source, "not declared") or "not declared",
            "canonical_unit": canonical_unit,
            "conversion": conversion,
            "quality_column": qc_col or "none",
            "maximum_accepted_qc": config.maximum_accepted_qc,
            "n_raw_numeric": int(converted.notna().sum()),
            "n_accepted": int(out[variable].notna().sum()),
            "n_raw_missing": int(converted.isna().sum()),
            "n_rejected_qc": int((flag == "rejected_by_eddypro_qc").sum()),
            "n_rejected_physical": int((flag == "outside_physical_screen").sum()),
            "n_rejected_robust_outlier": int((flag == "robust_outlier").sum()),
            "n_not_enough_data": int((flag == "not_enough_data").sum()),
            "sheet": "EddyPro TXT",
        })

    # Preserve every soil sensor separately. Sensor activity is assessed later
    # at each interval during the storage-correction step; users do not need to
    # identify historical active periods manually.
    import re as _re
    def _soil_sensor_id(column: str, prefix: str) -> int | None:
        match = _re.match(rf"^{prefix}_(\d+)(?:_|$)", str(column), flags=_re.I)
        return int(match.group(1)) if match else None

    shf_cols = [c for c in raw.columns if _soil_sensor_id(c, "SHF") is not None]
    shf_values = []
    sensor_additions = {}
    for c in shf_cols:
        sensor_id = _soil_sensor_id(c, "SHF")
        values, _, conversion = _convert_eddypro_series(raw, c, "g", units)
        sensor_additions[f"shf_{sensor_id}"] = values.to_numpy()
        sensor_additions[f"shf_{sensor_id}_source_column"] = np.repeat(c, len(raw))
        accepted_values = values.where(values.between(-500.0, 500.0, inclusive="both"))
        shf_values.append(accepted_values)
        audit_rows.append({
            "variable": f"shf_{sensor_id}", "source_column": c,
            "declared_unit": units.get(c, "not declared") or "not declared",
            "canonical_unit": "W m-2", "conversion": conversion,
            "quality_column": "none", "maximum_accepted_qc": np.nan,
            "n_raw_numeric": int(values.notna().sum()),
            "n_accepted": int(accepted_values.notna().sum()),
            "n_raw_missing": int(values.isna().sum()), "n_rejected_qc": 0,
            "n_rejected_physical": int((values.notna() & accepted_values.isna()).sum()),
            "n_rejected_robust_outlier": 0, "n_not_enough_data": 0, "sheet": "EddyPro TXT",
        })
    if shf_values:
        plate_matrix = pd.concat(shf_values, axis=1)
        plate_median = plate_matrix.median(axis=1, skipna=True)
        sensor_additions.update({
            "g_plate_median_raw": plate_median.to_numpy(),
            "g_plate_sensor_count": plate_matrix.notna().sum(axis=1).to_numpy(),
            "g_original_unfiltered": plate_median.to_numpy(),
            "g": plate_median.to_numpy(),
            "g_screen_flag": np.where(plate_median.notna(), "accepted_uncorrected_plate_median", "raw_missing"),
        })

    ts_cols = [c for c in raw.columns if _soil_sensor_id(c, "TS") is not None]
    ts_values = []
    for c in ts_cols:
        sensor_id = _soil_sensor_id(c, "TS")
        values, _, _ = _convert_eddypro_series(raw, c, "at", units)
        sensor_additions[f"ts_{sensor_id}"] = values.to_numpy()
        sensor_additions[f"ts_{sensor_id}_source_column"] = np.repeat(c, len(raw))
        ts_values.append(values.where(values.between(-50.0, 80.0, inclusive="both")))
    if ts_values:
        sensor_additions["soil_temperature_representative"] = pd.concat(ts_values, axis=1).median(axis=1, skipna=True).to_numpy()

    swc_cols = [c for c in raw.columns if _soil_sensor_id(c, "SWC") is not None]
    swc_values = []
    for c in swc_cols:
        sensor_id = _soil_sensor_id(c, "SWC")
        values = pd.to_numeric(raw[c], errors="coerce")
        sensor_additions[f"swc_{sensor_id}"] = values.to_numpy()
        sensor_additions[f"swc_{sensor_id}_source_column"] = np.repeat(c, len(raw))
        swc_values.append(values.where(values.between(0.0, 0.8, inclusive="both")))
    if swc_values:
        matrix = pd.concat(swc_values, axis=1)
        swc_median = matrix.median(axis=1, skipna=True)
        sensor_additions.update({
            "swc_original_unfiltered": swc_median.to_numpy(),
            "swc_sensor_count": matrix.notna().sum(axis=1).to_numpy(),
            "swc": swc_median.to_numpy(),
            "swc_screen_flag": np.where(swc_median.isna(), "raw_missing", "accepted"),
            "soil_water_content_representative": swc_median.to_numpy(),
        })
    if sensor_additions:
        out = pd.concat([out.reset_index(drop=True), pd.DataFrame(sensor_additions)], axis=1).copy()

    # Preserve a few optional turbulence variables as internal diagnostics only.
    turbulence_sources = {
        "ustar": ("u*", "ustar", "u_star"),
        "obukhov_length": ("L", "obukhov_length"),
        "wind_dir": ("wind_dir",),
        "wind_speed_footprint": ("wind_speed",),
        "v_var": ("v_var",),
    }
    turbulence_additions = {}
    for target_name, candidates in turbulence_sources.items():
        source = _pick_existing_column(raw, candidates)
        if source is not None:
            turbulence_additions[target_name] = pd.to_numeric(raw[source], errors="coerce").to_numpy()
            turbulence_additions[f"{target_name}_source_column"] = np.repeat(source, len(raw))
    if "v_var" in turbulence_additions:
        turbulence_additions["sigma_v"] = np.sqrt(pd.Series(turbulence_additions["v_var"]).clip(lower=0)).to_numpy()
    if turbulence_additions:
        out = pd.concat([out.reset_index(drop=True), pd.DataFrame(turbulence_additions)], axis=1).copy()

    # Duplicate timestamps are resolved by averaging numeric values; valid DATA
    # rows naturally dominate all-NaN not_enough_data rows.
    out, duplicate_count = _aggregate_duplicate_timestamps(out, precipitation_columns=("rain",))
    if out.empty:
        raise ValueError("No valid EddyPro timestamps remained after parsing.")
    full_index = pd.date_range(out["datetime"].min(), out["datetime"].max(), freq=step)
    if max_days is not None and (full_index[-1] - full_index[0]).days > max_days:
        raise ValueError(f"Dataset exceeds configured maximum of {max_days} days.")
    regular = out.set_index("datetime").reindex(full_index)
    regular.index.name = "datetime"
    regular = regular.reset_index()
    regular["datetime_midpoint"] = regular["datetime"]
    regular["datetime_end"] = regular["datetime"] + step / 2

    flux_vars = [v for v in ("LE", "H", "ET", "NEE") if v in regular and f"{v}_original_unfiltered" in regular and regular[f"{v}_original_unfiltered"].notna().any()]
    biomet_vars = [v for v in BIOMET_VARS if v in regular and regular[v].notna().any()]
    if "LE" not in flux_vars and "H" not in flux_vars:
        raise ValueError("No usable LE or H columns were found in the EddyPro TXT file.")
    qc_audit = pd.DataFrame(audit_rows)
    regular.attrs["input_qc_audit"] = qc_audit
    regular.attrs["declared_units"] = units
    regular.attrs["input_format"] = "EddyPro full-output TXT"
    metadata = ProcessingMetadata(
        timestep_minutes=step.total_seconds() / 60.0,
        start_datetime=full_index[0], end_datetime=full_index[-1],
        flux_rows_original=len(raw), rows_regularized=len(full_index),
        duplicate_flux_timestamps=duplicate_count, duplicate_biomet_timestamps=0,
        biomet_native_timestep_minutes=step.total_seconds() / 60.0,
        timezone=config.timezone, time_basis=config.time_basis,
        timestamp_convention="end", canonical_time_basis="local_standard_midpoint",
        invalid_flux_timestamps=invalid_timestamps + dst_invalid,
        invalid_biomet_timestamps=0, dst_ambiguous_or_nonexistent=dst_invalid,
        qc_audit_records=len(qc_audit),
    )
    logger.info(
        "Loaded EddyPro TXT: %s regularized rows at %.1f min; %s duplicate records",
        len(regular), metadata.timestep_minutes, duplicate_count,
    )
    return regular, flux_vars, biomet_vars, metadata


def read_input_data(
    filepath: str,
    max_days: Optional[int] = None,
    processing_config: Optional[InputProcessingConfig] = None,
):
    """Dispatch to EddyPro TXT or legacy XLSX ingestion."""
    suffix = os.path.splitext(str(filepath))[1].lower()
    if suffix in {".txt", ".tsv", ".csv"}:
        return read_eddypro_txt(filepath, max_days=max_days, processing_config=processing_config)
    if suffix == ".xlsx":
        return read_xlsx_data(filepath, max_days=max_days, processing_config=processing_config)
    raise ValueError("Supported input formats are EddyPro .txt/.tsv files and legacy .xlsx workbooks.")
