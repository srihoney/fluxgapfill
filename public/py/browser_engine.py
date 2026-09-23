from __future__ import annotations
import json, math, os, traceback
from pathlib import Path
from typing import Dict, List
import numpy as np
import pandas as pd

from data_processor import read_eddypro_txt, InputProcessingConfig, infer_timestep
from external_sources import retrieve_external_reference, ExternalSourceConfig, SiteConfig
from gap_filler import gap_run_information, mds_gap_fill
from advanced_ml_gap_filler import fit_candidate_model, predict_candidate
from phase2_qc import apply_phase2_qc
from validation_engine import run_blocked_validation, gap_class
from universal_import import inspect_file, read_mapped_file, align_supplemental, capabilities, project_schema
from phase3_energy_water import run_phase3_analysis, report_markdown
from phase4_carbon_footprint import run_phase4_analysis, phase4_report

STATE: Dict[str, object] = {"data": None, "data_imported": None, "metadata": None, "benchmark": None, "filled": None, "project": None, "inspection": {}, "qc_audit": None, "qc_summary": None, "phase3": None, "phase4": None, "phase4_geojson": None}


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


def inspect_input(path: str, kind: str = "primary"):
    """Inspect a user-selected file without committing it to the project."""
    result = inspect_file(path, kind=kind)
    STATE.setdefault('inspection', {})[kind] = result
    return _j(result)


def import_schema():
    return _j(project_schema())


def _summary_from_data(data: pd.DataFrame, *, source_info: dict, external_info: dict | None, project_meta: dict, flux_vars=None, biomet_vars=None) -> dict:
    dt = pd.to_datetime(data['datetime'])
    step = infer_timestep(dt)
    summary = {
        "records": int(len(data)),
        "start": str(dt.min()), "end": str(dt.max()),
        "timestep_minutes": float(step / pd.Timedelta(minutes=1)),
        "flux_variables": list(flux_vars or [x for x in ('LE','H','NEE') if x in data and pd.to_numeric(data[x],errors='coerce').notna().any()]),
        "biomet_variables": list(biomet_vars or [x for x in ('sr','rn','g','at','rh','vpd','ws','wind_dir','rain','pa','swc','soil_temperature_representative','ustar') if x in data]),
        "source": source_info,
        "external": external_info,
        "project": project_meta,
        "cimis": external_info if external_info and external_info.get('type') == 'CIMIS' else None,
        "LE": _gap_summary(data, 'LE'),
        "H": _gap_summary(data, 'H'),
        "NEE": _gap_summary(data, 'NEE'),
        "plot": _downsample(data, ['LE','H','NEE','rn','g','sr','vpd','at','swc']),
    }
    predictor_cols = [
        'sr','vpd','at','rh','ws','pa','rn','g','swc','soil_temperature_representative','wind_dir','ustar','obukhov_length','sigma_v',
        'ETo','ET','rain','irrigation','pbl_height','wstar','vp','dew_point',
        'eto_ref','rain_ref','irrigation_ref','sr_ref','rn_ref','vp_ref','vpd_ref','pa_ref','at_ref','rh_ref','dew_ref','ws_ref','wind_dir_ref','soil_temp_ref'
    ]
    avail = []
    for col in predictor_cols:
        if col in data:
            sval = pd.to_numeric(data[col], errors='coerce')
            if sval.notna().any():
                avail.append({"variable": col, "available_pct": float(100*sval.notna().mean()), "n": int(sval.notna().sum())})
    summary['predictors'] = avail
    summary['capabilities'] = capabilities(data, project_meta)
    return summary


