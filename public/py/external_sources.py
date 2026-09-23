"""Local CIMIS reference-data ingestion and temporal alignment.

This module supports the hourly CIMIS CSV export used by the web app. Hourly
records are interpreted as interval-end values in Pacific Standard Time and are
aligned to the regular EddyPro midpoint grid. Hourly means are held across the
finer target intervals; hourly precipitation totals are apportioned so totals
are conserved.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from data_processor import infer_timestep


@dataclass(frozen=True)
class SiteConfig:
    latitude: float
    longitude: float
    timezone: str = "America/Los_Angeles"
    elevation_m: Optional[float] = None


@dataclass(frozen=True)
class ExternalSourceConfig:
    use_cimis: bool = True
    cimis_app_key: Optional[str] = None
    cimis_local_path: Optional[str] = None
    cimis_radius_km: float = 100.0
    cimis_station_limit: int = 5
    cimis_station_ids: Optional[object] = None
    min_overlap: int = 100
    calibration_folds: int = 5
    cimis_accepted_qc_codes: object = ""
    external_timestamp_convention: str = "end"
    allow_uncalibrated_external: bool = False


CIMIS_VARIABLES = {
    # Pandas de-duplicates the repeated ``qc`` headers as qc, qc.1, qc.2, ...
    # following the native column order in the CIMIS hourly export.
    "eto": ("ETo (mm)", "qc", "mm h-1 total", True),
    "rain": ("Precip (mm)", "qc.1", "mm h-1 total", True),
    "sr": ("Sol Rad (W/sq.m)", "qc.2", "W m-2", False),
    "rn": ("Net Rad (W/sq.m)", "qc.3", "W m-2", False),
    "vp": ("Vap Pres (kPa)", "qc.4", "kPa", False),
    "at": ("Air Temp (C)", "qc.5", "degC", False),
    "rh": ("Rel Hum (%)", "qc.6", "%", False),
    "dew": ("Dew Point (C)", "qc.7", "degC", False),
    "ws": ("Wind Speed (m/s)", "qc.8", "m s-1", False),
    "wind_dir": ("Wind Dir (0-360)", "qc.9", "degree", False),
    "soil_temp": ("Soil Temp (C)", "qc.10", "degC", False),
}


def _normalised_station_filter(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
    elif isinstance(value, Iterable):
        parts = list(value)
    else:
        parts = [value]
    return {str(item).strip() for item in parts if str(item).strip()}


def _accepted_qc_codes(value: object) -> Optional[set[str]]:
    """Return accepted codes, or None to retain all non-missing numeric values.

    The web form normally leaves this setting blank. In that case the numeric
    value is retained and the original CIMIS flag remains represented in the
    source inventory. A supplied comma-separated list applies an explicit
    filter and always accepts blank flags as unflagged records.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parts = text.replace(";", ",").split(",")
    elif isinstance(value, Iterable):
        parts = list(value)
    else:
        parts = [value]
    return {"", *(str(item).strip().upper() for item in parts if str(item).strip())}


def _parse_cimis_interval_start(frame: pd.DataFrame) -> pd.Series:
    if "Date" not in frame or "Hour (PST)" not in frame:
        raise ValueError("CIMIS CSV must contain 'Date' and 'Hour (PST)' columns.")
    date = pd.to_datetime(frame["Date"], errors="coerce").dt.normalize()
    hour_code = pd.to_numeric(frame["Hour (PST)"], errors="coerce")
    hour = np.floor(hour_code / 100.0)
    hour = pd.Series(hour, index=frame.index).where(hour_code.ne(2400), 24.0)
    interval_end = date + pd.to_timedelta(hour, unit="h")
    return interval_end - pd.Timedelta(hours=1)


def _select_station(frame: pd.DataFrame, allowed_ids: set[str], target_start: pd.Timestamp, target_end: pd.Timestamp) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    station_col = "Stn Id" if "Stn Id" in frame else None
    name_col = "Stn Name" if "Stn Name" in frame else None
    if station_col is None:
        frame = frame.copy()
        frame["Stn Id"] = "CIMIS"
        station_col = "Stn Id"
    frame[station_col] = frame[station_col].astype(str).str.strip()
    if allowed_ids:
        filtered = frame[frame[station_col].isin(allowed_ids)].copy()
        if filtered.empty:
            raise ValueError("None of the requested CIMIS station IDs were found in the uploaded CSV.")
        frame = filtered

    rows = []
    for station_id, group in frame.groupby(station_col, sort=False):
        period = group[group["_interval_start"].between(target_start.floor("h"), target_end.floor("h"), inclusive="both")]
        available = 0
        possible = 0
        for value_col, _, _, _ in CIMIS_VARIABLES.values():
            if value_col in period:
                values = pd.to_numeric(period[value_col], errors="coerce")
                available += int(values.notna().sum())
                possible += int(len(values))
        completeness = float(available / possible) if possible else 0.0
        station_name = str(group[name_col].iloc[0]).strip() if name_col and name_col in group else station_id
        rows.append({
            "station_id": station_id,
            "station_name": station_name,
            "records_in_target_period": int(len(period)),
            "available_variable_values": int(available),
            "possible_variable_values": int(possible),
            "completeness_fraction": completeness,
        })
    ranking = pd.DataFrame(rows).sort_values(
        ["completeness_fraction", "records_in_target_period"], ascending=[False, False]
    ).reset_index(drop=True)
    if ranking.empty:
        raise ValueError("No CIMIS station records overlap the EddyPro period.")
    selected = str(ranking.iloc[0]["station_id"])
    return selected, frame[frame[station_col].eq(selected)].copy(), ranking


