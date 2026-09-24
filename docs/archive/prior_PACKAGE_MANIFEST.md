# FluxGapFill 1.0 — Package Manifest

## Root

- `README.md` — production overview and deployment
- `USER_GUIDE.md` — practical workflow instructions
- `METHODS_AND_REFERENCES.md` — scientific method notes
- `RELEASE_NOTES.md` — 1.0 changes
- `FINAL_TEST_REPORT.md` — verification summary
- `CLOUDFLARE_SETUP.txt` — concise deployment instructions
- `.gitignore`
- `docs/archive/` — development-stage reports retained for traceability

## `public/`

Cloudflare Pages publish directory.

### UI

- `index.html`
- `assets/css/app.1.0.0.css`
- `assets/js/app.1.0.0.js`
- `assets/js/py-worker.1.0.0.js`
- `assets/vendor/plotly-6.5.2.min.js`
- `favicon.svg`
- `deployment-check.html`
- `_headers`

### Scientific Python

- `py/universal_import.py`
- `py/data_processor.py`
- `py/scientific_qc.py`
- `py/phase2_qc.py`
- `py/gap_filler.py`
- `py/advanced_ml_gap_filler.py`
- `py/validation_engine.py`
- `py/external_sources.py`
- `py/calibration_utils.py`
- `py/phase3_energy_water.py`
- `py/phase4_carbon_footprint.py`
- `py/browser_engine.py`

### Example data

- Campbell TOA5 example
- FLUXNET-style example
- generic CSV example
- generic Excel example
- supplemental weather example
- daily irrigation example