def load_universal_project(primary_path: str, primary_spec_json: str,
                           supplemental_path: str | None = None, supplemental_spec_json: str | None = None,
                           project_json: str | None = None):
    """Load a mapped project from EddyPro, Campbell, FLUXNET, generic, Excel or CIMIS-compatible sources."""
    primary_spec = json.loads(primary_spec_json) if isinstance(primary_spec_json, str) else dict(primary_spec_json or {})
    supplemental_spec = json.loads(supplemental_spec_json) if isinstance(supplemental_spec_json, str) and supplemental_spec_json else (dict(supplemental_spec_json or {}) if supplemental_spec_json else None)
    project = json.loads(project_json) if isinstance(project_json, str) and project_json else dict(project_json or {})
    primary_fmt = str(primary_spec.get('format') or 'generic')
    mapping = primary_spec.get('mapping') or {}
    maximum_qc = float(project.get('maximum_qc', 1.0))
    timezone = str(project.get('timezone') or 'America/Los_Angeles')
    time_basis = str(project.get('time_basis') or 'local_standard')
    convention = str(project.get('timestamp_convention') or primary_spec.get('timestamp_recommendation') or 'midpoint')
    config = {"maximum_qc":maximum_qc,"timezone":timezone,"time_basis":time_basis,"timestamp_convention":convention}

    if primary_fmt == 'eddypro':
        _emit('progress', value=5, message='Reading EddyPro full-output file')
        pcfg = InputProcessingConfig(maximum_accepted_qc=maximum_qc, timezone=timezone, time_basis=time_basis, timestamp_convention='end')
        data, flux_vars, biomet_vars, pmeta = read_eddypro_txt(primary_path, processing_config=pcfg)
        data = data.reset_index(drop=True)
        source_info = {"type":"EddyPro","format":"eddypro","name":Path(primary_path).name,"specialized_parser":True,
                       "timestamp_convention":"end","timezone":timezone,"time_basis":time_basis,
                       "rows_regularized":int(len(data))}
    else:
        _emit('progress', value=5, message=f"Reading {primary_spec.get('format_label', primary_fmt)}")
        data, pmeta = read_mapped_file(primary_path, mapping, config, fmt=primary_fmt)
        flux_vars=[x for x in ('LE','H','NEE') if x in data]
        biomet_vars=[x for x in ('sr','rn','g','at','rh','vpd','ws','rain','irrigation','pa','swc','soil_temperature_representative','pbl_height','wstar') if x in data]
        source_info={"type":primary_spec.get('format_label',primary_fmt),"format":primary_fmt,"name":Path(primary_path).name,
                     "specialized_parser":False,"timestamp_convention":convention,"timezone":timezone,"time_basis":time_basis,
                     **{k:v for k,v in pmeta.items() if k not in {'audit'}}}
        source_info['audit']=pmeta.get('audit',[])

    external_info = None
    if supplemental_path and supplemental_spec:
        sfmt=str(supplemental_spec.get('format') or 'generic')
        _emit('progress', value=38, message=f"Aligning supplemental {supplemental_spec.get('format_label', sfmt)} data")
        if sfmt == 'cimis':
            ext, ranking, inventory = retrieve_external_reference(
                data, SiteConfig(latitude=float(project.get('latitude') or 0.0), longitude=float(project.get('longitude') or 0.0)),
                ExternalSourceConfig(cimis_local_path=supplemental_path),
            )
            for col in ext.columns:
                if col != 'datetime': data[col] = ext[col].to_numpy()
            external_info={"type":"CIMIS","format":"cimis","name":Path(supplemental_path).name,"station":ext.attrs.get('selected_station'),
                           "inventory":inventory.to_dict(orient='records'),"ranking":ranking.head(10).to_dict(orient='records')}
        else:
            smap=supplemental_spec.get('mapping') or {}
            sconvention=str(project.get('supplemental_timestamp_convention') or supplemental_spec.get('timestamp_recommendation') or 'midpoint')
            sconfig={"maximum_qc":maximum_qc,"timezone":timezone,"time_basis":time_basis,"timestamp_convention":sconvention}
            sup,smeta=read_mapped_file(supplemental_path,smap,sconfig,fmt=sfmt)
            aligned=align_supplemental(sup,data)
            rename={
                'ETo':'eto_ref','rain':'rain_ref','irrigation':'irrigation_ref','pbl_height':'pbl_height','wstar':'wstar','sr':'sr_ref','rn':'rn_ref','vp':'vp_ref','vpd':'vpd_ref','pa':'pa_ref','at':'at_ref','rh':'rh_ref',
                'dew_point':'dew_ref','ws':'ws_ref','wind_dir':'wind_dir_ref','soil_temperature_representative':'soil_temp_ref'
            }
            added=[]
            for col in aligned.columns:
                if col=='datetime': continue
                target=rename.get(col, f'{col}_ref' if col in {'swc','g'} else None)
                if target:
                    data[target]=aligned[col].to_numpy(); added.append(target)
            external_info={"type":supplemental_spec.get('format_label',sfmt),"format":sfmt,"name":Path(supplemental_path).name,
                           "station":smeta.get('reader_metadata',{}).get('station'),"added_variables":added,"timestep_minutes":smeta.get('timestep_minutes'),
                           "timestamp_convention":sconvention}

    _emit('progress', value=72, message='Computing project capabilities and data-quality diagnostics')
    # Mirror primary ETo/rain variables into names used by water-analysis readiness when present.
    summary=_summary_from_data(data,source_info=source_info,external_info=external_info,project_meta=project,flux_vars=flux_vars,biomet_vars=biomet_vars)
    STATE['data_imported']=data.copy(); STATE['data']=data.copy(); STATE['metadata']=summary; STATE['benchmark']=None; STATE['filled']=None; STATE['project']=project; STATE['qc_audit']=None; STATE['qc_summary']=None; STATE['phase3']=None
    _emit('progress', value=100, message='Project dataset ready')
    return _j(summary)


