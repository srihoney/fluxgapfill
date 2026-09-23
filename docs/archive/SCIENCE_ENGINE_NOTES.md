# FluxGapFill Phase 3 — Science Engine Notes

**Build:** `20260923p3`

## Scope boundary

FluxGapFill starts from **already-computed fluxes** and half-hourly/hourly environmental time series. It does not compute EC fluxes from raw high-frequency sonic/IRGA signals.

## Processing order

The Phase-3 processing path is:

`Universal Import → standardized schema → source/EddyPro QC → optional Phase-2 QC → gap diagnostics → blocked validation → fixed/adaptive reconstruction → Energy Balance → ET/Water → provenance/export`

Each stage keeps the upstream scientific state available so later modules do not need to overwrite measured variables.

---

# Phase 1 foundation retained

Universal Import maps EddyPro, CIMIS, Campbell TOA5, AmeriFlux/FLUXNET-style, generic delimited files and supported spreadsheet files to a common schema. Manual mapping is available when automatic detection is uncertain.

Phase 3 extends the canonical roles with:

- `irrigation` — interval applied-water total
- `pbl_height` — planetary boundary-layer height (m)
- `wstar` — convective velocity scale (m s⁻¹)

The dedicated water-input loader also supports **date-only daily totals**. Such records are kept on their stated calendar day, and daily totals are apportioned only among target records from that same day.

---

# Phase 2 engine retained

## Configurable QC

`public/py/phase2_qc.py` preserves pre-Phase-2 values and provides range/plausibility screening plus an optional rolling Hampel-style flux spike screen. The additional spike screen is disabled by default.

## MDS

`public/py/gap_filler.py` supports:

- `MDS_Reichstein05`
- `MDS_Vekuri23`

The Reichstein-style LUT/MDC search hierarchy does not recycle reconstructed target values as donors. The Vekuri-style daytime LUT aggregation balances donor subsets around target radiation before combining them; nighttime/fallback behavior remains separately handled.

MDS method/search-window QC, gap-duration class and donor SD are kept distinct. Donor SD is not presented as a calibrated 95% prediction interval.

## Machine learning

`public/py/advanced_ml_gap_filler.py` supports RF and XGBoost with:

- `external`
- `tower`
- `cross`

profiles. Target-derived predictors are excluded from the same target; specifically, EddyPro ET is not used as a hidden predictor of LE.

## Blocked reconstruction validation

`public/py/validation_engine.py` creates artificial contiguous calendar outages. Scoring uses only target records that were genuinely observed/QC-accepted before masking. Metrics include coverage, RMSE, MAE, bias and R², with day/night and seasonal diagnostics.

This is a **post-processing reconstruction** test, not a future-forecast evaluation. RF/XGBoost may use valid records both before and after an artificial gap.

## Empirical residual intervals

When enough held-out residuals exist for a target × gap duration × model, the 2.5th and 97.5th residual percentiles are stored and propagated during reconstruction. These are empirical blocked-validation residual intervals, not tree-variance intervals or MDS donor SD.

The same validation set currently supports candidate selection and residual calibration. Publication-grade unbiased method comparison can require a separate nested/untouched test set.

## Adaptive filling

For each real gap the engine considers duration, validated performance and actual predictor availability. Cross-flux models are not selected when the complementary flux is unavailable through the gap. Measured/QC-accepted values are never overwritten.

---

# Phase 3 energy + water engine

The Phase-3 science module is `public/py/phase3_energy_water.py`.

## 1. Data precedence and preservation

For H and LE, Phase 3 uses the Phase-2 reconstructed series when it exists (`H_filled`, `LE_filled`) as the **base continuous analysis series**, while separately retaining measured-only H and LE for primary energy-closure diagnostics.

Phase 3 creates new columns rather than overwriting raw/measured or Phase-2 reconstructed columns.

## 2. Available and turbulent energy

Definitions:

`AE = Rn - G`

`TE = H + LE`

Primary closure diagnostics use measured/QC-accepted H and LE plus finite Rn/G. Reconstructed closure is computed separately so filling cannot silently improve the primary measured closure statistic.

Diagnostics include:

- ratio-of-sums EBR = Σ(H+LE) / Σ(Rn-G)
- OLS slope/intercept/R²
- through-origin slope
- Huber robust slope/intercept when scikit-learn is available
- AE-vs-TE RMSE and MAE

A constant-AE edge case is handled explicitly rather than fitting a numerically rank-deficient OLS line.

Daylight/active-energy screening uses shortwave radiation when available and Rn as a fallback. The default threshold is 20 W m⁻² and is user-configurable.

## 3. Ground heat flux and soil heat storage

Default mode: **use G as supplied**.

Optional buried-plate mode:

`G_surface = G_plate + S`

`S = Cv × z × dT/dt`

`Cv = rho_b × cp_dry + theta_v × rho_w × cp_w`

Required explicit inputs/series:

- raw/plate G
- soil temperature
- volumetric SWC
- plate depth
- measured bulk density

Dry-soil specific heat is editable (default 840 J kg⁻¹ K⁻¹). Water density and heat capacity use physical constants in the module.

The implementation applies conservative screens to:

- absolute soil-temperature rate
- absolute storage flux
- absolute corrected surface G

The user must explicitly select the buried-plate correction. If required physical information is missing, energy analysis reports `needs_data`; the code does not silently substitute raw G.

Positive-G direction is configurable and internally normalized to downward-positive.

## 4. Energy-balance correction sensitivity products

All four products remain separate from original/reconstructed H and LE.

### Mauder 2013 daily Bowen-preserving

