# FluxGapFill Phase 3 FINAL — Verification Report

**Build:** `20260923p3`

This report records regression/smoke tests run against the Phase-3 package before packaging. The numerical values below are **test outputs for the supplied datasets**, not universal expected closure or ET values.

## 1. Static and syntax verification

Passed:

- `python -m compileall` for all Python modules in `public/py`
- `node --check` for `app.20260923p3.js`
- `node --check` for `py-worker.20260923p3.js`
- all literal local `src`/`href` files referenced by `index.html` exist
- all 110 literal `$('#...')` DOM IDs referenced in the application JavaScript exist in `index.html`
- every Python file listed in the Phase-3 worker `PYFILES` manifest exists

The final worker is a **module-type** Pyodide worker; no classic `importScripts()` worker is used.

## 2. Real-data Phase-3 smoke test — Esparto

Inputs:

- Esparto EddyPro full-output file
- CIMIS station 226 hourly file
- default `G as supplied` mode
- default 90% daily ET completeness threshold

Standardized records: **5,843**

Measured-only primary energy diagnostic:

- status: `ready`
- primary intervals: **2,610**
- ratio-of-sums EBR: **0.7568434141**
- OLS slope: **0.7085166133**
- R²: **0.9098342458**

ET/water smoke-test state before Phase-2 reconstruction:

- daily calendar rows: **123**
- days meeting the 90% ET completeness threshold: **41**
- accepted-day ET sum: **201.4174541 mm**
- ETo source: `eto_ref`

The accepted-day total above is intentionally not treated as a full-season ET total because many days do not pass the completeness threshold until gaps are reconstructed.

Correction availability in this default run:

- Mauder daily-Bowen product: ready; 3,843 intervals changed
- Charuchittipan buoyancy product: ready; 2,072 intervals changed
- De Roo direct: `needs_data` without explicit measurement-height/PBL information
- De Roo EBR-rescaled: `needs_data` without explicit measurement-height/PBL information

This confirms the intended optional-module behavior: De Roo products do not invent required site/scaling inputs.

## 3. Real-data Phase-3 smoke test — Modesto

Inputs:

- Modesto EddyPro full-output file
- CIMIS station 71 hourly file
- default `G as supplied` mode
- default 90% daily ET completeness threshold

Standardized records: **34,092**

Measured-only primary energy diagnostic:

- status: `ready`
- primary intervals: **10,438**
- ratio-of-sums EBR: **0.6572775174**
- OLS slope: **0.5208967042**
- R²: **0.5273592221**

ET/water smoke-test state before Phase-2 reconstruction:

- daily calendar rows: **711**
- days meeting the 90% completeness threshold: **218**
- accepted-day ET sum: **857.9106074 mm**
- ETo source: `eto_ref`

Again, this is a completeness-screened smoke-test total, not a claim of complete site-period ET before gap reconstruction.

Correction availability in the default run:

- Mauder: ready; 22,369 intervals changed
- Charuchittipan: ready; 9,412 intervals changed
- De Roo methods: correctly remained `needs_data` when required site/PBL information was not supplied

## 4. Optional buried-plate soil heat-storage test

Esparto was rerun with explicit test inputs:

- plate depth: 8 cm
- bulk density: 1,400 kg m⁻³
- dry-soil heat capacity: 840 J kg⁻¹ K⁻¹
- effective measurement height: 4 m
- PBL-height sensitivity value: 1,000 m

Results:

- storage correction status: `ready`
- accepted storage records: **5,492**
- rejected storage records by rate/flux plausibility QC: **122**
- Mauder ready: 3,843 intervals changed
- Charuchittipan ready: 1,971 intervals changed
- De Roo direct ready: 1,923 eligible convective intervals
- De Roo EBR-rescaled ready: 1,923 eligible convective intervals
- the low-effective-height applicability warning was raised as designed