def load_project(ec_path: str, cimis_path: str | None = None, maximum_qc: float = 1.0):
    """Backward-compatible EddyPro+CIMIS entry point."""
    p=inspect_file(ec_path,'primary')
    s=inspect_file(cimis_path,'supplemental') if cimis_path else None
    project={"maximum_qc":float(maximum_qc),"timezone":"America/Los_Angeles","time_basis":"local_standard","timestamp_convention":"end"}
    return load_universal_project(ec_path, _j(p), cimis_path, _j(s) if s else None, _j(project))


def update_project_metadata(project_json: str | None = None):
    project = json.loads(project_json) if isinstance(project_json, str) and project_json else dict(project_json or {})
    current = dict(STATE.get('project') or {})
    current.update(project)
    STATE['project'] = current
    data = STATE.get('data')
    if data is None:
        return _j([])
    caps = capabilities(data, current)
    if isinstance(STATE.get('metadata'), dict):
        STATE['metadata']['project'] = current
        STATE['metadata']['capabilities'] = caps
    return _j(caps)


def run_qc(config_json: str | None = None):
    """Apply the configurable Phase-2 QC layer to the standardized import."""
    base = STATE.get('data_imported')
    if base is None:
        raise RuntimeError('Load a dataset first.')
    config = json.loads(config_json) if isinstance(config_json, str) and config_json else dict(config_json or {})
    _emit('progress', value=10, message='Applying quality-control range and robust spike screening')
    screened, audit, qc_summary = apply_phase2_qc(base, config)
    STATE['data'] = screened
    STATE['qc_audit'] = audit
    STATE['qc_summary'] = qc_summary
    STATE['benchmark'] = None
    STATE['filled'] = None
    STATE['phase3'] = None
    old = STATE.get('metadata') or {}
    summary = _summary_from_data(
        screened,
        source_info=old.get('source') or {},
        external_info=old.get('external'),
        project_meta=STATE.get('project') or {},
        flux_vars=old.get('flux_variables'),
        biomet_vars=old.get('biomet_variables'),
    )
    summary['qc'] = {
        'summary': qc_summary,
        'audit': audit.where(pd.notna(audit), None).to_dict(orient='records') if not audit.empty else [],
        'plot': _downsample(screened, ['LE','H','NEE','LE_pre_phase2_qc','H_pre_phase2_qc','NEE_pre_phase2_qc']),
    }
    STATE['metadata'] = summary
    _emit('progress', value=100, message='Phase-2 QC complete')
    return _j(summary)


def run_benchmark(targets_json='["LE","H"]', durations_json='[1,3,7,14,30,60]', windows=2,
                  candidates_json='["MDS_Reichstein05","MDS_Vekuri23","RF_cross","XGB_cross"]'):
    data = STATE.get('data')
    if data is None:
        raise RuntimeError('Load a dataset first.')
    targets = json.loads(targets_json) if isinstance(targets_json, str) else list(targets_json)
    durations = [float(x) for x in (json.loads(durations_json) if isinstance(durations_json, str) else durations_json)]
    candidates = json.loads(candidates_json) if isinstance(candidates_json, str) else list(candidates_json)
    targets = [t for t in targets if t in data and pd.to_numeric(data[t], errors='coerce').notna().sum() >= 20]
    if not targets:
        raise ValueError('No selected target has enough accepted observations for blocked validation.')
    result = run_blocked_validation(
        data, targets, durations, candidates, windows_per_duration=max(1, int(windows)),
        minimum_observed_fraction=0.35,
        progress=lambda pct,msg: _emit('progress', value=max(1, min(99, int(pct))), message=msg),
        random_state=42,
    )
    STATE['benchmark'] = result
    _emit('progress', value=100, message='Blocked validation complete')
    return _j(result)


def _nearest_summary(target: str, gap_days: float, model: str | None = None):
    b = STATE.get('benchmark') or {}
    rows = [x for x in b.get('summary', []) if x.get('target') == target and (model is None or x.get('model') == model)]
    if not rows:
        return None
    return min(rows, key=lambda x: abs(float(x.get('gap_days', 0)) - float(gap_days)))


def _nearest_best(target: str, gap_days: float):
    b = STATE.get('benchmark') or {}
    choices = [x for x in b.get('best', []) if x.get('target') == target]
    if not choices:
        return None
    return min(choices, key=lambda x: abs(float(x['gap_days']) - float(gap_days)))




def _candidate_eligible_for_gap(data: pd.DataFrame, target: str, rows: pd.Series, candidate: str) -> bool:
    """Require the driver regime implied by a candidate to exist in the real gap."""
    if candidate.startswith('MDS_'):
        return True
    if candidate.endswith('_cross'):
        other = 'H' if target.upper() == 'LE' else 'LE' if target.upper() == 'H' else None
        if not other or other not in data:
            return False
        return float(pd.to_numeric(data.loc[rows, other], errors='coerce').notna().mean()) >= 0.50
    if candidate.endswith('_external'):
        cols = [c for c in ('sr_ref','rn_ref','at_ref','rh_ref','vpd_ref','vp_ref','ws_ref','eto_ref','soil_temp_ref') if c in data]
        usable = sum(float(pd.to_numeric(data.loc[rows, c], errors='coerce').notna().mean()) >= 0.50 for c in cols)
        return usable >= 2
    if candidate.endswith('_tower'):
        cols = [c for c in ('sr','rn','g','at','rh','vpd','ws','swc','soil_temperature_representative','ustar') if c in data]
        usable = sum(float(pd.to_numeric(data.loc[rows, c], errors='coerce').notna().mean()) >= 0.35 for c in cols)
        return usable >= 2
    return True


