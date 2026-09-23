# FluxGapFill 1.0

**Production build:** `20260923v100`  
**Target site:** `https://fluxgapfill.pages.dev/`

FluxGapFill is a local-first scientific web application for **already-computed fluxes and half-hourly/hourly environmental data**. It does not process raw 10–20 Hz sonic/IRGA signals into eddy-covariance fluxes.

The production release integrates the complete workflow developed across Phases 1–4 into one clean application:

- Universal processed-data import and variable mapping
- transparent quality control
- MDS / Random Forest / XGBoost gap filling
- blocked calendar-gap validation and adaptive filling
- energy-balance diagnostics and correction sensitivity products
- LE-to-ET and water-input analysis
- u* threshold diagnostics
- nighttime carbon partitioning
- footprint climatology and optional GeoJSON AOI analysis
- local scientific exports
- journal-oriented figure export

## Privacy and processing model

Scientific input files are not uploaded to FluxGapFill. The browser copies selected files into Pyodide's temporary in-browser filesystem/memory and performs the calculations locally. Working copies and in-memory results disappear when the browser session ends unless the user downloads them. Original files on the user's computer are never modified.

## Supported input routes

FluxGapFill accepts processed data from:

- EddyPro full-output TXT
- Campbell Scientific TOA5
- AmeriFlux / FLUXNET-style tables
- generic CSV / TSV / TXT / DAT
- Excel / spreadsheet files supported by the browser Python runtime
- CIMIS as an automatically recognized supplemental weather source
- generic supplemental weather/environmental files
- optional daily/sub-daily water-management files
- optional GeoJSON AOI polygons

When a format cannot be confidently auto-detected, the user can explicitly map timestamp, variables and units.

## Appearance

FluxGapFill 1.0 provides three user-selectable themes:

- **Light** — high-contrast daytime workspace
- **Dark** — navy scientific workspace
- **Night** — near-black low-glare workspace

The selected theme is saved locally in the browser. Journal figure exports are always rendered on a white print background, independent of the application theme.

## Built-in guidance

A persistent workflow coach recommends the next step. The **Guide** button and inline help strips explain:

- what data are required
- what each module does
- what is optional
- what to run next
- when a module should be skipped

The application does not silently invent missing site metadata for optional analyses.

## Journal Figure Studio

Every rendered Plotly chart can be sent to **Journal Figure Studio** from its chart card or from **Export & figures**.

Available presets:

- single column: 85 mm
- double column: 180 mm
- square: 85 × 85 mm

Export formats:

- **PNG with 600-DPI metadata**
- **SVG vector**

Journal exports use:

- white background
- regular-weight Arial/sans-serif typography
- restrained line/marker weights
- automatic margins
- configurable legend placement
- optional in-figure title (off by default)
- optional light grid lines (off by default)
- colorblind-friendly figure palette for standard traces

The 600-DPI PNG writer inserts a PNG `pHYs` chunk corresponding to approximately 600 dpi. The single-column preset exports approximately 2010 px wide and the double-column preset approximately 4254 px wide.

## Scientific workflow

1. **Project & files** — choose processed flux/environmental files.
2. **Variable mapping** — verify timestamp convention, roles and units.
3. **Data overview** — inspect coverage, record length and gap structure.
4. **Quality control** — apply conservative transparent screening.
5. **Readiness** — see which optional modules can run and what is missing.
6. **Gap filling** — run blocked validation, compare candidates, reconstruct real gaps.
7. **Results** — inspect measured vs filled data, method provenance and empirical reconstruction intervals.
8. **Energy & water** — optional closure, ET, ETo, precipitation, irrigation and SWC analyses.
9. **Carbon & footprint** — optional u*, carbon partitioning, footprint and AOI analysis.
10. **Export & figures** — download numerical audit products, reports and publication figures.

See `USER_GUIDE.md` for a practical step-by-step guide and `METHODS_AND_REFERENCES.md` for scientific method notes.

## Deployment

This package is intended for the existing Cloudflare Pages project `fluxgapfill`.

Recommended Cloudflare Pages settings:

- Project name: `fluxgapfill`
- Production branch: `main`
- Framework preset: `None`
- Build command: `exit 0`
- Build output directory: `public`
- Root directory: blank

After deployment, open:

`https://fluxgapfill.pages.dev/deployment-check.html`

It should show build `20260923v100`.

## Browser runtime

The site serves its UI and Plotly bundle statically from Cloudflare Pages. Scientific Python is loaded into a module Web Worker using Pyodide. The worker currently attempts multiple Pyodide CDN mirrors/versions and loads NumPy, pandas, SciPy, scikit-learn, XGBoost, timezone support and spreadsheet support.

Because model fitting runs in the user's browser rather than on a PythonAnywhere-style server process, there is no fixed Cloudflare compute timeout for the scientific job. Practical limits are browser memory, CPU, tab lifetime and the size/complexity of the validation run.

## Important scope boundary

FluxGapFill starts from computed fluxes or environmental time series. It does **not** replace EddyPro or another raw eddy-covariance processing package for rotation, time-lag optimization, spectral corrections or covariance computation from high-frequency signals.