These values demonstrate code-path execution only. The 8 cm / 1,400 kg m⁻³ / 1,000 m values were explicit functional test inputs and are **not site recommendations**.

A second test selected buried-plate correction without supplying plate depth or bulk density. Energy analysis returned:

- status: `needs_data`
- reason: `Plate depth and bulk density are required for soil heat-storage correction.`

No silent fallback to raw G occurred.

## 5. Date-only daily irrigation conservation

Included test file:

`public/examples/daily_irrigation_example.csv`

Input daily totals:

- 2024-06-01: 12 mm
- 2024-06-02: 0 mm
- 2024-06-03: 8 mm
- 2024-06-04: 0 mm
- 2024-06-05: 15 mm

Universal/water-file inspection detected:

- `Date` as the date field
- `Irrigation_mm` as irrigation
- source timestep: 1,440 minutes

After alignment to the Esparto half-hour grid, daily sums were conserved as:

- 12.0, 0.0, 8.0, 0.0, 15.0 mm respectively

This verifies the Phase-3 date-only daily-water path and same-calendar-day apportionment.

## 6. Phase-2 → Phase-3 integration smoke test

Esparto was processed through:

`Import → Phase-2 QC → one 7-day blocked window → adaptive gap filling → Phase 3`

Candidates in this compact regression test:

- `MDS_Reichstein05`
- `XGB_cross`

Observed 7-day RMSE values for that selected held-out window:

- H / MDS: **38.47 W m⁻²**
- H / XGB-cross: **24.91 W m⁻²**
- LE / MDS: **27.84 W m⁻²**
- LE / XGB-cross: **21.72 W m⁻²**

Adaptive filling reported:

- LE reconstructed records: **864**
- H reconstructed records: **833**

Phase 3 then completed successfully on the reconstructed product:

- energy status: `ready`
- measured-only EBR remained approximately **0.757**
- water table contained **123** daily rows

The measured-only EBR remaining unchanged after reconstruction is an important regression check: Phase-2 gap filling does not overwrite the measured observations used for the primary Phase-3 closure diagnostic.

## 7. Synthetic exact-closure invariant

A four-day, 30-minute synthetic dataset was constructed with active-period values:

- Rn = 300 W m⁻²
- G = 50 W m⁻²
- H = 100 W m⁻²
- LE = 150 W m⁻²

Thus `Rn-G = H+LE = 250 W m⁻²` exactly.

Phase-3 output:

- active intervals: **96**
- ratio-of-sums EBR: **1.0**
- AE-vs-TE RMSE: **0.0 W m⁻²**
- through-origin slope: **1.0**
- OLS slope/intercept/R² intentionally reported as undefined for the constant-AE edge case rather than issuing a rank-deficient fit warning
- first-day ET: **2.65358982 mm d⁻¹** at the specified synthetic LE/temperature pattern
- daily completeness: **100%**

## 8. Phase-3 export smoke test

After a real Esparto Phase-3 run, non-empty exports were generated for:

- interval energy/water
- daily energy
- monthly energy
- annual energy
- correction summary
- correction daily
- daily ET/water
- monthly ET/water
- annual ET/water
- Phase-3 Markdown report

## 9. Known interpretation limits retained intentionally

- Energy-closure correction products are sensitivity scenarios, not causal attribution.
- De Roo methods are conditional on required turbulence/site/PBL inputs and include an applicability warning at low effective measurement height.
- Phase-2 empirical LE intervals propagated to ET describe gap-reconstruction uncertainty only.
- `rain + irrigation - ET` is a diagnostic, not a complete water balance.
- Daily ET below the configured completeness threshold is preserved as a raw partial-day value but not accepted as a complete daily total.
- The Phase-4 u*, carbon and footprint cards are readiness/input guides only in this release.

## Result

**PASS — Phase-3 science engine, Phase-1/2 integration, optional-water alignment, local export paths and static package checks completed for build `20260923p3`.**
