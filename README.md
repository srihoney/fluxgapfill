# FluxGapFill — GitHub + Cloudflare Pages package

A local-first eddy-covariance gap-filling workspace using Pyodide in the browser.

## Production URL requested

Create the Cloudflare Pages project with the exact project name:

`fluxgapfill`

If that Pages project name is available in your Cloudflare account, the production URL will be:

`https://fluxgapfill.pages.dev/`

## Deployment

1. Create a new GitHub repository (for example `fluxgapfill`).
2. Upload the **contents of this package** to the repository root. Do not upload the outer ZIP as the website.
3. In Cloudflare: **Workers & Pages → Create → Pages → Connect to Git**.
4. Select the GitHub repository.
5. Use:
   - Project name: `fluxgapfill`
   - Production branch: `main`
   - Framework preset: `None`
   - Build command: `exit 0`
   - Build output directory: `public`
   - Root directory: leave blank
6. Deploy.
7. Open `https://fluxgapfill.pages.dev/deployment-check.html` and confirm build `20260922a`.

No Cloudflare secret or paid service is required for this version. Static assets are hosted by Pages, while scientific calculations execute on the visitor's computer. Pyodide and its scientific packages are loaded from jsDelivr on first use and are then normally cached by the browser.

## Current scientific workflow

- EddyPro full-output TXT parsing
- QC 0/1 default screening and physical screens
- interval-end to midpoint timestamp conversion
- optional CIMIS hourly CSV alignment
- MDS / Reichstein-style gap filling
- Random Forest candidate models
- XGBoost candidate models
- tower/external/cross-flux predictor profiles
- contiguous calendar-gap validation
- adaptive method selection by validated gap duration
- LE/H provenance fields
- ET derived from final LE
- compact/full CSV export

## Data privacy

The app has no upload API, database, or server-side scientific processing. Selected files are passed to a Web Worker and written only to Pyodide's in-memory browser filesystem for the current page session. Reloading/closing the page clears that runtime state.

## Browser requirements

Use a recent Chrome, Edge, Firefox, or Safari. Large XGBoost/blocked-validation jobs are CPU- and memory-intensive because they run locally. Start with **1 validation window per duration** and increase to 2–3 when you want stronger validation.

## Important scientific note

The app reports blocked-validation RMSE separately from uncertainty. The displayed validation interval fields are derived from empirical blocked-validation RMSE and are not labeled as formal statistical confidence intervals.


## v20260922b runtime fix
Pyodide 314 requires a **module-type Web Worker**. This package uses `new Worker(..., {type: "module"})` and imports `pyodide.mjs`; it no longer uses the unsupported classic-worker `importScripts()` path. Runtime mirrors are retried automatically.
