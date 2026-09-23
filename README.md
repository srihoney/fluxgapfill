# FluxGapFill — Phase 1 FINAL

**Build:** `20260923p1`  
**Target site:** `https://fluxgapfill.pages.dev/`

FluxGapFill is a local-first scientific web application for **already-computed fluxes and half-hourly/hourly environmental data**. Phase 1 establishes the Universal Import + Project Foundation while retaining the working MDS/RF/XGBoost gap-filling workflow from the previous release.

> FluxGapFill does **not** process raw 10–20 Hz sonic/IRGA data into eddy-covariance fluxes. Raw EC processing should be completed in EddyPro or equivalent software first.

## What is included in Phase 1

### Universal Import

The importer can recognize or guide users through:

- EddyPro full-output TXT
- CIMIS hourly CSV
- Campbell Scientific TOA5
- AmeriFlux / FLUXNET-style files
- generic CSV / TSV / delimited TXT/DAT
- Excel workbooks (`.xlsx`, `.xls`, `.xlsb`, `.ods`) through `python-calamine` in the browser
- optional supplemental weather/environmental files in the same supported generic formats

For standard formats, FluxGapFill auto-detects as much as it can. For custom/manual files, the user receives an explicit **Variable Mapping** screen and can correct columns, units, timestamp convention, and project metadata before processing. The importer never intentionally invents a hidden variable mapping.

### Standard internal variables

Phase 1 can standardize common fields including:

- LE, H, NEE/CO2 flux and QC flags
- incoming shortwave radiation, net radiation, soil heat flux
- air temperature, RH, VPD, vapor pressure, pressure
- wind speed and direction
- precipitation
- soil water content and soil temperature
- friction velocity (`u*`), Monin–Obukhov length and lateral wind SD when supplied
- measured ET and reference ETo

Common units can be converted to the canonical units used internally. The mapping screen exposes the selected units so users can correct uncertain metadata before processing.

### Capability-aware project workspace

After import, FluxGapFill checks the standardized dataset and reports whether each module is **Ready**, **Needs data**, or can be **Skipped**. Phase 1 provides readiness checks for:

- gap filling & validation
- energy balance
- ET / water analysis
- u* threshold analysis
- carbon partitioning
- footprint analysis
- irrigation / rainfall overlays

Only gap filling/validation is a fully active scientific analysis module in this Phase 1 package. The other cards establish the required-data workflow for the later phases; they do not falsely claim the later Tovi-like analyses are already implemented.

If a module needs site information such as measurement height or canopy height, the user can supply it in that module area. If the user does not need a module, it can be skipped without blocking the rest of the project.

### Existing gap-filling science retained

Phase 1 retains the validated workflow already developed for the project:

- EddyPro QC screening and physical screens
- timestamp normalization to a regular midpoint grid
- optional CIMIS alignment
- Reichstein-style MDS
- Random Forest candidate models
- XGBoost candidate models
- current/tower/cross-flux predictor profiles
- contiguous calendar-gap validation
- adaptive method selection from validation results
- per-record provenance
- LE-to-ET conversion
- compact/full CSV exports and validation export

## Included example files

`public/examples/` contains synthetic demonstration files so Universal Import can be tested without requiring additional field datasets:

- `campbell_toa5_example.dat`
- `fluxnet_style_example.csv`
- `generic_manual_example.csv`
- `generic_manual_example.xlsx`
- `supplemental_weather_example.csv`

These examples are synthetic demonstration data and are not field observations.

## Privacy and file lifecycle

Scientific files are processed **locally in the visitor's browser**.

- no scientific-file upload API
- no application database
- no Cloudflare storage of the user's EC/weather files
- no GitHub storage of user-selected scientific files
- temporary copies exist only in the browser/Pyodide runtime during the page session
- closing/reloading the page clears application runtime state
- original files on the user's computer are not modified
- exported files are saved by the user's browser to the user's chosen/default download location

## Cloudflare Pages deployment

If the existing `fluxgapfill` Pages project is already connected to GitHub, replace the repository contents with the contents of this package and commit/push to `main`.

For a new Pages deployment use:

- **Project name:** `fluxgapfill`
- **Production branch:** `main`
- **Framework preset:** `None`
- **Build command:** `exit 0`
- **Build output directory:** `public`
- **Root directory:** leave blank

Expected site URL, if the project name belongs to your account:

`https://fluxgapfill.pages.dev/`

After deployment open:

`https://fluxgapfill.pages.dev/deployment-check.html`

It must report:

`Build: 20260923p1`

If an older build appears, wait for the GitHub deployment to finish and then hard-refresh the browser once.

## Browser runtime

The application uses a **module-type Web Worker** and Pyodide 314.0.x. This avoids the classic-worker `importScripts()` incompatibility encountered in the earlier package.

The worker loads the scientific stack locally in the browser, including NumPy, pandas, SciPy, scikit-learn, XGBoost, timezone data, and `python-calamine` for Excel import. Initial startup can take longer because the browser must download the scientific runtime; subsequent visits are normally faster due to browser caching.

Large blocked-validation/model-training runs are limited primarily by the user's CPU/RAM rather than a PythonAnywhere-style server execution timeout. Do not close/reload the tab while a calculation is running.

## Recommended first test after deployment

1. Confirm `deployment-check.html` shows `20260923p1`.
2. Open the main app and wait for **Python + XGBoost ready**.
3. Try one of the files under **Built-in examples**.
4. Inspect the detected format and variable mapping.
5. Process the mapped project.
6. Review the Module Readiness cards.
7. For a real EddyPro project, upload EddyPro as the primary file and CIMIS as the optional supplemental file.

## Phase status

- **Phase 1:** Universal Import + Project Foundation — **included in this package**
- **Phase 2:** QC + Gap Filling + Validation expansion — planned
- **Phase 3:** Energy + Water / ET analysis — planned
- **Phase 4:** Carbon + u* + Footprint — planned
- **Phase 5:** Full integration, hardening and production release — planned
