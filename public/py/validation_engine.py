"""Phase-2 blocked validation and empirical uncertainty calibration.

The engine masks contiguous calendar windows, trains only on target observations
outside each masked window, and scores only target observations that existed
before masking. This supports imperfect real-world EC records without reverting
to optimistic random-point cross-validation.
"""
from __future__ import annotations
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple
import math
import numpy as np
import pandas as pd

from gap_filler import mds_gap_fill
from advanced_ml_gap_filler import fit_candidate_model, predict_candidate


def _season(month: int) -> str:
    if month in (12, 1, 2): return "DJF"
    if month in (3, 4, 5): return "MAM"
    if month in (6, 7, 8): return "JJA"
    return "SON"


def _day_mask(data: pd.DataFrame) -> Tuple[pd.Series, str]:
    for col in ("sr_filled", "sr", "sr_ref"):
        if col in data:
            x = pd.to_numeric(data[col], errors="coerce")
            if x.notna().any():
                return x >= 20.0, f"{col} >= 20 W m-2"
    dt = pd.to_datetime(data["datetime"], errors="coerce")
    hour = dt.dt.hour + dt.dt.minute / 60.0
    return hour.between(6.0, 18.0, inclusive="left"), "clock fallback 06:00-18:00"


def _metrics(truth: np.ndarray, pred: np.ndarray) -> Dict[str, float]:
    ok = np.isfinite(truth) & np.isfinite(pred)
    if int(ok.sum()) < 2:
        return {"n": int(ok.sum()), "coverage": float(ok.mean()) if len(ok) else 0.0,
                "rmse": np.nan, "mae": np.nan, "bias": np.nan, "r2": np.nan}
    y = truth[ok]; p = pred[ok]
    err = p - y
    ss_res = float(np.sum((y - p) ** 2)); ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return {"n": int(len(y)), "coverage": float(ok.mean()),
            "rmse": float(np.sqrt(np.mean(err ** 2))), "mae": float(np.mean(np.abs(err))),
            "bias": float(np.mean(err)), "r2": float(r2)}


def _candidate_windows(data: pd.DataFrame, target: str, duration_days: float,
                       n_windows: int, minimum_observed_fraction: float = 0.35) -> List[pd.Series]:
    dt = pd.to_datetime(data["datetime"], errors="coerce")
    y = pd.to_numeric(data[target], errors="coerce")
    if dt.isna().any() or len(dt) < 20:
        return []
    start, end = dt.min(), dt.max()
    span = end - start
    if span < pd.Timedelta(days=duration_days):
        return []

    # Dense deterministic set of possible centers, then greedily choose windows
    # spread across the record with no overlap. A calendar window can contain
    # pre-existing target gaps; only the originally observed records are scored.
    centers = [start + span * q for q in np.linspace(0.04, 0.96, 93)]
    candidates = []
    for center in centers:
        left = center - pd.Timedelta(days=duration_days / 2.0)
        right = center + pd.Timedelta(days=duration_days / 2.0)
        if left < start or right > end + pd.Timedelta(seconds=1):
            continue
        mask = dt.between(left, right, inclusive="left")
        total = int(mask.sum()); observed = int((mask & y.notna()).sum())
        if total < 2 or observed < 20:
            continue
        frac = observed / total
        if frac < minimum_observed_fraction:
            continue
        candidates.append((center, frac, observed, mask))
    if not candidates:
        return []

    # Prefer good coverage but preserve temporal spread by targeting equally
    # spaced quantiles of the record and choosing the nearest non-overlapping
    # candidate to each target center.
    target_centers = [start + span * q for q in np.linspace(0.10, 0.90, max(1, n_windows))]
    chosen: List[Tuple[pd.Timestamp, pd.Series]] = []
    for tc in target_centers:
        ranked = sorted(candidates, key=lambda z: (abs((z[0] - tc).total_seconds()), -z[1], -z[2]))
        for center, frac, observed, mask in ranked:
            left = center - pd.Timedelta(days=duration_days / 2.0)
            right = center + pd.Timedelta(days=duration_days / 2.0)
            overlaps = False
            for cc, _ in chosen:
                cleft = cc - pd.Timedelta(days=duration_days / 2.0)
                cright = cc + pd.Timedelta(days=duration_days / 2.0)
                if max(left, cleft) < min(right, cright):
                    overlaps = True; break
            if not overlaps:
                chosen.append((center, mask)); break
    return [m for _, m in chosen]


