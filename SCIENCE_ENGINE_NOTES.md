# FluxGapFill Phase 1 — Science / Import Notes

Build: `20260923p1`

## Scope boundary

FluxGapFill Phase 1 accepts **processed fluxes and half-hourly/hourly environmental time series**. It deliberately does not calculate EC fluxes from raw high-frequency sonic/IRGA measurements.

## Universal import foundation

`public/py/universal_import.py` provides:

- format detection for EddyPro, Campbell TOA5, CIMIS, FLUXNET/AmeriFlux-style, generic delimited data and Excel workbooks
- conservative column-name matching with detection confidence
- explicit manual mapping fallback
- timestamp/date/time handling
- start/end/midpoint timestamp conventions
- canonical-unit conversion
- regular time-grid construction
- physical screens for mapped environmental variables
- VPD derivation from temperature + RH when VPD is absent
- alignment of a supplemental environmental file onto the primary project time grid
- data/module capability checks

Specialized EddyPro and CIMIS parsers remain authoritative for those known formats. Generic mapping is intended for processed logger/manual files, not raw EC processing.

## Canonical project schema

The standardized project may contain:

`datetime, LE, H, NEE, qc_LE, qc_H, qc_NEE, sr, rn, g, at, rh, vpd, ws, wind_dir, rain, pa, swc, soil_temperature_representative, ustar, obukhov_length, sigma_v, ET, ETo, vp, dew_point`

Supplemental environmental inputs are aligned and stored as reference predictors where appropriate (for example `sr_ref`, `rn_ref`, `at_ref`, `vpd_ref`, `eto_ref`).

## EddyPro NEE retention

Phase 1 now retains processed EddyPro CO2 flux as internal `NEE` when present and applies the associated `qc_co2_flux`/equivalent QC flag. This allows the capability engine to recognize datasets suitable for later u* and carbon modules.

## Module readiness

The interface reports required and recommended inputs before a module can proceed. Examples:

- **Gap filling:** a regular timestamp plus at least one of LE/H/NEE; radiation/temperature/VPD recommended
- **Energy balance:** LE + H + Rn + G
- **ET analysis:** LE; ETo/rain/SWC recommended
- **u* analysis:** NEE + u* + air temperature; radiation recommended
- **Carbon partitioning:** NEE + air temperature; u* and radiation recommended
- **Footprint:** u* + wind direction + Monin–Obukhov length + measurement height; sigma-v/canopy height recommended
- **Irrigation overlay:** optional irrigation and/or rainfall input

A readiness result is not a claim that the future-phase scientific calculation has already been implemented. Phase 1 establishes the capability-aware input workflow.

## Retained gap-filling engine

The package continues to include:

- Reichstein-style MDS filling
- RF/XGBoost candidate models
- current/tower/cross-flux predictor profiles
- artificial contiguous calendar-gap validation
- adaptive model selection by validated gap duration
- provenance fields
- validation RMSE diagnostics
- LE-to-ET conversion

Measured/QC-accepted target observations are not overwritten by the gap-filling stage.

## Validation / uncertainty terminology

Blocked-validation RMSE is kept separate from formal uncertainty. Any RMSE-derived diagnostic bands should not be interpreted as statistically calibrated 95% prediction intervals unless a later phase explicitly implements and validates interval calibration.

## Local processing

The browser runtime is Pyodide running inside an ES-module Web Worker. User scientific files are copied to Pyodide's temporary browser filesystem for the current session; the web application does not upload them to a scientific processing server.