def _best_eligible(target: str, gap_days: float, rows: pd.Series, data: pd.DataFrame):
    b = STATE.get('benchmark') or {}
    summary = [x for x in b.get('summary', []) if x.get('target') == target and x.get('rmse') is not None]
    if not summary:
        return None
    distances = [abs(float(x.get('gap_days',0)) - float(gap_days)) for x in summary]
    mind = min(distances)
    nearby = [x for x in summary if abs(float(x.get('gap_days',0)) - float(gap_days)) == mind]
    nearby = sorted(nearby, key=lambda x: float(x.get('rmse', float('inf'))))
    for cand in nearby:
        if _candidate_eligible_for_gap(data, target, rows, str(cand.get('model'))):
            return cand
    # If no method at the nearest duration matches the actual driver regime,
    # allow MDS as a conservative fallback if it was validated at any duration.
    mds = sorted([x for x in summary if str(x.get('model','')).startswith('MDS_')], key=lambda x: abs(float(x.get('gap_days',0))-float(gap_days)))
    return mds[0] if mds else None

def _candidate_prediction(data: pd.DataFrame, target: str, candidate: str, max_gap_days: float, cache: dict):
    key=(target,candidate)
    if key in cache:
        return cache[key]
    if candidate in {'MDS_Reichstein05','MDS_Vekuri23'}:
        variant = candidate.split('_',1)[1]
        res = mds_gap_fill(data, target, max_gap_days=max_gap_days, method=variant)
        pred = pd.to_numeric(res[f'{target}_filled'], errors='coerce').to_numpy(dtype=float)
        extra = {
            'method': res.get(f'{target}_fill_method'),
            'fill_qc': res.get(f'{target}_fill_qc'),
            'donor_sd': res.get(f'{target}_fill_donor_sd'),
            'fill_n': res.get(f'{target}_fill_n'),
            'fill_window_days': res.get(f'{target}_fill_window_days'),
        }
    else:
        alg, profile = candidate.split('_',1)
        y = pd.to_numeric(data[target], errors='coerce')
        bundle = fit_candidate_model(data, target, algorithm=alg, profile=profile, train_mask=y.notna(), random_state=42)
        pred = predict_candidate(bundle, data)
        extra = {'diagnostics': bundle.diagnostics}
    cache[key]=(pred,extra)
    return cache[key]


