# FluxGapFill — Phase 2 FINAL

**Build:** `20260923p2`  
**Target site:** `https://fluxgapfill.pages.dev/`

FluxGapFill is a local-first scientific web application for **already-computed fluxes and half-hourly/hourly environmental data**. Phase 2 keeps the Phase 1 Universal Import foundation and adds the full **QC + Gap Filling + Blocked Validation** workspace.

> FluxGapFill does **not** process raw 10–20 Hz sonic/IRGA data into eddy-covariance fluxes. Raw EC processing should be completed in EddyPro or equivalent software first.

## Phase 2 scientific workflow

### 1. Universal Import retained from Phase 1

Supported project inputs include:

- EddyPro full-output TXT
- CIMIS hourly CSV
- Campbell Scientific TOA5
- AmeriFlux / FLUXNET-style files
- generic CSV / TSV / delimited TXT/DAT
- Excel-compatible workbooks supported by `python-calamine`
- optional supplemental environmental/weather files
- manual variable mapping when automatic detection is uncertain

Known formats use specialized parsing where available. Custom files are mapped into one internal schema before any scientific processing.

### 2. Configurable Phase 2 QC

The Quality Control workspace includes:

- preservation of the standardized pre-QC values
- explicit per-record QC reason flags
- broad physical/range screening
- editable LE/H/NEE limits
- optional conservative rolling Hampel-style spike screening
- configurable spike window and robust-sigma threshold
- before/after diagnostic plots
- per-variable QC audit table
- downloadable QC audit CSV

The additional Phase 2 spike screen is **off by default**. It is optional because aggressive automatic spike removal can reject genuine flux events. EddyPro/source QC already applied during import remains upstream of this layer.

### 3. Two MDS variants

Phase 2 exposes:

- `MDS_Reichstein05`
- `MDS_Vekuri23`

The Reichstein-style search hierarchy is retained. The Vekuri23 daytime LUT aggregation separates lower- and higher-radiation donor subsets before combining them; nighttime/fallback behavior is handled separately.

MDS output keeps:

- method/search-window QC
- donor count
- donor standard deviation
- search-window provenance

Donor standard deviation is **not** labeled as a calibrated 95% prediction interval.

### 4. Expanded Random Forest and XGBoost candidates

For LE/H/NEE where predictors are available, Phase 2 can benchmark:

- `RF_external`
- `RF_tower`
- `RF_cross`
- `XGB_external`
- `XGB_tower`
- `XGB_cross`

The profiles distinguish external-reference predictors, tower/environmental predictors, and optional complementary-flux information. The adaptive filler checks whether the driver regime required by a candidate is actually available inside a real gap before selecting that model.

Target-derived variables are not used as hidden predictors of the same target. In particular, EddyPro ET is not used to predict LE.

### 5. Contiguous blocked validation

Validation uses artificial **calendar outages**, not random-row cross-validation. Default selectable durations are:

`1, 3, 7, 14, 30, 60 days`

The validator:

- masks contiguous calendar windows
- scores only target observations that were genuinely present before masking
- supports multiple separated windows per gap duration
- does not require every record inside the calendar block to have been originally observed
- reports prediction coverage, RMSE, MAE, bias and R²
- reports day/night diagnostics
- reports seasonal diagnostics (DJF/MAM/JJA/SON)
- stores held-out residual distributions for empirical reconstruction intervals

Validation intensity choices are available in the interface. **Quick (1 window per duration)** is the default for browser practicality; stronger settings take longer.

### 6. Adaptive model selection and provenance

After validation, Adaptive Fill can select a validated candidate by target and nearest tested gap duration, subject to actual predictor availability inside each real gap.

For each target, exported provenance can include:

- original value
- filled value
- source/model
- method detail
- gap ID
- gap length
- gap-risk class
- MDS fill QC when MDS is used
- MDS donor SD/count/window
- validation RMSE associated with the selected candidate
- empirical 95% residual interval when enough held-out residuals are available

Measured/QC-accepted target records are never overwritten by the gap-filling stage.

### 7. Gap-duration classes

Phase 2 classifies gaps as:

- A: ≤1 day
- B: >1–3 days
- C: >3–7 days
- D: >7–14 days
- E: >14–30 days
- F: >30–60 days
- G: >60–90 days
- H: >90 days

This is kept separate from canonical MDS method/search-window quality.

### 8. Export package

The browser can export:

- compact results CSV
- full provenance CSV
- standardized/QC dataset CSV
- QC audit CSV
- validation-detail CSV
- validation-summary CSV
- day/night + seasonal diagnostics CSV
- empirical interval-calibration CSV
- reproducible validation report in Markdown

LE-to-ET conversion and daily ET summaries are retained for continuity; the full Energy + Water/ET analysis workspace belongs to Phase 3.

## Important validation interpretation

The site-specific blocked-validation windows serve two purposes in the current Phase 2 workflow: **model selection** and **empirical residual calibration**. Therefore the displayed validation performance is a reconstruction/selection diagnostic, not an independent untouched final test set. A separate nested/holdout evaluation is appropriate when an unbiased final comparative performance estimate is required for a publication.

RF/XGBoost reconstruction also uses observations available before and after an artificial gap. This is appropriate for post-processing/gap reconstruction of a completed record and should not be described as forecasting future fluxes.

## Privacy and file lifecycle

Scientific files are processed **locally in the visitor's browser**.

- no scientific-file upload API
- no application database
- no Cloudflare storage of the user's flux/weather files
- no GitHub storage of user-selected scientific files
- temporary files and model state live only in the browser/Pyodide session
- refreshing/closing the page clears application runtime state
- original input files are not modified
- downloaded exports remain on the user's computer

## Cloudflare Pages deployment

For the existing `fluxgapfill` Pages project, replace the GitHub repository contents with the contents of this package and commit/push to `main`.

Cloudflare Pages settings:

- **Project name:** `fluxgapfill`
- **Production branch:** `main`
- **Framework preset:** `None`
- **Build command:** `exit 0`
- **Build output directory:** `public`
- **Root directory:** leave blank

After deployment open:

`https://fluxgapfill.pages.dev/deployment-check.html`

It must report:

`Build: 20260923p2`

If an older build appears, wait for the production deployment to finish and hard-refresh the browser once.

## Browser runtime

The application uses a module-type Web Worker with Pyodide 314.x and browser-side NumPy, pandas, SciPy, scikit-learn, XGBoost, timezone support and `python-calamine`. Scientific computation therefore occurs on the user's CPU/RAM rather than on a PythonAnywhere-style application worker.

Comprehensive blocked validation can involve many model fits. Start with **Quick** validation to verify the project, then use Balanced/Strong/Comprehensive only when the additional validation depth is useful.

## Phase status

- **Phase 1:** Universal Import + Project Foundation — included
- **Phase 2:** QC + Gap Filling + Validation — **included in this package**
- **Phase 3:** Energy + Water / ET analysis — not yet implemented as a full module
- **Phase 4:** Carbon + u* + Footprint — not yet implemented as full modules
- **Phase 5:** Full integration, hardening and production release — planned

The Phase 3/4 readiness cards remain capability-aware input guides. They do not claim those later scientific analyses are already active.
