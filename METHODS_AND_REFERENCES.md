# FluxGapFill 1.0 — Methods and Reference Notes

This file summarizes the method families implemented in the production package. It is not a substitute for reading the cited literature when publishing scientific results.

## Gap filling

### MDS / Reichstein-style gap filling

The canonical MDS search hierarchy follows the established marginal distribution sampling approach used in eddy-covariance post-processing. Search windows, driver tolerances and method provenance are retained in the filled output.

Reference:
- Reichstein, M. et al. (2005). On the separation of net ecosystem exchange into assimilation and ecosystem respiration: review and improved algorithm. *Global Change Biology*.

### Vekuri-style radiation-balanced MDS

An alternative MDS donor aggregation is available to reduce bias that can arise from skewed radiation donor distributions. It should be evaluated against the conventional MDS method on the user's own data.

Reference:
- Vekuri et al. (2023), published methodological work on MDS gap-filling bias and donor balancing.

### Random Forest

RF candidates use environmental/time predictors and are evaluated using contiguous blocked calendar gaps rather than relying only on OOB diagnostics.

Reference:
- Breiman, L. (2001). Random Forests. *Machine Learning*.

### XGBoost

XGBoost candidates use the same leakage-aware reconstruction framework and are benchmarked against MDS/RF rather than assumed to be universally superior.

Reference:
- Chen, T. & Guestrin, C. (2016). XGBoost: A Scalable Tree Boosting System.

## Validation

Blocked validation hides contiguous calendar windows and scores predictions only where target observations existed before masking. This evaluates retrospective reconstruction of gaps in a complete record; it is not a forecasting experiment.

Metrics include RMSE, MAE, bias, R2 and coverage, with day/night and seasonal diagnostics.

## Energy balance

Available energy is represented as `Rn - G` and turbulent energy as `H + LE`. Primary closure diagnostics use measured/QC-accepted fluxes.

The package includes separate correction sensitivity products based on published energy-closure approaches, including Mauder-, Charuchittipan- and De Roo-type formulations. These are sensitivity analyses; they should not be interpreted as direct proof of the physical cause of non-closure.

## Evapotranspiration

LE is converted to water depth using temperature-dependent latent heat of vaporization. Daily totals apply an explicit completeness threshold and do not silently label incomplete days as complete.

## u* threshold

FluxGapFill provides independent MPT-style and CPD-style threshold diagnostics with seasonal/bootstrap summaries. Thresholds are site/data/QC sensitive and should be inspected rather than accepted automatically.

## Carbon partitioning

The current carbon workflow uses a nighttime temperature-response approach based on Lloyd-Taylor ecosystem respiration modeling, then derives GPP from NEE and modeled respiration using the selected sign convention.

## Footprint

The footprint module implements a Kljun et al. (2015)-style flux-footprint parameterization using the required turbulence/site inputs and optional AOI polygons.

Reference:
- Kljun, N., Calanca, P., Rotach, M. W., & Schmid, H. P. (2015). A simple two-dimensional parameterisation for Flux Footprint Prediction (FFP). *Geoscientific Model Development*.

## Software provenance

FluxGapFill is an independent implementation. It does not contain proprietary Tovi source code. Comparable workflow concepts are implemented from published methods and publicly documented scientific descriptions.