def run_gap_fill(targets_json='["LE","H"]', mode='adaptive', fixed_model='XGB_cross', max_gap_days=60.0):
    data = STATE.get('data')
    if data is None:
        raise RuntimeError('Load a dataset first.')
    targets = json.loads(targets_json) if isinstance(targets_json, str) else list(targets_json)
    out = data.copy()
    cache = {}
    source_counts = {}
    model_diagnostics = []

    for ti, target in enumerate(targets):
        if target not in data:
            continue
        _emit('progress', value=int(5 + 80 * ti / max(1, len(targets))), message=f'Gap filling {target}')
        y = pd.to_numeric(data[target], errors='coerce')
        step = infer_timestep(pd.to_datetime(data['datetime']))
        gap = gap_run_information(y.isna(), step)
        eligible = y.isna() & gap['gap_length_days'].le(float(max_gap_days) + 1e-12)
        filled = y.to_numpy(dtype=float).copy()
        source = np.where(y.notna(), 'measured', np.where(eligible, 'unfilled', 'unfilled_gap_over_limit')).astype(object)
        validation_rmse = np.full(len(data), np.nan)
        lower = np.full(len(data), np.nan); upper = np.full(len(data), np.nan)
        mds_fill_qc = np.where(y.notna(), 0.0, np.nan).astype(float)
        donor_sd = np.full(len(data), np.nan); donor_n = np.zeros(len(data), dtype=int); fill_window = np.full(len(data), np.nan)
        method_detail = np.where(y.notna(), 'measured', 'unfilled').astype(object)
        gap_class_arr = np.array([gap_class(d) if d > 0 else 'measured' for d in gap['gap_length_days']], dtype=object)

        for gid in gap.loc[eligible, 'gap_id'].unique():
            rows = eligible & gap['gap_id'].eq(gid)
            gd = float(gap.loc[rows, 'gap_length_days'].iloc[0])
            best = _best_eligible(target, gd, rows, data) if mode == 'adaptive' else None
            candidate = str(best['model']) if best else str(fixed_model)
            try:
                pred, extra = _candidate_prediction(data, target, candidate, float(max_gap_days), cache)
            except Exception:
                candidate = 'MDS_Reichstein05'
                pred, extra = _candidate_prediction(data, target, candidate, float(max_gap_days), cache)
            good = rows.to_numpy() & np.isfinite(pred)
            filled[good] = pred[good]
            source[good] = candidate
            method_detail[good] = candidate
            cal = _nearest_summary(target, gd, candidate)
            if cal:
                if cal.get('rmse') is not None:
                    validation_rmse[good] = float(cal['rmse'])
                qlo = cal.get('residual_q025'); qhi = cal.get('residual_q975')
                if qlo is not None and qhi is not None:
                    lower[good] = filled[good] + float(qlo)
                    upper[good] = filled[good] + float(qhi)
            if candidate.startswith('MDS_'):
                for nm, dest in [('fill_qc',mds_fill_qc),('donor_sd',donor_sd),('fill_n',donor_n),('fill_window_days',fill_window)]:
                    val = extra.get(nm)
                    if val is not None:
                        arr = pd.to_numeric(val, errors='coerce').to_numpy()
                        dest[good] = arr[good]
                md = extra.get('method')
                if md is not None:
                    marr = pd.Series(md).astype(str).to_numpy()
                    method_detail[good] = marr[good]
            else:
                diag = extra.get('diagnostics')
                if diag and not any(x.get('target')==target and x.get('candidate')==candidate for x in model_diagnostics):
                    model_diagnostics.append({'target':target,'candidate':candidate,**diag})

        out[f'{target}_original'] = y
        out[f'{target}_filled'] = filled
        out[f'{target}_source'] = source
        out[f'{target}_method_detail'] = method_detail
        out[f'{target}_gap_id'] = gap['gap_id'].to_numpy()
        out[f'{target}_gap_length_days'] = gap['gap_length_days'].to_numpy()
        out[f'{target}_gap_class'] = gap_class_arr
        out[f'{target}_mds_fill_qc'] = mds_fill_qc
        out[f'{target}_mds_donor_sd'] = donor_sd
        out[f'{target}_mds_donor_n'] = donor_n
        out[f'{target}_mds_window_days'] = fill_window
        out[f'{target}_validation_rmse'] = validation_rmse
        # These are empirical blocked-validation residual intervals, not donor-SD intervals.
        out[f'{target}_empirical_lower95'] = lower
        out[f'{target}_empirical_upper95'] = upper
        source_counts[target] = pd.Series(source).value_counts().to_dict()

    daily = pd.DataFrame(columns=['date','ET_mm_day'])
    if 'LE_filled' in out:
        temp = None
        for col in ('at_filled','at','at_ref'):
            if col in out:
                temp = pd.to_numeric(out[col], errors='coerce'); break
        if temp is None:
            temp = pd.Series(20.0, index=out.index)
        temp = temp.fillna(20.0)
        lambda_j_kg = (2.501 - 0.002361 * temp) * 1e6
        step_seconds = float(infer_timestep(pd.to_datetime(out['datetime'])) / pd.Timedelta(seconds=1))
        out['ET_from_LE_mm_interval'] = pd.to_numeric(out['LE_filled'], errors='coerce') * step_seconds / lambda_j_kg
        day = pd.to_datetime(out['datetime']).dt.floor('D')
        daily = out.assign(_day=day).groupby('_day', as_index=False)['ET_from_LE_mm_interval'].sum(min_count=1)
        daily = daily.rename(columns={'_day':'date','ET_from_LE_mm_interval':'ET_mm_day'})

    STATE['filled'] = out
    STATE['phase3'] = None
    STATE['phase4'] = None
    gap_class_counts = {}
    for t in targets:
        if f'{t}_gap_class' in out and f'{t}_source' in out:
            m = out[f'{t}_source'].astype(str).ne('measured') & ~out[f'{t}_source'].astype(str).str.startswith('unfilled')
            gap_class_counts[t] = out.loc[m, f'{t}_gap_class'].value_counts().to_dict()
    result = {
        'source_counts': source_counts,
        'gap_class_counts': gap_class_counts,
        'model_diagnostics': model_diagnostics,
        'plot': _downsample(out, [c for c in ['LE_original','LE_filled','LE_empirical_lower95','LE_empirical_upper95','H_original','H_filled','H_empirical_lower95','H_empirical_upper95'] if c in out]),
        'daily_et': [{'date':str(r['date'].date()), 'ET_mm_day':_safe_float(r['ET_mm_day'])} for _,r in daily.iterrows()],
        'filled_records': {t:int(((pd.Series(out.get(f'{t}_source',[]))!='measured') & (pd.Series(out.get(f'{t}_source',[])).str.startswith('unfilled')==False)).sum()) if f'{t}_source' in out else 0 for t in targets},
        'interval_note': '95% empirical residual intervals are calibrated from blocked validation for the selected model/gap duration when >=20 held-out residuals are available.'
    }
    _emit('progress', value=100, message='Gap filling complete')
    return _j(result)