def _parse_candidate(name: str):
    if name == "MDS_Reichstein05": return ("MDS", "Reichstein05")
    if name == "MDS_Vekuri23": return ("MDS", "Vekuri23")
    if "_" not in name: raise ValueError(f"Unknown candidate {name}")
    return tuple(name.split("_", 1))


def run_blocked_validation(data: pd.DataFrame, targets: Sequence[str], durations: Sequence[float],
                           candidates: Sequence[str], windows_per_duration: int = 2,
                           minimum_observed_fraction: float = 0.35,
                           progress=None, random_state: int = 42) -> dict:
    rows: List[dict] = []
    segment_rows: List[dict] = []
    residual_pool: Dict[Tuple[str, float, str], List[float]] = {}
    daymask, day_basis = _day_mask(data)
    dt = pd.to_datetime(data["datetime"], errors="coerce")
    season = dt.dt.month.map(_season)

    total = max(1, len(targets) * len(durations) * len(candidates) * max(1, windows_per_duration))
    done = 0
    for target in targets:
        if target not in data: continue
        original = pd.to_numeric(data[target], errors="coerce")
        for duration in durations:
            masks = _candidate_windows(data, target, float(duration), int(windows_per_duration), minimum_observed_fraction)
            if not masks:
                for cand in candidates:
                    rows.append({"target": target, "gap_days": float(duration), "window": 0,
                                 "model": cand, "available": False, "n": 0, "coverage": 0.0,
                                 "rmse": None, "mae": None, "bias": None, "r2": None,
                                 "error": "No eligible calendar validation window"})
                continue
            for wi, mask in enumerate(masks, start=1):
                score_mask = mask & original.notna()
                if int(score_mask.sum()) < 20: continue
                work = data.copy()
                work.loc[mask, target] = np.nan
                truth_all = original.loc[score_mask].to_numpy(dtype=float)
                score_idx = np.flatnonzero(score_mask.to_numpy())
                for cand in candidates:
                    if progress:
                        progress(100.0 * done / total, f"Validating {target} · {duration:g} d · {cand} · window {wi}/{len(masks)}")
                    done += 1
                    try:
                        alg, profile = _parse_candidate(cand)
                        if alg == "MDS":
                            result = mds_gap_fill(work, target, max_gap_days=max(60.0, float(duration)), method=profile)
                            prediction_all = pd.to_numeric(result[f"{target}_filled"], errors="coerce").to_numpy(dtype=float)
                        else:
                            bundle = fit_candidate_model(work, target, algorithm=alg, profile=profile,
                                                         train_mask=work[target].notna(), random_state=random_state + wi)
                            prediction_all = predict_candidate(bundle, work)
                        pred = prediction_all[score_idx]
                        met = _metrics(truth_all, pred)
                        ok = np.isfinite(truth_all) & np.isfinite(pred)
                        residuals = (truth_all[ok] - pred[ok]).tolist()
                        residual_pool.setdefault((target, float(duration), cand), []).extend(residuals)
                        center_time = dt.loc[mask].min() + (dt.loc[mask].max() - dt.loc[mask].min()) / 2
                        rows.append({"target": target, "gap_days": float(duration), "window": wi,
                                     "window_start": str(dt.loc[mask].min()), "window_end": str(dt.loc[mask].max()),
                                     "window_season": _season(center_time.month), "model": cand, "available": True, **met})

                        # Segment diagnostics from exactly the same held-out records.
                        score_day = daymask.loc[score_mask].to_numpy(dtype=bool)
                        score_season = season.loc[score_mask].to_numpy(dtype=object)
                        for label, selector in (("day", score_day), ("night", ~score_day)):
                            mm = _metrics(truth_all[selector], pred[selector]) if selector.any() else _metrics(np.array([]), np.array([]))
                            segment_rows.append({"target": target, "gap_days": float(duration), "window": wi,
                                                 "model": cand, "segment_type": "daynight", "segment": label, **mm})
                        for seas in ("DJF", "MAM", "JJA", "SON"):
                            selector = score_season == seas
                            if selector.any():
                                mm = _metrics(truth_all[selector], pred[selector])
                                segment_rows.append({"target": target, "gap_days": float(duration), "window": wi,
                                                     "model": cand, "segment_type": "season", "segment": seas, **mm})
                    except Exception as exc:
                        rows.append({"target": target, "gap_days": float(duration), "window": wi,
                                     "model": cand, "available": False, "n": 0, "coverage": 0.0,
                                     "rmse": None, "mae": None, "bias": None, "r2": None, "error": str(exc)})

    detail = pd.DataFrame(rows)
    ok = detail[detail.get("available", False).eq(True)].copy() if not detail.empty else pd.DataFrame()
    summary_rows = []
    best = []
    calibration = []
    if not ok.empty:
        for (target, gap_days, model), grp in ok.groupby(["target", "gap_days", "model"], dropna=False):
            weights = pd.to_numeric(grp["n"], errors="coerce").fillna(0).to_numpy(dtype=float)
            def wavg(col):
                vals = pd.to_numeric(grp[col], errors="coerce").to_numpy(dtype=float)
                good = np.isfinite(vals) & (weights > 0)
                return float(np.average(vals[good], weights=weights[good])) if good.any() else np.nan
            key = (str(target), float(gap_days), str(model))
            res = np.asarray(residual_pool.get(key, []), dtype=float)
            res = res[np.isfinite(res)]
            q025 = float(np.quantile(res, 0.025)) if len(res) >= 20 else np.nan
            q975 = float(np.quantile(res, 0.975)) if len(res) >= 20 else np.nan
            mederr = float(np.median(res)) if len(res) else np.nan
            row = {"target": target, "gap_days": float(gap_days), "model": model,
                   "windows": int(grp["window"].nunique()), "n": int(pd.to_numeric(grp["n"], errors="coerce").fillna(0).sum()),
                   "coverage": wavg("coverage"), "rmse": wavg("rmse"), "mae": wavg("mae"),
                   "bias": wavg("bias"), "r2": wavg("r2"),
                   "residual_q025": q025, "residual_q975": q975, "residual_median": mederr}
            summary_rows.append(row)
            calibration.append({"target": target, "gap_days": float(gap_days), "model": model,
                                "n_residual": int(len(res)), "residual_q025": q025,
                                "residual_q975": q975, "residual_median": mederr})
        summary = pd.DataFrame(summary_rows)
        for (target, gap_days), grp in summary.groupby(["target", "gap_days"]):
            valid = grp[pd.to_numeric(grp["rmse"], errors="coerce").notna()]
            if len(valid):
                winner = valid.loc[pd.to_numeric(valid["rmse"]).idxmin()]
                best.append({k: (float(winner[k]) if k in {"gap_days", "rmse", "mae", "bias", "r2", "residual_q025", "residual_q975"} and pd.notna(winner[k]) else winner[k])
                             for k in ["target", "gap_days", "model", "rmse", "mae", "bias", "r2", "residual_q025", "residual_q975"]})
    else:
        summary = pd.DataFrame()

    seg = pd.DataFrame(segment_rows)
    seg_summary = []
    if not seg.empty:
        for keys, grp in seg.groupby(["target", "gap_days", "model", "segment_type", "segment"]):
            weights = pd.to_numeric(grp["n"], errors="coerce").fillna(0).to_numpy(dtype=float)
            row = dict(zip(["target", "gap_days", "model", "segment_type", "segment"], keys))
            row["windows"] = int(grp["window"].nunique()); row["n"] = int(weights.sum())
            for col in ("coverage", "rmse", "mae", "bias", "r2"):
                vals = pd.to_numeric(grp[col], errors="coerce").to_numpy(dtype=float)
                good = np.isfinite(vals) & (weights > 0)
                row[col] = float(np.average(vals[good], weights=weights[good])) if good.any() else np.nan
            seg_summary.append(row)

    if progress: progress(100.0, "Blocked validation complete")
    return {
        "rows": detail.where(pd.notna(detail), None).to_dict(orient="records") if not detail.empty else [],
        "summary": summary.where(pd.notna(summary), None).to_dict(orient="records") if not summary.empty else [],
        "best": best,
        "segments": seg_summary,
        "calibration": calibration,
        "daynight_basis": day_basis,
        "design": {"calendar_blocked": True, "minimum_observed_fraction": float(minimum_observed_fraction),
                   "windows_per_duration": int(windows_per_duration), "selection_metric": "RMSE"},
    }


def gap_class(days: float) -> str:
    d = float(days)
    if d <= 1: return "A · ≤1 day"
    if d <= 3: return "B · >1–3 days"
    if d <= 7: return "C · >3–7 days"
    if d <= 14: return "D · >7–14 days"
    if d <= 30: return "E · >14–30 days"
    if d <= 60: return "F · >30–60 days"
    if d <= 90: return "G · >60–90 days"
    return "H · >90 days"
