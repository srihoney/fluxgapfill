# FluxGapFill Phase 2 — Science Engine Notes

**Build:** `20260923p2`

## Scope boundary

FluxGapFill starts from processed fluxes and half-hourly/hourly environmental time series. It does not compute EC fluxes from raw high-frequency sonic/IRGA signals.

## Processing order

The intended Phase 2 flow is:

`Universal Import → standardized schema → source/EddyPro QC → optional Phase 2 QC → gap diagnostics → blocked validation → adaptive/fixed reconstruction → provenance/export`

The pre-Phase-2 standardized dataset is preserved in application state so changing Phase 2 QC settings re-runs from the same imported baseline rather than repeatedly screening already-screened values.

## Phase 2 QC

`public/py/phase2_qc.py` provides:

- broad range/plausibility screens
- optional rolling Hampel-style flux spike screening
- `<variable>_pre_phase2_qc` preservation
- `<variable>_phase2_qc_flag` record-level reasons
- downloadable per-variable audit statistics

The optional spike screen is disabled by default in the UI. It is intentionally treated as an analyst-controlled extra screen, not as a mandatory replacement for source/EddyPro QC.

## MDS

`public/py/gap_filler.py` supports:

- `Reichstein05`
- `Vekuri23`

The Reichstein05 path retains the existing LUT/MDC search hierarchy and does not recycle filled target values as donors.

The Vekuri23 daytime LUT aggregation balances donor subsets below/at versus above the target radiation before combining their means. Nighttime or incomplete-side cases fall back to the regular donor mean. MDC fallback remains available as part of the MDS search hierarchy.

### MDS quality and uncertainty terminology

Phase 2 separates three concepts:

1. **MDS fill QC** — method/search-window information.
2. **Gap-duration class** — separate duration-based risk category A–H.
3. **MDS donor SD** — spread of the donor population used for a fill.

Donor SD is not called a 95% prediction interval. Previous `fill_uncertainty` compatibility fields, where retained internally, should be interpreted as donor spread rather than calibrated predictive coverage.

## Machine-learning candidates

`public/py/advanced_ml_gap_filler.py` supports RF and XGBoost profiles:

- `external`: external/reference meteorological drivers
- `tower`: available tower/environmental drivers
- `cross`: tower/external drivers plus the complementary flux for LE/H when available

Feature engineering includes cyclic time features and selected physically interpretable environmental interactions. Missing-driver indicators are retained where appropriate.

### Leakage protection

A predictor derived from the target itself must not be used to validate reconstruction of that target. In particular, EddyPro `ET` is derived from LE and is **not used to predict LE**. This rule is important because target-derived variables can create unrealistically low validation error.

## Contiguous calendar-block validation

`public/py/validation_engine.py` creates artificial contiguous outages at user-selected durations.

Key design choices:

- test windows are calendar-contiguous
- validation is deterministic/reproducible
- separated windows are chosen when multiple windows are requested
- a window may contain naturally missing target records
- scoring uses only records that had an observed/QC-accepted target before artificial masking
- training labels exclude the artificially hidden target records
- metrics: coverage, RMSE, MAE, bias, R²
- diagnostics: overall, day/night and season

Day/night uses shortwave radiation when available; otherwise a clock-based fallback is reported.

### Reconstruction vs forecasting

RF/XGBoost may train on valid observations both before and after the held-out calendar window. This measures **post-processing reconstruction/interpolation skill** for a completed record. It is not a future-forecast evaluation. A train-before-test design would be required for forecasting claims.

## Empirical residual intervals

For each target × gap duration × candidate, validation pools held-out residuals:

`residual = observed - predicted`

When at least 20 finite held-out residuals are available, the 2.5th and 97.5th percentiles are stored. During real-gap filling, these residual quantiles are added to the selected reconstruction to form the exported empirical interval.

These are empirical blocked-validation residual intervals. They are not generated from tree variance or MDS donor SD.

The same blocked-validation set currently supports candidate selection and interval calibration. Consequently these intervals/performance metrics are site-specific reconstruction diagnostics rather than a completely independent final test. Publication-grade method comparison may require a nested or untouched holdout design.

## Adaptive filling

For each real gap, Phase 2:

1. computes the gap duration;
2. finds the nearest validated duration for that target;
3. orders available candidates by validation RMSE;
4. checks whether the candidate's required driver regime actually exists in the real gap;
5. uses the best eligible validated candidate;
6. falls back conservatively to validated Reichstein05 MDS when appropriate;
7. never overwrites measured/QC-accepted target observations.

Eligibility checks include complementary-flux availability for `cross`, external-reference driver availability for `external`, and tower-driver availability for `tower` profiles.

## Provenance fields

Depending on target/method, full export can include:

`<target>_original`  
`<target>_filled`  
`<target>_source`  
`<target>_method_detail`  
`<target>_gap_id`  
`<target>_gap_length_days`  
`<target>_gap_class`  
`<target>_mds_fill_qc`  
`<target>_mds_donor_sd`  
`<target>_mds_donor_n`  
`<target>_mds_window_days`  
`<target>_validation_rmse`  
`<target>_empirical_lower95`  
`<target>_empirical_upper95`

## Phase 1 import compatibility retained

Universal Import continues to map processed EddyPro, Campbell TOA5, FLUXNET/AmeriFlux-style, generic delimited files and Excel files into a common schema. Optional supplemental meteorology can be aligned to the primary grid. Known CIMIS QC-column mapping follows the actual duplicated-header sequence and includes net radiation, vapor pressure, air temperature, RH, dew point, wind speed/direction and soil temperature.

## Later phases

Phase 3 will implement the complete Energy + Water/ET workspace. Phase 4 will implement u* threshold, carbon partitioning and footprint modules. Their current readiness cards only identify inputs and metadata required to proceed later; they are not active analysis claims in Phase 2.