def _align_hourly_to_target(
    source: pd.DataFrame,
    target_index: pd.DatetimeIndex,
    target_step: pd.Timedelta,
    variable: str,
    value_column: str,
    qc_column: str,
    accepted_codes: Optional[set[str]],
    total: bool,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    values = pd.to_numeric(source[value_column], errors="coerce") if value_column in source else pd.Series(np.nan, index=source.index)
    flags = source[qc_column].astype(str).str.strip().str.upper() if qc_column in source else pd.Series("", index=source.index)
    if accepted_codes is not None:
        values = values.where(flags.isin(accepted_codes))

    hourly = pd.DataFrame({"interval_start": source["_interval_start"], "value": values, "flag": flags})
    hourly = hourly.dropna(subset=["interval_start"]).sort_values("interval_start")
    if hourly["interval_start"].duplicated().any():
        hourly = hourly.groupby("interval_start", as_index=False).agg(value=("value", "mean"), flag=("flag", "first"))
    hourly = hourly.set_index("interval_start")

    target_start = target_index.floor("h")
    aligned = pd.Series(target_start.map(hourly["value"]), index=target_index, dtype=float)
    source_flag = pd.Series(target_start.map(hourly["flag"]), index=target_index, dtype=object)
    if total:
        fraction = float(target_step / pd.Timedelta(hours=1))
        aligned = aligned * fraction

    # 0 = exact native interval match; 1 = safe temporal disaggregation/hold;
    # 9 = no source value available.
    exact_native = target_step == pd.Timedelta(hours=1)
    align_flag = pd.Series(np.where(aligned.notna(), 0 if exact_native else 1, 9), index=target_index, dtype="int8")
    return aligned, align_flag, source_flag


def retrieve_external_reference(
    data: pd.DataFrame,
    site_config: SiteConfig,
    config: ExternalSourceConfig,
    audit_path: Optional[Path] = None,
):
    """Read uploaded CIMIS CSV and align the best station to ``data``."""
    if not config.use_cimis:
        raise ValueError("CIMIS is disabled.")
    if not config.cimis_local_path:
        raise ValueError("An uploaded CIMIS CSV is required.")
    path = Path(config.cimis_local_path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    raw = pd.read_csv(path, dtype=str, keep_default_na=False, low_memory=False)
    raw["_interval_start"] = _parse_cimis_interval_start(raw)
    raw = raw.dropna(subset=["_interval_start"]).copy()
    if raw.empty:
        raise ValueError("No valid CIMIS timestamps were found.")

    dt = pd.DatetimeIndex(pd.to_datetime(data["datetime"], errors="coerce"))
    if dt.isna().any() or len(dt) < 2:
        raise ValueError("The EddyPro target grid has invalid timestamps.")
    target_step = infer_timestep(dt)
    station_ids = _normalised_station_filter(config.cimis_station_ids)
    selected_id, selected, ranking = _select_station(raw, station_ids, dt.min(), dt.max())
    station_name = str(selected["Stn Name"].iloc[0]).strip() if "Stn Name" in selected else selected_id
    accepted_codes = _accepted_qc_codes(config.cimis_accepted_qc_codes)

    external = pd.DataFrame(index=data.index)
    external["datetime"] = dt.to_numpy()
    inventory_rows = []
    for variable, (value_column, qc_column, units, total) in CIMIS_VARIABLES.items():
        if value_column not in selected:
            continue
        aligned, align_flag, original_flag = _align_hourly_to_target(
            selected, dt, target_step, variable, value_column, qc_column,
            accepted_codes, total,
        )
        external[f"{variable}_ref"] = aligned.to_numpy()
        external[f"{variable}_ref_align_flag"] = align_flag.to_numpy()
        external[f"{variable}_ref_source"] = f"CIMIS_{selected_id}"
        inventory_rows.append({
            "station_id": selected_id,
            "station_name": station_name,
            "variable": variable,
            "source_column": value_column,
            "units": units,
            "native_records": int(pd.to_numeric(selected[value_column], errors="coerce").notna().sum()),
            "aligned_records": int(aligned.notna().sum()),
            "aligned_completeness_fraction": float(aligned.notna().mean()),
            "qc_filter": "all numeric" if accepted_codes is None else ",".join(sorted(accepted_codes)),
            "observed_qc_codes": ",".join(sorted(set(original_flag.dropna().astype(str)))),
        })

    external.attrs["selected_station"] = f"{station_name}:{selected_id}"
    external.attrs["selected_station_id"] = selected_id
    source_inventory = pd.DataFrame(inventory_rows)

    if audit_path is not None:
        audit = Path(audit_path)
        audit.mkdir(parents=True, exist_ok=True)
        ranking.to_csv(audit / "station_ranking.csv", index=False)
        source_inventory.to_csv(audit / "source_inventory.csv", index=False)

    return external, ranking, source_inventory
