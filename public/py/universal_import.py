"""FluxGapFill Phase-1 universal import and project-capability foundation.

Scope
-----
This module starts from already processed flux/environmental time series.
It does not process high-frequency sonic/IRGA data into eddy-covariance fluxes.

Supported primary/supplemental containers:
* EddyPro full-output TXT (specialized reader remains authoritative)
* CIMIS hourly CSV
* Campbell Scientific TOA5
* AmeriFlux / FLUXNET-style CSV
* generic CSV / TSV / delimited TXT
* Excel-compatible workbooks readable by pandas+python-calamine

The importer never silently invents a mapping. It returns suggestions with
confidence and allows the browser UI to override column and unit choices.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from data_processor import InputProcessingConfig, canonicalize_timestamps, infer_timestep
from scientific_qc import PHYSICAL_LIMITS

CANONICAL_UNITS: Dict[str, str] = {
    "LE": "W m-2", "H": "W m-2", "NEE": "umol m-2 s-1",
    "sr": "W m-2", "rn": "W m-2", "g": "W m-2",
    "at": "degC", "rh": "%", "vpd": "kPa", "ws": "m s-1",
    "wind_dir": "degree", "rain": "mm interval-1", "pa": "kPa",
    "swc": "m3 m-3", "soil_temperature": "degC", "ustar": "m s-1",
    "obukhov_length": "m", "sigma_v": "m s-1", "ET": "mm interval-1",
    "ETo": "mm interval-1", "vp": "kPa", "dew_point": "degC",
}

ROLE_LABELS: Dict[str, str] = {
    "timestamp": "Timestamp", "date": "Date", "time": "Time",
    "LE": "Latent heat flux (LE)", "H": "Sensible heat flux (H)",
    "NEE": "Net ecosystem exchange / CO2 flux",
    "qc_LE": "LE quality flag", "qc_H": "H quality flag", "qc_NEE": "NEE quality flag",
    "sr": "Incoming shortwave radiation", "rn": "Net radiation", "g": "Soil heat flux",
    "at": "Air temperature", "rh": "Relative humidity", "vpd": "VPD",
    "ws": "Wind speed", "wind_dir": "Wind direction", "rain": "Precipitation",
    "pa": "Air pressure", "swc": "Soil water content", "soil_temperature": "Soil temperature",
    "ustar": "Friction velocity (u*)", "obukhov_length": "Monin-Obukhov length",
    "sigma_v": "Lateral wind SD (sigma-v)", "ET": "Measured ET", "ETo": "Reference ET",
    "vp": "Vapor pressure", "dew_point": "Dew point",
}

ROLE_GROUPS = {
    "Time": ("timestamp", "date", "time"),
    "Fluxes": ("LE", "H", "NEE", "qc_LE", "qc_H", "qc_NEE"),
    "Radiation / energy": ("sr", "rn", "g"),
    "Meteorology": ("at", "rh", "vpd", "ws", "wind_dir", "rain", "pa", "vp", "dew_point", "ETo"),
    "Soil / turbulence": ("swc", "soil_temperature", "ustar", "obukhov_length", "sigma_v", "ET"),
}

ALIASES: Dict[str, Sequence[str]] = {
    "timestamp": ("timestamp", "datetime", "date_time", "time_stamp", "timestamp_start", "timestamp_end", "TIMESTAMP"),
    "date": ("date", "day", "DATE"),
    "time": ("time", "hour", "time_end", "Hour_End", "TIME"),
    "LE": ("LE", "LE_F", "LE_F_MDS", "LE_1_1_1", "LE_Wm2", "latent_heat_flux", "latentheat", "le_avg"),
    "H": ("H", "H_F", "H_F_MDS", "H_1_1_1", "H_Wm2", "sensible_heat_flux", "sensibleheat", "h_avg"),
    "NEE": ("NEE", "NEE_VUT_REF", "NEE_CUT_REF", "FC", "FCO2", "co2_flux", "CO2_flux", "fc_avg"),
    "qc_LE": ("qc_LE", "LE_QC", "LE_qc", "qc_le", "LE_SSITC_TEST"),
    "qc_H": ("qc_H", "H_QC", "H_qc", "qc_h", "H_SSITC_TEST"),
    "qc_NEE": ("qc_NEE", "NEE_QC", "FC_QC", "qc_co2_flux", "CO2_flux_QC"),
    "sr": ("SW_IN", "SW_IN_F", "SWIN", "SWIN_1_1_1", "Rg", "solar_radiation", "Solar_Wm2", "SolarRadiation_Wm2", "Sol Rad (W/sq.m)", "SR", "SlrW_Avg"),
    "rn": ("NETRAD", "NETRAD_F", "RN", "RN_1_1_1", "net_radiation", "Net Rad (W/sq.m)", "Rn_Avg", "NR_Wm2_Avg"),
    "g": ("G", "G_F_MDS", "SHF", "SHF_1_1_1", "soil_heat_flux", "HFP01SC_Avg", "G_Avg"),
    "at": ("TA", "TA_F", "air_temperature", "Air Temp (C)", "Tair", "AirTC_Avg", "TA_1_1_1", "Temp_C"),
    "rh": ("RH", "RH_F", "relative_humidity", "Rel Hum (%)", "RH_Avg", "RH_pct", "RH_1_1_1"),
    "vpd": ("VPD", "VPD_F", "vpd", "VPD_kPa"),
    "ws": ("WS", "WS_F", "wind_speed", "Wind Speed (m/s)", "WS_ms_Avg", "WindSpd", "WindSpeed", "WindSpeed_ms", "WindSpd_Avg"),
    "wind_dir": ("WD", "WD_F", "wind_dir", "Wind Dir (0-360)", "WindDir", "WD_deg"),
    "rain": ("P", "P_F", "RAIN", "rain", "precip", "precipitation", "Precip (mm)", "Rain_mm_Tot", "Rain_mm"),
    "pa": ("PA", "PA_F", "air_pressure", "pressure", "BP", "Baro_kPa_Avg"),
    "swc": ("SWC", "SWC_F_MDS", "SWC_1_1_1", "soil_moisture", "VWC", "VWC_Avg", "SWC_fraction", "water_content"),
    "soil_temperature": ("TS", "TS_F_MDS", "TS_1_1_1", "soil_temperature", "Soil Temp (C)", "SoilTC_Avg"),
    "ustar": ("USTAR", "u*", "ustar", "u_star"),
    "obukhov_length": ("L", "MO_LENGTH", "obukhov_length", "ObukhovLength"),
    "sigma_v": ("SIGMA_V", "sigma_v", "v_sd", "v_std"),
    "ET": ("ET", "ET_mm", "ET_measured"),
    "ETo": ("ETo", "ETo (mm)", "ETo_mm", "ETO", "ET0", "reference_et"),
    "vp": ("VP", "VAPOR_PRESSURE", "Vap Pres (kPa)", "vap_pres", "ea"),
    "dew_point": ("TDEW", "Dew Point (C)", "dew_point", "Tdew"),
}

# Module readiness is deliberately conservative. Optional modules can be skipped.
MODULE_REQUIREMENTS = {
    "gap_filling": {
        "label": "Gap filling & validation",
        "all": ["datetime"], "any": [["LE", "H", "NEE"]],
        "recommended": ["sr", "at", "vpd"],
        "help": "At least one flux target is required. Radiation, temperature and VPD strengthen MDS/ML models.",
    },
    "energy_balance": {
        "label": "Energy balance",
        "all": ["LE", "H", "rn", "g"], "any": [], "recommended": [],
        "help": "Requires LE, H, net radiation and soil heat flux for closure diagnostics/correction.",
    },
    "et_analysis": {
        "label": "ET / water analysis",
        "all": ["LE"], "any": [], "recommended": ["ETo", "rain", "swc"],
        "help": "LE is required. ETo, precipitation/irrigation and soil water are optional enhancements.",
    },
    "ustar": {
        "label": "u* threshold analysis",
        "all": ["NEE", "ustar", "at"], "any": [], "recommended": ["sr"],
        "help": "Requires NEE/CO2 flux, friction velocity and temperature; radiation supports day/night separation.",
    },
    "carbon": {
        "label": "Carbon partitioning",
        "all": ["NEE", "at"], "any": [], "recommended": ["ustar", "sr"],
        "help": "Requires NEE and temperature; u* and radiation are strongly recommended for screened partitioning.",
    },
    "footprint": {
        "label": "Footprint analysis",
        "all": ["ustar", "wind_dir", "obukhov_length", "measurement_height"], "any": [],
        "recommended": ["sigma_v", "canopy_height"],
        "help": "Requires turbulence/wind variables plus site geometry. Missing site metadata can be entered manually.",
    },
    "irrigation": {
        "label": "Irrigation / rainfall overlay",
        "all": [], "any": [["rain", "irrigation"]], "recommended": ["ET", "ETo", "swc"],
        "help": "Optional module. Supply irrigation and/or precipitation to overlay water inputs with ET/soil response.",
    },
}


def _norm(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").strip().lower())


def _column_lookup(columns: Iterable[object]) -> Dict[str, str]:
    out = {}
    for c in columns:
        key = _norm(c)
        if key and key not in out:
            out[key] = str(c)
    return out


def _best_alias(columns: Sequence[str], role: str) -> tuple[Optional[str], float, str]:
    lookup = _column_lookup(columns)
    aliases = ALIASES.get(role, ())
    # Exact normalized alias match.
    for rank, alias in enumerate(aliases):
        key = _norm(alias)
        if key in lookup:
            conf = 0.99 if rank < 2 else max(0.80, 0.97 - 0.015 * rank)
            return lookup[key], conf, f"matched {alias}"
    # Conservative token fallback for custom logger naming.
    role_tokens = {
        "LE": ("latent", "heat"), "H": ("sensible", "heat"), "NEE": ("nee",),
        "sr": ("solar", "rad"), "rn": ("net", "rad"), "g": ("soil", "heat"),
        "at": ("air", "temp"), "rh": ("relative", "hum"), "vpd": ("vpd",),
        "ws": ("wind", "speed"), "wind_dir": ("wind", "dir"), "rain": ("precip",),
        "pa": ("pressure",), "swc": ("soil", "water"), "soil_temperature": ("soil", "temp"),
        "ustar": ("ustar",), "ETo": ("eto",), "vp": ("vapor", "pres"), "dew_point": ("dew",),
    }.get(role, ())
    if role_tokens:
        for c in columns:
            n = _norm(c)
            if all(t in n for t in role_tokens):
                return str(c), 0.65, "token match"
    return None, 0.0, "not detected"


def _sniff_delimiter(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(65536)
    lines = [x for x in sample.splitlines() if x.strip()][:20]
    if not lines:
        return ","
    joined = "\n".join(lines)
    try:
        return csv.Sniffer().sniff(joined, delimiters=",\t;|").delimiter
    except Exception:
        counts = {d: lines[0].count(d) for d in ("\t", ",", ";", "|")}
        return max(counts, key=counts.get) if max(counts.values()) else ","


def detect_format(path: str) -> dict:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in {".xlsx", ".xls", ".xlsb", ".ods"}:
        return {"format": "excel", "label": "Excel workbook", "confidence": 0.99, "delimiter": None}
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        head = "\n".join([f.readline().rstrip("\r\n") for _ in range(5)])
    first = head.splitlines()[0] if head else ""
    first_upper = first.upper()
    if first_upper.startswith("DATAH\t") and "\nDATAU\t" in head.upper():
        return {"format": "eddypro", "label": "EddyPro full output", "confidence": 1.0, "delimiter": "\\t"}
    if first_upper.startswith('"TOA5"') or first_upper.startswith("TOA5,"):
        return {"format": "campbell_toa5", "label": "Campbell Scientific TOA5", "confidence": 1.0, "delimiter": ","}
    delim = _sniff_delimiter(path)
    header = first
    hnorm = _norm(header)
    if "stnid" in hnorm and "hourpst" in hnorm and "cimisregion" in hnorm:
        return {"format": "cimis", "label": "CIMIS hourly CSV", "confidence": 1.0, "delimiter": delim}
    if "timestampstart" in hnorm or "timestampend" in hnorm or ("neevut" in hnorm and "ustar" in hnorm):
        return {"format": "fluxnet", "label": "AmeriFlux / FLUXNET-style", "confidence": 0.97, "delimiter": delim}
    label = "Generic TSV/TXT" if delim == "\t" else "Generic delimited file"
    return {"format": "generic", "label": label, "confidence": 0.80, "delimiter": delim}


def _read_table(path: str, fmt: Optional[str] = None, nrows: Optional[int] = None) -> tuple[pd.DataFrame, Dict[str, str], dict]:
    det = detect_format(path) if fmt is None else {**detect_format(path), "format": fmt}
    fmt = det["format"]
    units: Dict[str, str] = {}
    meta: dict = {}
    if fmt == "eddypro":
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            h = f.readline().rstrip("\r\n").split("\t")
            u = f.readline().rstrip("\r\n").split("\t")
        units = {str(c): (u[i] if i < len(u) else "") for i, c in enumerate(h)}
        df = pd.read_csv(path, sep="\t", header=0, skiprows=[1], nrows=nrows, low_memory=False,
                         na_values=["NaN", "nan", "NAN", "-9999", "-9999.0", ""])
        if len(df.columns):
            record = df.columns[0]
            df = df[df[record].astype(str).str.upper().eq("DATA")].copy()
        meta["header_rows"] = 2
    elif fmt == "campbell_toa5":
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            rows = [next(f, "").rstrip("\r\n") for _ in range(4)]
        cols = next(csv.reader([rows[1]])) if len(rows) > 1 else []
        unit_row = next(csv.reader([rows[2]])) if len(rows) > 2 else []
        units = {str(c): (unit_row[i] if i < len(unit_row) else "") for i, c in enumerate(cols)}
        try:
            station_meta = next(csv.reader([rows[0]]))
            meta["station"] = station_meta[1] if len(station_meta) > 1 else ""
            meta["logger"] = station_meta[2] if len(station_meta) > 2 else ""
        except Exception:
            pass
        df = pd.read_csv(path, skiprows=[0,2,3], header=0, nrows=nrows, low_memory=False,
                         na_values=["NAN", "NaN", "nan", "-9999", "-9999.0", ""])
        meta["header_rows"] = 4
    elif fmt == "excel":
        try:
            try:
                xls = pd.ExcelFile(path, engine="calamine")
                engine = "calamine"
            except Exception:
                xls = pd.ExcelFile(path)
                engine = xls.engine
            sheet = xls.sheet_names[0]
            df = pd.read_excel(path, sheet_name=sheet, engine=engine, nrows=nrows)
            meta["sheet"] = sheet
            meta["sheets"] = list(xls.sheet_names)
            meta["excel_engine"] = engine
        except Exception as exc:
            raise ValueError("The Excel workbook could not be read. In the browser FluxGapFill uses python-calamine. " + str(exc)) from exc
    else:
        sep = det.get("delimiter") or _sniff_delimiter(path)
        df = pd.read_csv(path, sep=sep, nrows=nrows, low_memory=False,
                         na_values=["NaN", "nan", "NAN", "-9999", "-9999.0", "-6999", ""])
    df.columns = [str(c).strip() for c in df.columns]
    return df, units, {**det, **meta}


def _unit_from_colname(col: str) -> str:
    text = str(col)
    m = re.search(r"\(([^()]*)\)\s*$", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"\[([^\[\]]*)\]\s*$", text)
    if m:
        return m.group(1).strip()
    low = text.lower()
    suffixes = [
        (r"(?:_|\b)wm2(?:_|$)", "W m-2"),
        (r"(?:_|\b)kpa(?:_|$)", "kPa"),
        (r"(?:_|\b)hpa(?:_|$)", "hPa"),
        (r"(?:_|\b)pct(?:_|$)", "%"),
        (r"(?:_|\b)fraction(?:_|$)", "m3 m-3"),
        (r"(?:_|\b)(?:ms|mps)(?:_|$)", "m s-1"),
        (r"(?:_|\b)deg(?:_|$)", "degree"),
        (r"(?:_|\b)mm(?:_|$)", "mm interval-1"),
        (r"umol.*m2.*s", "umol m-2 s-1"),
        (r"(?:_|\b)(?:degc|temp_c|_c)(?:_|$)", "degC"),
    ]
    for pat, unit in suffixes:
        if re.search(pat, low): return unit
    return ""


def _default_unit(role: str, fmt: str, column: Optional[str], declared: str) -> str:
    if declared:
        return str(declared).strip().strip("[]")
    # Common logger/manual temperature suffixes such as AirTemp_C or TA_C.
    # Handle these role-specifically so an unrelated column ending in _C is not
    # silently interpreted as temperature.
    if role in {"at", "soil_temperature", "dew_point"} and re.search(r"(?:_|\b)c$", str(column or "").strip().lower()):
        return "degC"
    from_name = _unit_from_colname(column or "")
    if from_name:
        return from_name
    # Well-documented standard-file conventions; the UI still exposes every unit.
    if fmt == "fluxnet":
        defaults = {"LE":"W m-2","H":"W m-2","NEE":"umol m-2 s-1","sr":"W m-2","rn":"W m-2","g":"W m-2",
                    "at":"degC","rh":"%","vpd":"hPa","ws":"m s-1","wind_dir":"degree","rain":"mm interval-1",
                    "pa":"kPa","swc":"%","soil_temperature":"degC","ustar":"m s-1","ETo":"mm interval-1"}
        return defaults.get(role, "")
    if fmt == "cimis":
        defaults = {"sr":"W m-2","rn":"W m-2","at":"degC","rh":"%","ws":"m s-1","wind_dir":"degree",
                    "rain":"mm interval-1","soil_temperature":"degC","ETo":"mm interval-1","vp":"kPa","dew_point":"degC"}
        return defaults.get(role, "")
    return ""


def _timestamp_recommendation(fmt: str, mapping: Mapping[str, dict]) -> tuple[str, str]:
    if fmt == "eddypro": return "end", "EddyPro full-output records are interval-end timestamps."
    if fmt == "cimis": return "end", "CIMIS Hour (PST) is treated as an hourly interval-end code by the CIMIS alignment parser."
    if fmt == "campbell_toa5": return "end", "TOA5 interval tables commonly timestamp records at the write/end time; confirm for the logger program."
    ts = str((mapping.get("timestamp") or {}).get("column") or "").upper()
    tm = str((mapping.get("time") or {}).get("column") or "").upper()
    encoded = ts + " " + tm
    if "END" in encoded: return "end", "Detected time column name indicates interval end."
    if "START" in encoded: return "start", "Detected time column name indicates interval start."
    return "midpoint", "No start/end convention was encoded in the detected column name; midpoint is the non-shifting default."


def inspect_file(path: str, kind: str = "primary") -> dict:
    """Inspect a file and return format, columns, units, suggestions and a preview."""
    det = detect_format(path)
    sample, units, read_meta = _read_table(path, det["format"], nrows=250)
    cols = list(sample.columns)
    mapping = {}
    for role in ROLE_LABELS:
        col, conf, reason = _best_alias(cols, role)
        declared = units.get(col, "") if col else ""
        mapping[role] = {
            "column": col, "confidence": conf, "reason": reason,
            "unit": _default_unit(role, det["format"], col, declared),
            "canonical_unit": CANONICAL_UNITS.get(role, ""),
            "label": ROLE_LABELS[role],
        }
    # EddyPro separate date+time is more trustworthy than trying to infer a synthetic timestamp.
    if det["format"] == "eddypro":
        mapping["timestamp"]["column"] = None; mapping["timestamp"]["confidence"] = 0.0
        for role in ("date", "time"):
            col, conf, reason = _best_alias(cols, role)
            mapping[role].update(column=col, confidence=conf, reason=reason)
    # CIMIS has Date + Hour (PST).
    if det["format"] == "cimis":
        mapping["timestamp"]["column"] = None
        date_col = next((c for c in cols if _norm(c)=="date"), None)
        hour_col = next((c for c in cols if "hourpst" in _norm(c)), None)
        mapping["date"].update(column=date_col, confidence=1.0 if date_col else 0.0, reason="CIMIS Date")
        mapping["time"].update(column=hour_col, confidence=1.0 if hour_col else 0.0, reason="CIMIS Hour (PST)")
    convention, convention_note = _timestamp_recommendation(det["format"], mapping)

    # Estimate rows without forcing a full parse for large text files.
    if Path(path).suffix.lower() in {".xlsx", ".xls", ".xlsb", ".ods"}:
        row_estimate = None
    else:
        try:
            with open(path, "rb") as f:
                row_estimate = sum(1 for _ in f)
            row_estimate = max(0, row_estimate - int(read_meta.get("header_rows", 1)))
        except Exception:
            row_estimate = None
    preview = sample.head(6).replace({np.nan: None}).astype(object)
    preview_rows = [{str(k): (None if pd.isna(v) else str(v)) for k,v in r.items()} for r in preview.to_dict(orient="records")]
    return {
        "kind": kind, "name": Path(path).name, "size_bytes": int(os.path.getsize(path)),
        "format": det["format"], "format_label": det["label"], "format_confidence": float(det["confidence"]),
        "delimiter": det.get("delimiter"), "columns": cols, "units": units, "mapping": mapping,
        "timestamp_recommendation": convention, "timestamp_note": convention_note,
        "row_estimate": row_estimate, "preview": preview_rows, "reader_metadata": read_meta,
        "role_groups": ROLE_GROUPS,
    }


def _parse_time_like(values: pd.Series) -> pd.Series:
    s = values.astype(str).str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    # HHMM / HHMMSS, including 2400.
    digits = s.str.replace(r"\.0$", "", regex=True)
    for i, x in digits.items():
        if not x or x.lower() in {"nan","none","nat"}: continue
        if re.fullmatch(r"\d{1,4}", x):
            iv = int(x)
            if len(x) <= 2 and 0 <= iv <= 23:
                hh, mm = iv, 0
            else:
                z = x.zfill(4); hh, mm = int(z[:2]), int(z[2:])
            if hh == 24 and mm == 0:
                out.loc[i] = pd.Timestamp("1900-01-02 00:00:00")
            elif 0 <= hh <= 23 and 0 <= mm <= 59:
                out.loc[i] = pd.Timestamp(1900,1,1,hh,mm)
        elif re.fullmatch(r"\d{5,6}", x):
            z=x.zfill(6); hh,mm,ss=int(z[:2]),int(z[2:4]),int(z[4:])
            if 0<=hh<=23 and 0<=mm<=59 and 0<=ss<=59: out.loc[i]=pd.Timestamp(1900,1,1,hh,mm,ss)
        else:
            p=pd.to_datetime(x, errors="coerce")
            if not pd.isna(p): out.loc[i]=pd.Timestamp(1900,1,1,p.hour,p.minute,p.second)
    return out


def _parse_timestamp(values: pd.Series) -> pd.Series:
    s = values.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    lens = s.str.len()
    fmts = {8:"%Y%m%d", 12:"%Y%m%d%H%M", 14:"%Y%m%d%H%M%S"}
    for n, fmt in fmts.items():
        m = lens.eq(n) & s.str.fullmatch(r"\d+")
        if m.any(): out.loc[m] = pd.to_datetime(s.loc[m], format=fmt, errors="coerce")
    rem = out.isna()
    if rem.any(): out.loc[rem] = pd.to_datetime(s.loc[rem], errors="coerce")
    return out


def _build_datetime(raw: pd.DataFrame, mapping: Mapping[str, dict]) -> pd.Series:
    ts_col = (mapping.get("timestamp") or {}).get("column")
    if ts_col and ts_col in raw:
        return _parse_timestamp(raw[ts_col])
    date_col = (mapping.get("date") or {}).get("column")
    time_col = (mapping.get("time") or {}).get("column")
    if not date_col or date_col not in raw or not time_col or time_col not in raw:
        raise ValueError("Map either one Timestamp column or both Date and Time columns.")
    dates = pd.to_datetime(raw[date_col], errors="coerce").dt.normalize()
    times = _parse_time_like(raw[time_col])
    result = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    for i in raw.index:
        d, t = dates.loc[i], times.loc[i]
        if pd.isna(d) or pd.isna(t): continue
        # 1900-01-02 encodes 24:00 from the helper.
        dayoff = 1 if t.day == 2 else 0
        result.loc[i] = d + pd.Timedelta(days=dayoff, hours=t.hour, minutes=t.minute, seconds=t.second)
    return result


def _unit_key(u: object) -> str:
    text = str(u or "").strip().lower().replace("−","-").replace("²","2").replace("³","3").replace("·"," ")
    text = re.sub(r"\s+", " ", text)
    return text


def _convert(values: pd.Series, role: str, unit: str, step: pd.Timedelta) -> tuple[pd.Series, str]:
    v = pd.to_numeric(values, errors="coerce")
    u = _unit_key(unit)
    if not u:
        return v, "assumed canonical"
    compact = re.sub(r"[\s_^/()]", "", u)
    # Energy flux.
    if role in {"LE","H","sr","rn","g"}:
        if compact in {"wm-2","wm2","wattm-2","wattm2","wattsm-2","wattsm2"}: return v, "no conversion"
        if compact in {"kwm-2","kwm2"}: return v*1000.0, "kW m-2 to W m-2"
        if "mjm-2h-1" in compact or "mjm2h-1" in compact or compact in {"mjm-2hr-1","mjm2hr-1"}: return v*1e6/3600.0, "MJ m-2 h-1 to W m-2"
        if "mjm-2d-1" in compact or "mjm2d-1" in compact: return v*1e6/86400.0, "MJ m-2 d-1 to W m-2"
    if role in {"at","soil_temperature","dew_point"}:
        if compact in {"degc","c","celsius","°c"}: return v, "no conversion"
        if compact in {"k","kelvin"}: return v-273.15, "K to degC"
        if compact in {"degf","f","fahrenheit","°f"}: return (v-32)*5/9, "degF to degC"
    if role == "rh":
        if "%" in u or compact in {"percent","pct"}: return v, "no conversion"
        if compact in {"fraction","01"}: return v*100.0, "fraction to %"
    if role in {"vpd","vp","pa"}:
        if compact in {"kpa"}: return v, "no conversion"
        if compact in {"pa","pascal","pascals"}: return v/1000.0, "Pa to kPa"
        if compact in {"hpa","mbar","mb"}: return v/10.0, "hPa/mbar to kPa"
    if role in {"ws","ustar","sigma_v"}:
        if compact in {"ms-1","m/s","ms","msec-1"}: return v, "no conversion"
        if compact in {"mph"}: return v*0.44704, "mph to m s-1"
        if compact in {"kmh-1","km/h","kmph"}: return v/3.6, "km h-1 to m s-1"
    if role == "wind_dir":
        if compact in {"deg","degree","degrees","0-360"}: return v % 360.0, "normalized degrees"
        if compact in {"rad","radian","radians"}: return np.rad2deg(v) % 360.0, "radians to degrees"
    if role in {"rain","ET","ETo"}:
        if compact in {"mm","mminterval-1","mminterval","mmrecord-1"}: return v, "no conversion"
        if compact in {"in","inch","inches","ininterval-1"}: return v*25.4, "inch to mm"
        hours=float(step/pd.Timedelta(hours=1))
        if compact in {"mmh-1","mmhr-1","mm/hour","mmhour-1"}: return v*hours, "mm h-1 to interval total"
        if compact in {"inh-1","inhr-1"}: return v*25.4*hours, "in h-1 to interval total"
    if role == "swc":
        if compact in {"m3m-3","m3m3","m3/m3","fraction","vv","v/v"}: return v, "no conversion"
        if "%" in u or compact in {"percent","pct"}: return v/100.0, "% to m3 m-3"
    if role == "NEE":
        if compact in {"umolm-2s-1","µmolm-2s-1","micromolm-2s-1","umolm2s","µmolm2s","umolm2s-1"}: return v, "no conversion"
        if compact in {"mgco2m-2s-1","mgco2m2s-1"}: return v*(1000.0/44.0095), "mg CO2 to umol CO2"
    if role == "obukhov_length" and compact in {"m","meter","metre"}: return v, "no conversion"
    # Permit canonical unit spellings.
    expected = _unit_key(CANONICAL_UNITS.get(role, ""))
    if compact == re.sub(r"[\s_^/()]", "", expected): return v, "no conversion"
    raise ValueError(f"Unsupported unit {unit!r} for {ROLE_LABELS.get(role, role)}. Choose a supported unit in Variable Mapping.")


def _quality_mask(values: pd.Series, maximum_qc: float) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    ok = pd.Series(False, index=values.index)
    nm = numeric.notna(); ok.loc[nm] = numeric.loc[nm] <= float(maximum_qc)
    text = values.astype(str).str.strip().str.lower()
    ok.loc[~nm] = text.loc[~nm].isin({"", "nan", "none", "good", "ok", "accepted", "true", "pass", "0"})
    return ok


def _apply_physical(values: pd.Series, role: str) -> tuple[pd.Series, pd.Series]:
    lim = PHYSICAL_LIMITS.get(role)
    if role == "NEE": lim = (-500.0, 500.0)
    if role == "soil_temperature": lim = (-60.0, 80.0)
    if role == "wind_dir": lim = (0.0, 360.0)
    if role == "ustar": lim = (0.0, 10.0)
    if role == "obukhov_length": lim = (-100000.0, 100000.0)
    if role == "sigma_v": lim = (0.0, 50.0)
    if role in {"vp"}: lim = (0.0, 15.0)
    if role in {"dew_point"}: lim = (-80.0, 70.0)
    if role in {"ETo"}: lim = (0.0, 20.0)
    if not lim: return values, pd.Series(True, index=values.index)
    ok = values.between(lim[0], lim[1], inclusive="both") | values.isna()
    return values.where(ok), ok


def _mapping_payload(mapping: Mapping[str, object]) -> Dict[str, dict]:
    out = {}
    for role, val in mapping.items():
        if isinstance(val, dict): out[role] = dict(val)
        else: out[role] = {"column": val}
    return out


def read_mapped_file(path: str, mapping: Mapping[str, object], config: Mapping[str, object], fmt: Optional[str]=None) -> tuple[pd.DataFrame, dict]:
    mapping = _mapping_payload(mapping)
    raw, declared_units, meta = _read_table(path, fmt=fmt, nrows=None)
    dt_input = _build_datetime(raw, mapping)
    good = dt_input.notna()
    if good.sum() < 3:
        raise ValueError("Fewer than three valid timestamps were found after applying the mapping.")
    raw = raw.loc[good].reset_index(drop=True); dt_input = dt_input.loc[good].reset_index(drop=True)
    step = infer_timestep(dt_input)
    convention = str(config.get("timestamp_convention") or "midpoint")
    time_basis = str(config.get("time_basis") or "local_standard")
    timezone = str(config.get("timezone") or "America/Los_Angeles")
    maximum_qc = float(config.get("maximum_qc", 1.0))
    cfg = InputProcessingConfig(timezone=timezone, time_basis=time_basis, timestamp_convention=convention, maximum_accepted_qc=maximum_qc)
    dt, dst_invalid = canonicalize_timestamps(dt_input, step, cfg)

    out = pd.DataFrame({"datetime": dt, "datetime_input": dt_input})
    audit=[]
    value_roles = [r for r in CANONICAL_UNITS if r not in {"timestamp","date","time"}]
    for role in value_roles:
        info=mapping.get(role) or {}; col=info.get("column")
        if not col or col not in raw: continue
        unit=str(info.get("unit") or declared_units.get(col) or _default_unit(role, meta.get("format","generic"), col, ""))
        converted, conversion = _convert(raw[col], role, unit, step)
        accepted, physical = _apply_physical(converted, role)
        qc_role=f"qc_{role}"; qinfo=mapping.get(qc_role) or {}; qcol=qinfo.get("column")
        if qcol and qcol in raw and role in {"LE","H","NEE"}:
            qok=_quality_mask(raw[qcol], maximum_qc); accepted=accepted.where(qok)
        else:
            qok=pd.Series(True,index=raw.index)
        internal = "soil_temperature_representative" if role=="soil_temperature" else role
        out[f"{internal}_raw"] = pd.to_numeric(raw[col], errors="coerce").to_numpy()
        out[internal] = accepted.to_numpy()
        if role in {"LE","H","NEE"}:
            out[f"{internal}_input_qc"] = (raw[qcol].to_numpy() if qcol and qcol in raw else np.repeat(np.nan,len(raw)))
            flag=np.where(pd.to_numeric(raw[col],errors="coerce").isna(),"raw_missing","accepted").astype(object)
            flag=np.where(~physical.to_numpy(),"outside_physical_screen",flag)
            if qcol and qcol in raw: flag=np.where(~qok.to_numpy(),"rejected_by_input_qc",flag)
            out[f"{internal}_screen_flag"] = flag
        audit.append({"variable":internal,"source_column":col,"declared_unit":unit or "not declared","canonical_unit":CANONICAL_UNITS.get(role,""),
                      "conversion":conversion,"quality_column":qcol or "none","n_raw_numeric":int(pd.to_numeric(raw[col],errors="coerce").notna().sum()),
                      "n_accepted":int(pd.Series(accepted).notna().sum()),"n_rejected_physical":int((~physical & converted.notna()).sum()),
                      "n_rejected_qc":int((~qok & converted.notna()).sum())})
    # Quality columns are not duplicated as science variables.
    out = out.dropna(subset=["datetime"]).sort_values("datetime")
    # Aggregate duplicate timestamps conservatively: totals sum, continuous values mean.
    dup_count=int(out["datetime"].duplicated(keep=False).sum())
    if dup_count:
        ag={}
        for c in out.columns:
            if c=="datetime": continue
            if c in {"rain","ET","ETo"}: ag[c]="sum"
            elif pd.api.types.is_numeric_dtype(out[c]): ag[c]="mean"
            else: ag[c]="first"
        out=out.groupby("datetime",as_index=False).agg(ag)
    # Regularize to a complete grid.
    grid=pd.date_range(out["datetime"].min(), out["datetime"].max(), freq=step)
    out=out.set_index("datetime").reindex(grid).rename_axis("datetime").reset_index()
    # Derived VPD when missing.
    if ("vpd" not in out or out["vpd"].notna().sum()<2) and "at" in out and "rh" in out:
        at=pd.to_numeric(out["at"],errors="coerce"); rh=pd.to_numeric(out["rh"],errors="coerce")
        es=0.6108*np.exp(17.27*at/(at+237.3)); out["vpd"]=(es*(1-rh/100.0)).clip(lower=0)
        audit.append({"variable":"vpd","source_column":"derived from at+rh","declared_unit":"derived","canonical_unit":"kPa","conversion":"Tetens saturation vapor pressure",
                      "quality_column":"none","n_raw_numeric":int(out["vpd"].notna().sum()),"n_accepted":int(out["vpd"].notna().sum()),"n_rejected_physical":0,"n_rejected_qc":0})
    return out.reset_index(drop=True), {
        "format": meta.get("format"), "format_label": meta.get("label"), "input_rows": int(len(raw)), "rows_regularized": int(len(out)),
        "timestep_minutes": float(step/pd.Timedelta(minutes=1)), "duplicate_timestamps": dup_count, "dst_invalid": int(dst_invalid),
        "audit": audit, "reader_metadata": meta,
    }


def align_supplemental(source: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    """Align a mapped supplemental environmental table to the target midpoint grid."""
    if len(source)<2 or len(target)<2: return pd.DataFrame({"datetime":target["datetime"]})
    s=source.copy(); s["datetime"]=pd.to_datetime(s["datetime"]); s=s.set_index("datetime").sort_index()
    tindex=pd.DatetimeIndex(pd.to_datetime(target["datetime"]))
    target_step=infer_timestep(tindex); source_step=infer_timestep(s.index)
    result=pd.DataFrame(index=tindex)
    totals={"rain","ET","ETo"}
    for col in [c for c in s.columns if c not in {"datetime_input"} and not c.endswith(("_raw","_input_qc")) and "screen_flag" not in c]:
        vals=pd.to_numeric(s[col],errors="coerce")
        if vals.notna().sum()<2: continue
        if source_step >= target_step:
            if col in totals:
                # Hold each interval total and apportion by duration ratio.
                interval_start=(tindex.floor(source_step) if source_step>=pd.Timedelta(hours=1) else tindex)
                # nearest source timestamp after canonical midpoint alignment works better via nearest with half-step tolerance.
                idxer=s.index.get_indexer(tindex, method="nearest", tolerance=source_step/2+target_step/2)
                arr=np.full(len(tindex),np.nan)
                good=idxer>=0; arr[good]=vals.iloc[idxer[good]].to_numpy()*float(target_step/source_step)
                result[col]=arr
            else:
                union=vals.index.union(tindex).sort_values(); tmp=vals.reindex(union).interpolate(method="time",limit_area="inside")
                arr=tmp.reindex(tindex)
                # Do not bridge gaps much larger than one native interval.
                prev=s.index.to_series().reindex(union).ffill().reindex(tindex)
                nxt=s.index.to_series().reindex(union).bfill().reindex(tindex)
                bridge=(nxt-prev)<=source_step*1.5
                result[col]=arr.where(bridge.to_numpy())
        else:
            # Source finer than target: average continuous drivers, sum totals.
            ser=vals.copy();
            # nearest target midpoint assignment within half target interval.
            loc=tindex.get_indexer(ser.index,method="nearest",tolerance=target_step/2)
            frame=pd.DataFrame({"loc":loc,"val":ser.to_numpy()}); frame=frame[frame["loc"]>=0]
            agg=frame.groupby("loc")["val"].sum() if col in totals else frame.groupby("loc")["val"].mean()
            arr=np.full(len(tindex),np.nan); arr[agg.index.to_numpy(dtype=int)]=agg.to_numpy(); result[col]=arr
    result=result.reset_index(names="datetime")
    return result


def capabilities(data: pd.DataFrame, project_meta: Optional[Mapping[str, object]]=None) -> list[dict]:
    meta=dict(project_meta or {})
    available=set()
    for c in data.columns:
        if c == "soil_temperature_representative": key="soil_temperature"
        elif c.endswith("_ref"): key=c[:-4]
        else: key=c
        try:
            if pd.to_numeric(data[c],errors="coerce").notna().any(): available.add(key)
        except Exception:
            if data[c].notna().any(): available.add(key)
    if "datetime" in data: available.add("datetime")
    for key,val in meta.items():
        if val not in (None,"",False): available.add(str(key))
    out=[]
    for mid,spec in MODULE_REQUIREMENTS.items():
        missing=[x for x in spec.get("all",[]) if x not in available]
        any_missing=[]
        any_groups=spec.get("any",[])
        for group in any_groups:
            if not any(x in available for x in group): any_missing.append(" or ".join(group))
        missing_all=missing+any_missing
        recommended=[x for x in spec.get("recommended",[]) if x not in available]
        status="ready" if not missing_all else "needs_data"
        out.append({"id":mid,"label":spec["label"],"status":status,"missing_required":missing_all,"missing_recommended":recommended,
                    "help":spec["help"],"available":sorted(available)})
    return out


def project_schema() -> dict:
    return {
        "roles":[{"id":r,"label":ROLE_LABELS[r],"canonical_unit":CANONICAL_UNITS.get(r,"")} for r in ROLE_LABELS],
        "groups":ROLE_GROUPS,
        "modules":MODULE_REQUIREMENTS,
    }
