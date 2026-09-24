# FluxGapFill 1.0 — User Guide

## 1. Project & files

Upload one primary processed dataset. A supplemental weather/environmental file is optional.

Use **Inspect & auto-detect files** first. FluxGapFill will identify the file structure and propose variable mappings without changing the source file.

### Good primary inputs

- EddyPro full-output tables
- processed Campbell TOA5 logger files
- AmeriFlux/FLUXNET-style half-hourly files
- custom CSV/TXT/Excel tables containing a timestamp and one or more scientific variables

### Not supported as a primary scientific starting point

Raw 10–20 Hz sonic anemometer / gas-analyzer time series that still require eddy-covariance covariance processing.

## 2. Variable mapping

Before processing, verify:

- timestamp convention: start, midpoint or end of interval
- LE, H and NEE/CO2 flux roles
- radiation variables
- meteorological variables
- soil/turbulence variables
- units

Correct uncertain mappings. Do not proceed simply because the software found a similarly named column.

## 3. Data overview

Review:

- start/end date
- averaging interval
- record count
- missing percentage
- longest gaps
- visually obvious discontinuities

Charts are downsampled for display only. Calculations use the full standardized table.

## 4. Quality control

Start with mapped QC flags and physically justified bounds. Robust spike screening is optional and is off by default.

The standardized pre-QC values are preserved so the QC audit remains reproducible.

## 5. Module readiness

Each module reports one of three states:

- **Ready** — required information is present
- **Needs data** — the missing requirements are listed
- **Skipped** — user elected not to run the module

Optional modules never block the core gap-filling workflow.

## 6. Gap filling and validation

Recommended sequence:

1. select target fluxes
2. start with Balanced validation
3. include MDS plus appropriate RF/XGBoost candidates
4. run contiguous blocked validation
5. inspect RMSE, MAE, bias, R2 and day/night/seasonal diagnostics
6. then fill the real gaps

Use Comprehensive validation for final scientific analysis when the record is long enough and the additional runtime is acceptable.

Adaptive filling uses validation information while also checking whether the predictors required by a candidate model exist inside the actual gap.

## 7. Results

Do not use only the final filled time series. Also inspect/download:

- method provenance
- gap duration/class
- MDS donor diagnostics
- validation RMSE
- empirical blocked-validation interval where available

Measured/QC-accepted values are not overwritten.

## 8. Energy & water

### Energy closure

Typical required variables:

- H
- LE
- Rn
- G

Primary energy-closure diagnostics are based on measured/QC-accepted H and LE so gap filling does not artificially improve observational closure.

### ET

ET can be derived from LE. Air temperature is used for temperature-dependent latent heat when available.

Optional context:

- ETo
- precipitation
- irrigation/applied water
- SWC

The water-input-minus-ET output is a diagnostic, not a full soil-water balance unless all storage/loss terms are independently accounted for.

## 9. u*, carbon and footprint

### u* threshold

Recommended inputs include:

- NEE
- u*
- temperature
- radiation

MPT and CPD results are shown independently. Inspect bootstrap and seasonal behavior rather than treating one number as unquestionable.

### Carbon partitioning

Requires NEE and temperature. Turbulence screening is recommended. The current module uses a nighttime respiration approach and reports GPP positive for uptake.

### Footprint climatology

Requires turbulence/site variables such as:

- u*
- wind direction
- Monin-Obukhov length
- sigma-v
- wind speed
- measurement height
- PBL height

Canopy height / displacement height and GeoJSON AOI information are optional but useful.

## 10. Export & figures

### Data exports

Keep the full provenance and validation outputs with any compact result used for publication or archiving.

### Journal Figure Studio

Choose a rendered chart, then select:

- single-column 85 mm
- double-column 180 mm
- square 85 × 85 mm

Use **PNG · 600 DPI** when a raster file is required. Use **SVG · vector** when accepted by the journal.

Default figure export deliberately removes the in-figure title because most journals use an external figure caption. Enable the title only when needed.

The export engine uses a white print background regardless of Light/Dark/Night application appearance.

## Privacy

Input files remain local to the current browser session. Closing/reloading the application clears its working copies and unsaved results. Downloaded files remain on the user's computer.