def add_water_input_file(path: str, spec_json: str, timestamp_convention: str = 'end'):
    """Add an optional irrigation/water-input table after the main project is loaded."""
    base = STATE.get('data_imported')
    if base is None:
        raise RuntimeError('Load the primary project before adding a water-input file.')
    spec = json.loads(spec_json) if isinstance(spec_json, str) else dict(spec_json or {})
    project = STATE.get('project') or {}
    mapping = spec.get('mapping') or {}
    date_only = bool((mapping.get('date') or {}).get('column')) and not bool((mapping.get('time') or {}).get('column')) and not bool((mapping.get('timestamp') or {}).get('column'))
    cfg = {
        'maximum_qc': float(project.get('maximum_qc', 1.0)),
        'timezone': str(project.get('timezone') or 'America/Los_Angeles'),
        'time_basis': str(project.get('time_basis') or 'local_standard'),
        # A date-only daily total belongs to that calendar day; midpoint avoids
        # shifting it across day boundaries regardless of the UI default.
        'timestamp_convention': 'midpoint' if date_only else str(timestamp_convention or spec.get('timestamp_recommendation') or 'end'),
        'allow_long_timestep': True,
    }
    src, meta = read_mapped_file(path, mapping, cfg, fmt=str(spec.get('format') or 'generic'))
    aligned = align_supplemental(src, base)
    # Daily water-total files are common. For source intervals >=20 h, allocate
    # each daily total only across target intervals belonging to that same date
    # rather than using nearest-neighbor alignment across day boundaries.
    try:
        src_step=infer_timestep(pd.to_datetime(src['datetime']))
    except Exception:
        st=pd.DatetimeIndex(pd.to_datetime(src['datetime'])).dropna().sort_values().unique()
        diffs=pd.Series(st[1:]-st[:-1]); diffs=diffs[diffs>pd.Timedelta(0)]
        src_step=(diffs.mode().iloc[0] if not diffs.empty and not diffs.mode().empty else (diffs.median() if not diffs.empty else pd.Timedelta(0)))
    if src_step >= pd.Timedelta(hours=20):
        target_day=pd.to_datetime(base['datetime']).dt.floor('D')
        src_day=pd.to_datetime(src['datetime']).dt.floor('D')
        for total_col in ('irrigation','rain','ETo'):
            if total_col not in src: continue
            daily_src=pd.DataFrame({'day':src_day,'v':pd.to_numeric(src[total_col],errors='coerce')}).groupby('day')['v'].sum(min_count=1)
            arr=np.full(len(base),np.nan)
            for day,val in daily_src.items():
                if not np.isfinite(val): continue
                mask=target_day.eq(day); n=int(mask.sum())
                if n: arr[mask.to_numpy()]=float(val)/n
            aligned[total_col]=arr
    added=[]
    rename={'irrigation':'irrigation_ref','rain':'rain_water_ref','ETo':'eto_water_ref','swc':'swc_water_ref'}
    for col,target in rename.items():
        if col in aligned and pd.to_numeric(aligned[col],errors='coerce').notna().any():
            for state_key in ('data_imported','data','filled'):
                frame=STATE.get(state_key)
                if frame is not None:
                    frame[target]=pd.to_numeric(aligned[col],errors='coerce').to_numpy()
            added.append(target)
    # Phase-3 conventions: irrigation_ref always points to optional applied-water input;
    # only use water-file rain/ETo as general references when not already supplied.
    for state_key in ('data_imported','data','filled'):
        frame=STATE.get(state_key)
        if frame is None: continue
        if 'rain_water_ref' in frame and ('rain_ref' not in frame or pd.to_numeric(frame['rain_ref'],errors='coerce').notna().sum()==0):
            frame['rain_ref']=frame['rain_water_ref']
        if 'eto_water_ref' in frame and ('eto_ref' not in frame or pd.to_numeric(frame['eto_ref'],errors='coerce').notna().sum()==0):
            frame['eto_ref']=frame['eto_water_ref']
        if 'swc_water_ref' in frame and ('swc_ref' not in frame or pd.to_numeric(frame['swc_ref'],errors='coerce').notna().sum()==0):
            frame['swc_ref']=frame['swc_water_ref']
    STATE['phase3']=None
    STATE['phase4']=None
    old=STATE.get('metadata') or {}
    current=STATE.get('data')
    summary=_summary_from_data(current,source_info=old.get('source') or {},external_info=old.get('external'),project_meta=STATE.get('project') or {},flux_vars=old.get('flux_variables'),biomet_vars=old.get('biomet_variables'))
    summary['water_input']={'name':Path(path).name,'format':spec.get('format'),'added_variables':added,'timestep_minutes':meta.get('timestep_minutes')}
    STATE['metadata']=summary
    return _j({'added_variables':added,'metadata':summary,'source':summary['water_input']})


