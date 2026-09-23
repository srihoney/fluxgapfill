# FluxGapFill science engine notes

## Implemented in this package
- EddyPro full-output TXT ingestion with QC screening and timestamp regularization.
- Optional hourly CIMIS CSV alignment, including ETo, precipitation, solar/net radiation, vapor pressure, air temperature, RH, dew point, wind, wind direction, and soil temperature.
- Reichstein-style MDS filling.
- Random Forest and XGBoost candidates with current, tower, and cross-flux profiles.
- Calendar-blocked validation for 1/7/14/30/60-day outages (user-selectable).
- Adaptive model selection by the site's blocked-validation RMSE.
- Full per-record provenance and validation RMSE fields.
- ET conversion from final LE using temperature-dependent latent heat of vaporization.

## Deliberate choices
The browser version uses one CPU thread for tree models because it runs inside WebAssembly. The scientific model definitions otherwise follow the v2 candidate engine used in the real-data benchmarking. Measured accepted observations are never overwritten.

Blocked-validation RMSE is kept separate from uncertainty. The exported `*_validation_lower95` / `*_validation_upper95` fields are empirical RMSE-based diagnostic bands, not formal confidence intervals.

## Recommended operating sequence
1. Load EddyPro + CIMIS.
2. Review completeness and predictor availability.
3. Start with 1 validation window per gap duration.
4. Increase to 2–3 windows for final scientific evaluation.
5. Enable adaptive selection and create the final product.
6. Export the validation table and the full provenance CSV together.
