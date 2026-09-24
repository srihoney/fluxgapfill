# FluxGapFill Phase 2 FINAL — Verification Report

**Build:** `20260923p2`

This report records local regression and scientific-engine checks performed before packaging. It documents software behavior; it is not a substitute for independent publication-specific validation.

## Static release checks

Verified:

- every Python module byte-compiles successfully
- release application JavaScript passes `node --check`
- release module-worker JavaScript passes `node --check`
- HTML parses successfully with no duplicate element IDs
- `index.html` references the Phase 2 release JavaScript

## Phase 1 import regression

The Phase 1 universal-import behavior was retained and regression-tested with:

- real Esparto EddyPro + CIMIS
- real Modesto EddyPro + CIMIS
- synthetic Campbell TOA5
- synthetic FLUXNET/AmeriFlux-style CSV
- synthetic generic CSV
- synthetic generic Excel
- synthetic supplemental-weather input

The purpose of the synthetic files is parser/mapping verification only; they are not scientific field datasets.

## Esparto real-data Phase 2 test

Project load produced a regularized 30-minute dataset with **5,843 rows**.

With the optional Phase 2 spike screen disabled:

- variables screened: 14
- range rejections: 2
- additional spike rejections: 0

A one-window, 1-day LE blocked-validation smoke test produced:

| Candidate | RMSE (W m-2) | Held-out n | Prediction coverage |
|---|---:|---:|---:|
| MDS_Reichstein05 | 20.64 | 38 | 1.00 |
| MDS_Vekuri23 | 21.14 | 38 | 1.00 |
| XGB_cross | 21.25 | 38 | 1.00 |

These values are a software regression/smoke test for one deterministic selected validation window, not a general ranking of the methods.

Adaptive filling completed successfully. In this test the validated Reichstein05 candidate was selected for the real LE gaps and filled 864 records while preserving 4,979 measured/QC-accepted LE records. A fixed Vekuri23 reconstruction also completed successfully for the same eligible gaps.

## Modesto real-data Phase 2 test

Project load produced a regularized 30-minute dataset with **34,092 rows**.

With the optional Phase 2 spike screen disabled:

- variables screened: 14
- range rejections: 6
- additional spike rejections: 0

A one-window, 14-day LE blocked-validation smoke test produced:

| Candidate | RMSE (W m-2) | Held-out n | Prediction coverage |
|---|---:|---:|---:|
| MDS_Reichstein05 | 42.81 | 617 | 1.00 |
| MDS_Vekuri23 | 42.56 | 617 | 1.00 |
| XGB_tower | 31.18 | 617 | 1.00 |
| XGB_cross | 23.92 | 617 | 1.00 |

Again, these results describe one deterministic held-out calendar window. The production interface allows multiple windows per duration because performance varies with season, site state, gap length and driver availability.

## Target-leakage check

During development, EddyPro `ET` was identified as target-derived from LE. It has therefore been excluded from LE model predictors. This prevents artificially optimistic LE reconstruction caused by giving a model a variable derived from the target it is supposed to reconstruct.

## QC behavior check

The optional rolling spike screen was tested separately. Because such a screen can reject genuine high-frequency flux events depending on site behavior and parameter choice, the release UI intentionally leaves it **off by default**. Users can enable it and inspect the before/after plot and audit counts before continuing.

## Interval/provenance check

Verified that:

- MDS donor SD is exported separately from prediction intervals
- empirical residual quantiles are generated only when enough held-out residuals exist
- adaptive fill records the selected source/method per reconstructed record
- measured records remain marked as measured and are not overwritten
- gap-duration class is stored separately from MDS method/search-window QC

## Interpretation caveat

The same blocked-validation exercise is used for site-specific candidate selection and empirical residual calibration in Phase 2. Therefore the displayed metrics are reconstruction/model-selection diagnostics, not an untouched independent final test. Publication-grade model comparison can add nested selection/holdout testing where needed.