def run_phase3(config_json: str | None = None):
    source = STATE.get('filled') if STATE.get('filled') is not None else STATE.get('data')
    if source is None:
        raise RuntimeError('Load a project first.')
    config=json.loads(config_json) if isinstance(config_json,str) and config_json else dict(config_json or {})
    _emit('progress',value=6,message='Preparing energy and water variables')
    interval,result=run_phase3_analysis(source,STATE.get('project') or {},config)
    STATE['phase3']={'interval':interval,'result':result,'config':config}
    # Compact JSON payload for browser rendering.
    e=result.get('summary',{}).get('energy',{}); w=result.get('summary',{}).get('water',{})
    daily=result.get('water_daily',pd.DataFrame()); monthly=result.get('energy_monthly',pd.DataFrame())
    corr=result.get('summary',{}).get('corrections',[])
    payload={
      'summary':result.get('summary',{}),
      'energy_monthly':monthly.where(pd.notna(monthly),None).to_dict(orient='records') if isinstance(monthly,pd.DataFrame) else [],
      'water_daily':daily.where(pd.notna(daily),None).to_dict(orient='records') if isinstance(daily,pd.DataFrame) else [],
      'water_monthly':result.get('water_monthly',pd.DataFrame()).where(pd.notna(result.get('water_monthly',pd.DataFrame())),None).to_dict(orient='records') if isinstance(result.get('water_monthly'),pd.DataFrame) else [],
      'corrections':corr,
      'plot_energy':result.get('plot_energy',[]),
    }
    _emit('progress',value=100,message='Energy and water analysis complete')
    return _j(payload)


def phase3_csv(kind='interval'):
    p3=STATE.get('phase3')
    if not p3: return ''
    r=p3['result']
    mapping={
      'interval':r.get('interval'), 'energy_daily':r.get('energy_daily'), 'energy_monthly':r.get('energy_monthly'), 'energy_annual':r.get('energy_annual'),
      'correction_summary':r.get('correction_summary'), 'correction_daily':r.get('correction_daily'),
      'water_daily':r.get('water_daily'), 'water_monthly':r.get('water_monthly'), 'water_annual':r.get('water_annual'),
    }
    frame=mapping.get(str(kind))
    return '' if frame is None else frame.to_csv(index=False)


def phase3_report_md():
    p3=STATE.get('phase3')
    if not p3: return '# FluxGapFill energy and water report\n\nEnergy and water analysis has not been run.\n'
    return report_markdown(p3['result'],STATE.get('project') or {})



def load_phase4_geojson(path: str):
    if not path:
        STATE['phase4_geojson']=None
        return _j({'status':'cleared'})
    text=Path(path).read_text(encoding='utf-8',errors='replace')
    obj=json.loads(text)
    if not isinstance(obj,dict) or obj.get('type') not in ('FeatureCollection','Feature'):
        raise ValueError('AOI file must be GeoJSON FeatureCollection or Feature.')
    if obj.get('type')=='Feature': obj={'type':'FeatureCollection','features':[obj]}
    STATE['phase4_geojson']=json.dumps(obj)
    return _j({'status':'ready','features':len(obj.get('features',[])),'name':Path(path).name})

def run_phase4(config_json: str | None = None):
    source=STATE.get('filled') if STATE.get('filled') is not None else STATE.get('data')
    if source is None: raise RuntimeError('Load a project first.')
    config=json.loads(config_json) if isinstance(config_json,str) and config_json else dict(config_json or {})
    _emit('progress',value=5,message='Running u*, carbon and footprint analysis')
    result=run_phase4_analysis(source,STATE.get('project') or {},config,STATE.get('phase4_geojson'))
    STATE['phase4']={'result':result,'config':config}
    c=result.get('carbon',{})
    payload={'ustar':result.get('ustar',{}),'footprint':result.get('footprint',{})}
    if c.get('status')=='ready':
        daily=c.get('daily',pd.DataFrame())
        payload['carbon']={'status':'ready','summary':c.get('summary',{}),'daily':daily.where(pd.notna(daily),None).to_dict(orient='records')}
    else: payload['carbon']=c
    _emit('progress',value=100,message='Carbon and footprint analysis complete')
    return _j(payload)

