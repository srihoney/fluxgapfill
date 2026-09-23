# FluxGapFill Phase 4 test report

Build: `20260923p4`

## Scope
Phase 4 adds three optional post-processing modules on top of the complete Phase 1–3 package:

1. u* threshold analysis using independent MPT-style and CPD-style implementations with seasonal bootstrap summaries.
2. Nighttime NEE partitioning to ecosystem respiration (Reco) and GPP using a Lloyd–Taylor temperature response and a moving Rref.
3. Kljun et al. (2015) footprint climatology with optional GeoJSON AOI contribution summaries.

No raw 10–20 Hz eddy-covariance processing is included.

## Regression data
The Phase-4 engine was smoke-tested with the existing Esparto and Modesto EddyPro datasets already used to validate Phases 1–3.

### Esparto smoke test
- u* module: ready
- selected MPT threshold in the reduced 3-bootstrap smoke test: ~0.279 m s-1
- carbon module: ready
- footprint module: ready when measurement height, canopy height and a PBL fallback were supplied
- valid footprint intervals in the reduced test: 144

### Modesto smoke test
- u* module: ready
- selected MPT threshold in the reduced 3-bootstrap smoke test: ~0.251 m s-1
- carbon module: ready
- footprint module: ready when measurement height, canopy height and a PBL fallback were supplied
- valid footprint intervals in the reduced test: 119

These values are regression/smoke-test outputs, not recommended site thresholds. Production users should use the default 100 bootstrap iterations (or more if the threshold distribution is unstable), inspect seasonal results, and apply site-specific QC.

## Guardrails verified
- Phase-4 modules return `needs_data` rather than silently inventing missing inputs.
- Carbon partitioning can use the selected u* threshold automatically or a user-supplied override.
- Footprint processing requires u*, wind direction, Monin–Obukhov length, sigma-v, wind speed, measurement height and PBL height.
- If displacement height is not supplied, the UI explicitly uses 0.67 × canopy height as the fallback.
- AOI/land-cover overlay is optional and does not block footprint climatology.
- All calculations and exports remain browser-local.

## Interpretation cautions
- MPT and CPD are independent implementations based on published/operational method concepts; they are not copies of proprietary Tovi source code.
- u* threshold estimates are data- and QC-sensitive and should be inspected rather than treated as a single unquestionable number.
- Nighttime partitioning assumes nighttime NEE is respiration after turbulence screening; this assumption may be weak for some ecosystems/periods.
- Footprint results are sensitive to measurement height, canopy/displacement assumptions, PBL height and turbulence inputs.
