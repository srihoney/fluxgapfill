# FluxGapFill 1.0 — Release Notes

Build `20260923v100`

## Production integration

- Integrated Universal Import, QC, gap filling/validation, energy/water, carbon and footprint workspaces into one release.
- Removed phase-development language from the primary interface.
- Reorganized navigation as a ten-step analysis workspace.

## Professional interface

- Rebuilt the visual system with restrained cards, typography and spacing.
- Added persistent workflow coaching.
- Added contextual helper strips on every major workspace page.
- Added a full workflow/help modal.
- Added Light, Dark and Night appearance options with local preference persistence.

## Journal Figure Studio

- Added journal-oriented export for all 13 rendered analysis charts.
- Added single-column, double-column and square print presets.
- Added PNG export at 600-DPI metadata and high pixel dimensions.
- Added SVG vector export.
- Added print-only white figure background independent of UI theme.
- Added regular-weight sans-serif typography, restrained line weights and automatic legend placement.
- Added per-chart "Journal figure" shortcuts.

## Reliability

- Bundled Plotly 6.5.2 locally instead of using the mutable `plotly-latest` CDN endpoint.
- Kept module-worker Pyodide fallback mirrors.
- Versioned final JS/CSS assets for cache-safe Cloudflare deployment.
- Added long-lived caching only for versioned static assets; Python engine files remain revalidated.