def phase4_csv(kind='carbon_daily'):
    p4=STATE.get('phase4')
    if not p4:return ''
    r=p4['result']; c=r.get('carbon',{}); f=r.get('footprint',{}); u=r.get('ustar',{})
    if kind=='carbon_daily' and isinstance(c.get('daily'),pd.DataFrame): return c['daily'].to_csv(index=False)
    if kind=='carbon_interval' and isinstance(c.get('interval'),pd.DataFrame): return c['interval'].to_csv(index=False)
    if kind=='ustar_seasonal':
        return pd.DataFrame([{'season':k,**v} for k,v in (u.get('seasonal') or {}).items()]).to_csv(index=False)
    if kind=='ustar_bootstrap_summary': return pd.DataFrame([{'method':'MPT',**(u.get('MPT') or {})},{'method':'CPD',**(u.get('CPD') or {})}]).to_csv(index=False)
    if kind=='footprint_aoi': return pd.DataFrame(f.get('aoi_summary') or []).to_csv(index=False)
    if kind=='footprint_grid' and f.get('status')=='ready':
        x=np.asarray(f.get('x'),float); y=np.asarray(f.get('y'),float); z=np.asarray(f.get('z'),float); X,Y=np.meshgrid(x,y)
        return pd.DataFrame({'east_m':X.ravel(),'north_m':Y.ravel(),'footprint_density_m2':z.ravel()}).to_csv(index=False)
    return ''

def phase4_report_md():
    p4=STATE.get('phase4')
    if not p4:return '# FluxGapFill carbon and footprint report\n\nCarbon and footprint analysis has not been run.\n'
    return phase4_report(p4['result'],STATE.get('project') or {})

def export_csv(compact=True):
    out = STATE.get('filled') if STATE.get('filled') is not None else STATE.get('data')
    if out is None:
        raise RuntimeError('No dataset is loaded.')
    if compact:
        preferred = ['datetime']
        for target in ('LE','H','NEE'):
            preferred += [f'{target}_original',f'{target}_filled',f'{target}_source',f'{target}_method_detail',f'{target}_gap_id',f'{target}_gap_length_days',f'{target}_gap_class',f'{target}_mds_fill_qc',f'{target}_validation_rmse',f'{target}_empirical_lower95',f'{target}_empirical_upper95']
        preferred += ['ET_from_LE_mm_interval']
        cols = [c for c in preferred if c in out]
        frame = out[cols].copy()
    else:
        frame = out.copy()
    return frame.to_csv(index=False)


def benchmark_csv(kind='detail'):
    b = STATE.get('benchmark')
    if not b:
        return ''
    key = {'detail':'rows','summary':'summary','segments':'segments','calibration':'calibration'}.get(str(kind),'rows')
    return pd.DataFrame(b.get(key, [])).to_csv(index=False)


def qc_audit_csv():
    q = STATE.get('qc_audit')
    return '' if q is None else q.to_csv(index=False)


def validation_report_md():
    b = STATE.get('benchmark') or {}
    meta = STATE.get('metadata') or {}
    if not b:
        return '# FluxGapFill validation report\n\nNo blocked validation has been run.\n'
    lines = ['# FluxGapFill validation report','',
             f"Project: **{(STATE.get('project') or {}).get('project_name') or (STATE.get('project') or {}).get('site_name') or 'Untitled'}**", 
             f"Period: {meta.get('start','—')} to {meta.get('end','—')}",
             f"Records: {meta.get('records','—')}",
             '', '## Validation design',
             'Contiguous calendar windows were hidden from the target flux. Models were trained only on target observations outside each hidden window, while scoring used only observations that existed before masking.',
             f"Day/night definition: {b.get('daynight_basis','—')}",
             f"Windows per duration: {(b.get('design') or {}).get('windows_per_duration','—')}",
             '', '## Model comparison',
             '| Target | Gap (d) | Model | N | RMSE | MAE | Bias | R² |',
             '|---|---:|---|---:|---:|---:|---:|---:|']
    for r in b.get('summary',[]):
        lines.append(f"| {r.get('target')} | {float(r.get('gap_days',0)):g} | {r.get('model')} | {int(r.get('n') or 0)} | {r.get('rmse') if r.get('rmse') is not None else '—'} | {r.get('mae') if r.get('mae') is not None else '—'} | {r.get('bias') if r.get('bias') is not None else '—'} | {r.get('r2') if r.get('r2') is not None else '—'} |")
    lines += ['', '## Adaptive selection rules',
              '| Target | Gap (d) | Selected model | RMSE |', '|---|---:|---|---:|']
    for r in b.get('best',[]):
        lines.append(f"| {r.get('target')} | {float(r.get('gap_days',0)):g} | {r.get('model')} | {r.get('rmse')} |")
    lines += ['', '## Uncertainty note',
              'Where at least 20 blocked-validation residuals are available for a target/model/gap-duration combination, FluxGapFill stores empirical 2.5th and 97.5th percentiles of truth-minus-prediction residuals. These are used as empirical reconstruction intervals in the final product. MDS donor standard deviation is retained separately and is not labelled as a 95% prediction interval.',
              '', '## Reproducibility',
              'Build: 20260923p4. The detailed, segment and calibration CSV exports contain the exact validation diagnostics used by the adaptive selector.', '']
    return '\n'.join(lines)
