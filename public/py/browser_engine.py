from __future__ import annotations
import json, math, os, traceback
from pathlib import Path
from typing import Dict, List
import numpy as np
import pandas as pd

from data_processor import read_eddypro_txt, InputProcessingConfig, infer_timestep
from external_sources import retrieve_external_reference, ExternalSourceConfig, SiteConfig
from gap_filler import gap_run_information, mds_gap_fill
from advanced_ml_gap_filler import (
    fit_candidate_model, predict_candidate, calendar_validation_windows, score_prediction
)

STATE: Dict[str, object] = {"data": None, "metadata": None, "benchmark": None, "filled": None}


def _emit(kind: str, **payload):
    try:
        import js
        obj = {"type": kind, **payload}
        js.postMessage(json.dumps(obj))
    except Exception:
        pass


def _json_clean(obj):
    if isinstance(obj, dict):
        return {str(k): _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, (pd.Timestamp, np.datetime64)):
        return str(obj)
    return obj

def _j(obj):
    return json.dumps(_json_clean(obj), default=str, allow_nan=False)


def _safe_float(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _gap_summary(data: pd.DataFrame, target: str) -> dict:
    if target not in data:
        return {"available": False}
    y = pd.to_numeric(data[target], errors='coerce')
    step = infer_timestep(pd.to_datetime(data['datetime']))
    gaps = gap_run_information(y.isna(), step)
    miss = y.isna()
    ids = gaps.loc[miss & gaps['gap_id'].gt(0), 'gap_id'].unique()
    lengths = []
    for gid in ids:
        rows = gaps['gap_id'].eq(gid)
        if rows.any(): lengths.append(float(gaps.loc[rows, 'gap_length_days'].iloc[0]))
    return {
        "available": True,
        "observed": int(y.notna().sum()),
        "missing": int(y.isna().sum()),
        "missing_pct": float(100*y.isna().mean()),
        "gap_count": int(len(ids)),
        "longest_gap_days": float(max(lengths) if lengths else 0.0),
        "median_gap_days": float(np.median(lengths) if lengths else 0.0),
    }


def _downsample(data: pd.DataFrame, columns: List[str], limit: int = 2200):
    n = len(data)
    if n <= limit:
        idx = np.arange(n)
    else:
        idx = np.unique(np.linspace(0, n-1, limit).round().astype(int))
    out = {"datetime": [pd.Timestamp(v).isoformat() for v in pd.to_datetime(data['datetime']).iloc[idx]]}
    for col in columns:
        if col in data:
            vals = pd.to_numeric(data[col], errors='coerce').iloc[idx]
            out[col] = [_safe_float(v) for v in vals]
    return out


def load_project(ec_path: str, cimis_path: str | None = None, maximum_qc: float = 1.0):
    _emit('progress', value=5, message='Reading EddyPro file')
    config = InputProcessingConfig(maximum_accepted_qc=float(maximum_qc), timestamp_convention='end')
    data, flux_vars, biomet_vars, metadata = read_eddypro_txt(ec_path, processing_config=config)
    data = data.reset_index(drop=True)

    cimis_info = None
    if cimis_path:
        _emit('progress', value=35, message='Aligning CIMIS reference data')
        ext, ranking, inventory = retrieve_external_reference(
            data,
            SiteConfig(latitude=0.0, longitude=0.0),
            ExternalSourceConfig(cimis_local_path=cimis_path),
        )
        for col in ext.columns:
            if col != 'datetime':
                data[col] = ext[col].to_numpy()
        cimis_info = {
            "station": ext.attrs.get('selected_station'),
            "inventory": inventory.to_dict(orient='records'),
            "ranking": ranking.head(10).to_dict(orient='records'),
        }

    _emit('progress', value=65, message='Computing data-quality diagnostics')
    dt = pd.to_datetime(data['datetime'])
    step = infer_timestep(dt)
    summary = {
        "records": int(len(data)),
        "start": str(dt.min()),
        "end": str(dt.max()),
        "timestep_minutes": float(step / pd.Timedelta(minutes=1)),
        "flux_variables": list(flux_vars),
        "biomet_variables": list(biomet_vars),
        "cimis": cimis_info,
        "LE": _gap_summary(data, 'LE'),
        "H": _gap_summary(data, 'H'),
        "plot": _downsample(data, ['LE','H','rn','g','sr','vpd','at','swc']),
    }
    # Predictor availability for transparent model selection.
    predictor_cols = [
        'sr','vpd','at','rh','ws','pa','rn','g','swc','soil_temperature_representative','wind_dir',
        'eto_ref','rain_ref','sr_ref','rn_ref','vp_ref','at_ref','rh_ref','dew_ref','ws_ref','wind_dir_ref','soil_temp_ref'
    ]
    avail = []
    for col in predictor_cols:
        if col in data:
            s = pd.to_numeric(data[col], errors='coerce')
            avail.append({"variable": col, "available_pct": float(100*s.notna().mean()), "n": int(s.notna().sum())})
    summary['predictors'] = avail
    STATE['data'] = data
    STATE['metadata'] = summary
    STATE['benchmark'] = None
    STATE['filled'] = None
    _emit('progress', value=100, message='Dataset ready')
    return _j(summary)


def _candidate_name(algorithm, profile):
    return 'MDS_Reichstein05' if algorithm == 'MDS' else f'{algorithm}_{profile}'


def run_benchmark(targets_json='["LE","H"]', durations_json='[1,7,14,30,60]', windows=1,
                  candidates_json='["MDS_Reichstein05","RF_cross","XGB_cross"]'):
    data = STATE.get('data')
    if data is None: raise RuntimeError('Load a dataset first.')
    targets = json.loads(targets_json) if isinstance(targets_json, str) else list(targets_json)
    durations = [float(x) for x in (json.loads(durations_json) if isinstance(durations_json, str) else durations_json)]
    candidates = json.loads(candidates_json) if isinstance(candidates_json, str) else list(candidates_json)
    windows = max(1, int(windows))
    rows = []
    total = max(1, len(targets)*len(durations)*windows*len(candidates))
    done = 0

    for target in targets:
        if target not in data: continue
        y0 = pd.to_numeric(data[target], errors='coerce')
        for duration in durations:
            masks = calendar_validation_windows(data, target, duration, n_windows=windows, minimum_observed_fraction=0.30)
            if not masks:
                for cand in candidates:
                    rows.append({"target":target,"gap_days":duration,"window":0,"model":cand,"available":False,"n":0,"rmse":None,"mae":None,"bias":None,"r2":None})
                continue
            for wi, mask in enumerate(masks, start=1):
                score_mask = mask & y0.notna()
                if score_mask.sum() < 20: continue
                work = data.copy()
                work.loc[mask, target] = np.nan
                truth = y0.loc[score_mask].to_numpy(dtype=float)
                for cand in candidates:
                    _emit('progress', value=int(100*done/total), message=f'Validating {target} · {duration:g} d · {cand}')
                    done += 1
                    try:
                        if cand == 'MDS_Reichstein05':
                            res = mds_gap_fill(work, target, max_gap_days=max(60.0, duration))
                            pred = pd.to_numeric(res.loc[score_mask.to_numpy(), f'{target}_filled'], errors='coerce').to_numpy(dtype=float)
                        else:
                            alg, profile = cand.split('_', 1)
                            bundle = fit_candidate_model(work, target, algorithm=alg, profile=profile, train_mask=work[target].notna(), random_state=42+wi)
                            all_pred = predict_candidate(bundle, work)
                            pred = all_pred[score_mask.to_numpy()]
                        m = score_prediction(truth, pred)
                        rows.append({"target":target,"gap_days":duration,"window":wi,"model":cand,"available":True,**m})
                    except Exception as exc:
                        rows.append({"target":target,"gap_days":duration,"window":wi,"model":cand,"available":False,"n":0,"rmse":None,"mae":None,"bias":None,"r2":None,"error":str(exc)})

    bench = pd.DataFrame(rows)
    if bench.empty:
        result = {"rows":[],"summary":[],"best":[]}
        STATE['benchmark'] = result
        return _j(result)
    ok = bench[bench['available'].eq(True) & pd.to_numeric(bench['rmse'], errors='coerce').notna()].copy()
    summary = []
    best = []
    if not ok.empty:
        grouped = ok.groupby(['target','gap_days','model'], as_index=False).agg(
            windows=('window','count'), n=('n','sum'), rmse=('rmse','mean'), mae=('mae','mean'), bias=('bias','mean'), r2=('r2','mean')
        )
        summary = grouped.to_dict(orient='records')
        for (target, duration), grp in grouped.groupby(['target','gap_days']):
            winner = grp.loc[grp['rmse'].idxmin()]
            best.append({"target":target,"gap_days":float(duration),"model":winner['model'],"rmse":float(winner['rmse']),"mae":float(winner['mae']),"bias":float(winner['bias']),"r2":float(winner['r2'])})
    result = {"rows":bench.where(pd.notna(bench), None).to_dict(orient='records'), "summary":summary, "best":best}
    STATE['benchmark'] = result
    _emit('progress', value=100, message='Blocked validation complete')
    return _j(result)


def _nearest_best(target: str, gap_days: float):
    b = STATE.get('benchmark') or {}
    choices = [x for x in b.get('best', []) if x.get('target') == target]
    if not choices: return None
    return min(choices, key=lambda x: abs(float(x['gap_days']) - float(gap_days)))


def run_gap_fill(targets_json='["LE","H"]', mode='adaptive', fixed_model='XGB_cross', max_gap_days=90.0):
    data = STATE.get('data')
    if data is None: raise RuntimeError('Load a dataset first.')
    targets = json.loads(targets_json) if isinstance(targets_json, str) else list(targets_json)
    out = data.copy()
    model_cache = {}
    total_targets = max(1, len(targets))
    source_counts = {}

    for ti, target in enumerate(targets):
        if target not in data: continue
        _emit('progress', value=int(5+80*ti/total_targets), message=f'Gap filling {target}')
        y = pd.to_numeric(data[target], errors='coerce')
        dt = pd.to_datetime(data['datetime'])
        step = infer_timestep(dt)
        gap = gap_run_information(y.isna(), step)
        eligible = y.isna() & gap['gap_length_days'].le(float(max_gap_days)+1e-12)
        filled = y.to_numpy(dtype=float).copy()
        source = np.where(y.notna(), 'measured', 'unfilled').astype(object)
        validation_rmse = np.full(len(data), np.nan)

        # MDS once; ML models are trained once each as needed.
        mds = mds_gap_fill(data, target, max_gap_days=float(max_gap_days))
        mds_pred = pd.to_numeric(mds[f'{target}_filled'], errors='coerce').to_numpy(dtype=float)

        if mode == 'adaptive' and STATE.get('benchmark'):
            gap_ids = gap.loc[eligible, 'gap_id'].unique()
            needed = set()
            winners = {}
            for gid in gap_ids:
                rows = gap['gap_id'].eq(gid)
                gd = float(gap.loc[rows, 'gap_length_days'].iloc[0])
                best = _nearest_best(target, gd)
                model = best['model'] if best else fixed_model
                winners[int(gid)] = (model, best)
                if model != 'MDS_Reichstein05': needed.add(model)
            for model in sorted(needed):
                alg, profile = model.split('_', 1)
                bundle = fit_candidate_model(data, target, algorithm=alg, profile=profile, train_mask=y.notna(), random_state=42)
                model_cache[(target,model)] = predict_candidate(bundle, data)
            for gid, (model,best) in winners.items():
                rows = eligible & gap['gap_id'].eq(gid)
                pred = mds_pred if model == 'MDS_Reichstein05' else model_cache[(target,model)]
                good = rows.to_numpy() & np.isfinite(pred)
                filled[good] = pred[good]
                source[good] = model
                if best and best.get('rmse') is not None: validation_rmse[good] = float(best['rmse'])
        else:
            model = fixed_model
            if model == 'MDS_Reichstein05':
                pred = mds_pred
            else:
                alg, profile = model.split('_',1)
                bundle = fit_candidate_model(data, target, algorithm=alg, profile=profile, train_mask=y.notna(), random_state=42)
                pred = predict_candidate(bundle, data)
            good = eligible.to_numpy() & np.isfinite(pred)
            filled[good] = pred[good]
            source[good] = model
            # use benchmark RMSE closest to each gap when available
            if STATE.get('benchmark'):
                for gid in gap.loc[eligible, 'gap_id'].unique():
                    rows = eligible & gap['gap_id'].eq(gid)
                    gd = float(gap.loc[rows, 'gap_length_days'].iloc[0])
                    matches = [x for x in STATE['benchmark'].get('summary',[]) if x['target']==target and x['model']==model]
                    if matches:
                        best = min(matches, key=lambda x: abs(float(x['gap_days'])-gd))
                        validation_rmse[rows.to_numpy()] = float(best['rmse'])

        out[f'{target}_original'] = y
        out[f'{target}_filled'] = filled
        out[f'{target}_source'] = source
        out[f'{target}_gap_id'] = gap['gap_id'].to_numpy()
        out[f'{target}_gap_length_days'] = gap['gap_length_days'].to_numpy()
        out[f'{target}_validation_rmse'] = validation_rmse
        out[f'{target}_validation_lower95'] = filled - 1.96*validation_rmse
        out[f'{target}_validation_upper95'] = filled + 1.96*validation_rmse
        source_counts[target] = pd.Series(source).value_counts().to_dict()

    # Derived ET from filled LE. Temperature-dependent latent heat when air temperature is available.
    if 'LE_filled' in out:
        temp = None
        for col in ('at_filled','at','at_ref'):
            if col in out:
                temp = pd.to_numeric(out[col], errors='coerce'); break
        if temp is None: temp = pd.Series(20.0, index=out.index)
        temp = temp.fillna(20.0)
        lambda_j_kg = (2.501 - 0.002361*temp) * 1e6
        step_seconds = float(infer_timestep(pd.to_datetime(out['datetime'])) / pd.Timedelta(seconds=1))
        out['ET_from_LE_mm_interval'] = pd.to_numeric(out['LE_filled'], errors='coerce') * step_seconds / lambda_j_kg
        day = pd.to_datetime(out['datetime']).dt.floor('D')
        daily = out.assign(_day=day).groupby('_day', as_index=False)['ET_from_LE_mm_interval'].sum(min_count=1)
        daily = daily.rename(columns={'_day':'date','ET_from_LE_mm_interval':'ET_mm_day'})
    else:
        daily = pd.DataFrame(columns=['date','ET_mm_day'])

    STATE['filled'] = out
    plotcols = ['LE_original','LE_filled','H_original','H_filled']
    result = {
        "source_counts": source_counts,
        "plot": _downsample(out, plotcols, limit=2600),
        "daily_et": [{"date":str(r['date'].date()),"ET_mm_day":_safe_float(r['ET_mm_day'])} for _,r in daily.iterrows()],
        "filled_records": {t:int((pd.Series(out.get(f'{t}_source',[]))!='measured').sum()) if f'{t}_source' in out else 0 for t in targets},
    }
    _emit('progress', value=100, message='Gap filling complete')
    return _j(result)


def export_csv(compact=True):
    out = STATE.get('filled') if STATE.get('filled') is not None else STATE.get('data')
    if out is None: raise RuntimeError('No dataset is loaded.')
    if compact:
        preferred = ['datetime','LE_original','LE_filled','LE_source','LE_gap_id','LE_gap_length_days','LE_validation_rmse','LE_validation_lower95','LE_validation_upper95',
                     'H_original','H_filled','H_source','H_gap_id','H_gap_length_days','H_validation_rmse','H_validation_lower95','H_validation_upper95','ET_from_LE_mm_interval']
        cols = [c for c in preferred if c in out]
        frame = out[cols].copy()
    else:
        frame = out.copy()
    return frame.to_csv(index=False)


def benchmark_csv():
    b = STATE.get('benchmark')
    if not b: return ''
    return pd.DataFrame(b.get('rows',[])).to_csv(index=False)