A daily EBR scaling factor closes the daily turbulent-energy total while preserving the interval H:LE partition where the method is applicable.

### Charuchittipan 2014 buoyancy partition

The interval imbalance is partitioned using a buoyancy-flux-ratio formulation. Pathological/undefined partitions are not forced; those intervals remain transparently uncorrected.

### De Roo 2018 direct dispersive sensitivity

The implementation uses the published-style dispersive fractions as functions of u*/w* and z/zi, only for convective periods (`L < 0`) with valid scaling variables.

### De Roo 2018 EBR-rescaled dispersive sensitivity

Daily imbalance is constrained by observed daily EBR and partitioned according to the De Roo dispersive H/LE shares.

### De Roo prerequisites

The De Roo paths require:

- effective measurement height
- PBL height (`pbl_height` series or explicit sensitivity value)
- u*
- Monin–Obukhov length
- w* or enough variables to derive w*

The tool reports method availability rather than inventing missing variables. A low-effective-height warning is retained because applicability can be limited near the surface.

These products are **sensitivity scenarios**, not causal attribution of non-closure.

## 5. LE-to-ET conversion

Phase 3 uses:

`lambda(T) = (2.501 - 0.002361 T) × 10^6 J kg⁻¹`

and:

`ET_interval = LE × dt / lambda(T)`

where 1 kg m⁻² water equals 1 mm.

Negative ET is preserved. Air temperature is taken from the mapped tower/reference series. An explicit fallback temperature may be used for missing records and is reported in warnings.

ET is calculated for:

- base LE
- Mauder-corrected LE
- Charuchittipan-corrected LE
- De Roo direct-corrected LE
- De Roo EBR-rescaled LE

when the corresponding correction exists.

## 6. Daily completeness and aggregation

The target interval count per day is inferred from the standardized timestep. Daily ET coverage is calculated from valid base ET intervals.

The default accepted-day threshold is 90% and is editable.

Both raw partial-day totals and threshold-accepted daily totals are retained. Monthly and annual totals sum accepted days and report accepted-day coverage.

## 7. ET uncertainty from Phase 2

When Phase 2 provides empirical LE lower/upper residual-calibrated bounds, Phase 3 converts those to ET using the same temperature-dependent latent heat.

For measured rows, the reconstruction interval collapses to the measured base value. This interval represents **gap-reconstruction uncertainty only**, not total instrument/EC uncertainty.

## 8. External water context

Phase 3 can use:

- ETo
- precipitation
- irrigation/applied water
- SWC

Outputs include ET/ETo, cumulative ET, cumulative ETo and cumulative rain+irrigation.

`water_input_minus_ET_mm` is deliberately labeled a **simple diagnostic**. It is not a full water balance unless runoff, drainage, lateral flow and soil-water storage change are independently accounted for.

## 9. Optional water-file alignment

`browser_engine.add_water_input_file()` accepts separately mapped irrigation/rain/ETo/SWC files.

For source timesteps of about a day or longer, irrigation/rain/ETo totals are allocated only to target intervals with the same calendar date. This preserves daily totals and prevents nearest-neighbor assignment from leaking a daily total into an adjacent date.

Date-only files are supported in this optional loader and treated as calendar-day records.

## 10. Phase-3 exports

The engine can export:

- interval energy/water table
- measured-only daily/monthly/annual energy diagnostics
- correction summary
- Mauder daily correction diagnostics
- daily/monthly/annual ET-water tables
- Markdown Phase-3 report

The browser UI exposes the main Phase-3 tables while the Python engine retains the additional tables for extension in Phase 5.

---

# Regression-test datasets

The supplied real datasets used for Phase-3 regression checks are:

- Esparto EddyPro + CIMIS 226
- Modesto EddyPro + CIMIS 71

Phase-3 tests verify that energy diagnostics run on measured observations, ET works with the imported meteorology, optional buried-plate storage can run when required physical inputs are supplied, missing physical inputs block that mode cleanly, and daily irrigation totals remain conserved after alignment.

See `PHASE3_TEST_REPORT.md` for observed build-test values. These values are regression/smoke-test outputs for the supplied records, not universal expected closure targets.

---

# Remaining phases

Phase 4 will implement the scientific u* threshold, carbon partitioning and footprint modules. Phase 5 will harden the fully integrated application, expand export/report packaging and perform final browser/performance regression testing.

## Phase 4 scientific notes

### u* threshold analysis
FluxGapFill provides two independent threshold estimators: an MPT-style seasonal plateau detector and a CPD-style piecewise breakpoint detector. Nighttime records are stratified by season. The MPT pathway further stratifies by temperature and u* classes. Bootstrap resampling reports threshold distributions rather than only one value. This implementation is original and does not use proprietary Tovi source code.

### Carbon partitioning
The Phase-4 nighttime pathway uses the Lloyd–Taylor temperature-response form. A robust global E0 is fitted to turbulence-screened nighttime NEE and a moving Rref is estimated in overlapping windows. Reco is extrapolated across the full record and GPP is computed from NEE = Reco - GPP, with GPP reported positive for ecosystem uptake. The UI exposes NEE sign convention and u* threshold override.

### Footprints
The footprint climatology uses the Kljun et al. (2015) FFP parameterisation with the mean-wind-speed input branch. Required turbulence inputs are u*, wind direction, Monin–Obukhov length, sigma-v and wind speed, plus measurement height and PBL height. The output is tower-centered east/north footprint density. Optional GeoJSON polygons are converted to tower-local coordinates when project latitude/longitude are supplied and are used to summarize mean footprint contribution.
