"""FluxGapFill Phase-3 energy balance and water/ET analysis.

The module starts from already-computed fluxes/environmental time series. It does
not process raw high-frequency eddy-covariance signals.

Design principles
-----------------
* Never overwrite measured or Phase-2 reconstructed H/LE.
* Measured-only closure diagnostics are kept separate from reconstructed series.
* Energy-balance corrections are explicit sensitivity products.
* Soil-heat-storage correction is optional and requires user-supplied physical
  parameters rather than hidden database assumptions.
* ET uncertainty from Phase-2 empirical residual bands represents gap-
  reconstruction uncertainty only, not total measurement uncertainty.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Dict, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from sklearn.linear_model import HuberRegressor
except Exception:  # pragma: no cover - browser package normally provides it
    HuberRegressor = None

from data_processor import infer_timestep

SIGMA = 5.670374419e-8
G_GRAV = 9.80665
RD = 287.05
CP_AIR = 1004.67
RHO_WATER = 1000.0
CP_WATER = 4186.0


def _num(s, index=None):
    if s is None:
        return pd.Series(np.nan, index=index)
    return pd.to_numeric(s, errors="coerce")


def _safe(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _series(data: pd.DataFrame, names, prefer_filled=False):
    names = list(names)
    if prefer_filled:
        expanded=[]
        for n in names:
            expanded.extend([f"{n}_filled", n])
        names=expanded
    for c in names:
        if c in data and pd.to_numeric(data[c], errors="coerce").notna().any():
            return pd.to_numeric(data[c], errors="coerce"), c
    return pd.Series(np.nan, index=data.index), None


def latent_heat_j_kg(temp_c: pd.Series) -> pd.Series:
    """Temperature-dependent latent heat of vaporization, J kg-1."""
    t = pd.to_numeric(temp_c, errors="coerce")
    return (2.501 - 0.002361 * t) * 1e6


def _daylight_mask(data: pd.DataFrame, rn: pd.Series, threshold=20.0):
    for c in ("sr", "sr_ref"):
        if c in data and pd.to_numeric(data[c], errors="coerce").notna().any():
            return pd.to_numeric(data[c], errors="coerce") > float(threshold), c
    return rn > float(threshold), "rn_fallback"


def soil_heat_storage_correction(data: pd.DataFrame, config: Mapping[str, object]) -> Tuple[pd.Series, pd.Series, dict]:
    """Return surface G, storage term, and metadata.

    G_surface = G_plate + Cv * depth * dT/dt, with positive G downward.
    Cv = rho_b * cp_dry + theta_v * rho_w * cp_w.

    This is only applied when explicitly requested and required inputs exist.
    """
    g, g_col = _series(data, ("g", "g_ref"))
    if g_col is None:
        return g, pd.Series(np.nan,index=data.index), {"status":"unavailable","reason":"Soil heat flux (G) is missing."}

    sign = str(config.get("g_positive_direction") or "down").lower()
    if sign == "up":
        g = -g
    mode = str(config.get("g_mode") or "already_surface")
    if mode != "plate_storage":
        return g, pd.Series(0.0,index=data.index), {
            "status":"ready","mode":"already_surface","source":g_col,
            "note":"G used as supplied; no soil heat-storage correction was applied.",
            "positive_direction":"down"
        }

    depth_cm = config.get("plate_depth_cm")
    bulk_density = config.get("bulk_density_kg_m3")
    dry_cp = float(config.get("dry_soil_cp_j_kg_k") or 840.0)
    try:
        depth_m = float(depth_cm) / 100.0
        rho_b = float(bulk_density)
    except Exception:
        return pd.Series(np.nan,index=data.index), pd.Series(np.nan,index=data.index), {"status":"needs_data","reason":"Plate depth and bulk density are required for soil heat-storage correction."}
    if not (depth_m > 0 and rho_b > 0):
        return pd.Series(np.nan,index=data.index), pd.Series(np.nan,index=data.index), {"status":"needs_data","reason":"Plate depth and bulk density must be positive."}

    ts, ts_col = _series(data, ("soil_temperature_representative","soil_temperature","soil_temp_ref"))
    swc, swc_col = _series(data, ("swc","swc_ref"))
    if ts_col is None or swc_col is None:
        missing=[]
        if ts_col is None: missing.append("soil temperature")
        if swc_col is None: missing.append("soil water content")
        return pd.Series(np.nan,index=data.index), pd.Series(np.nan,index=data.index), {"status":"needs_data","reason":"Missing " + " and ".join(missing) + "."}

    dt = pd.to_datetime(data["datetime"])
    step_s = float(infer_timestep(dt) / pd.Timedelta(seconds=1))
    # centered derivative minimizes temporal shift; edges fall back to one-sided pandas difference
    dtdt = ts.diff().div(step_s)
    if len(ts) >= 3:
        centered = (ts.shift(-1) - ts.shift(1)) / (2.0 * step_s)
        dtdt = centered.where(centered.notna(), dtdt)
    cv = rho_b * dry_cp + swc * RHO_WATER * CP_WATER
    temp_rate_c_h = dtdt.abs() * 3600.0
    max_rate = float(config.get("max_soil_temperature_change_c_h") or 8.0)
    storage_raw = cv * depth_m * dtdt
    max_storage = float(config.get("max_storage_abs_wm2") or 200.0)
    max_corrected = float(config.get("max_corrected_g_abs_wm2") or 250.0)
    valid = temp_rate_c_h.le(max_rate) & storage_raw.abs().le(max_storage)
    storage = storage_raw.where(valid)
    surface_g = (g + storage).where((g + storage).abs().le(max_corrected))
    return surface_g, storage, {
        "status":"ready","mode":"plate_storage","source":g_col,"temperature_source":ts_col,"swc_source":swc_col,
        "plate_depth_cm":float(depth_m*100.0),"bulk_density_kg_m3":rho_b,"dry_soil_cp_j_kg_k":dry_cp,
        "positive_direction":"down",
        "max_temperature_change_c_h":max_rate,"max_storage_abs_wm2":max_storage,"max_corrected_g_abs_wm2":max_corrected,
        "n_storage_accepted":int(storage.notna().sum()),"n_storage_rejected":int(storage_raw.notna().sum()-storage.notna().sum()),
        "equation":"G_surface = G_plate + (rho_b*cp_dry + theta_v*rho_w*cp_w)*depth*dT/dt"
    }


def _regression_metrics(ae: pd.Series, te: pd.Series, mask: pd.Series) -> dict:
    x = pd.to_numeric(ae.where(mask), errors="coerce")
    y = pd.to_numeric(te.where(mask), errors="coerce")
    ok = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
    x=x[ok].to_numpy(float); y=y[ok].to_numpy(float)
    if len(x)<3:
        return {"n":int(len(x)),"ratio_of_sums":None,"slope":None,"intercept":None,"r2":None,"rmse":None,"mae":None,"through_origin_slope":None,"huber_slope":None,"huber_intercept":None}
    sx=float(np.sum(x)); sy=float(np.sum(y)); ratio=sy/sx if abs(sx)>1e-12 else np.nan
    # OLS slope is undefined when available energy is effectively constant.
    # Handle that explicitly rather than allowing np.polyfit to emit a RankWarning.
    if float(np.nanstd(x)) < 1e-12:
        slope = intercept = r2 = np.nan
    else:
        slope, intercept = np.polyfit(x,y,1)
        pred=slope*x+intercept
        ss_res=np.sum((y-pred)**2); ss_tot=np.sum((y-y.mean())**2)
        r2=1-ss_res/ss_tot if ss_tot>0 else np.nan
    rmse=float(np.sqrt(np.mean((y-x)**2)))
    mae=float(np.mean(np.abs(y-x)))
    denom=float(np.dot(x,x)); through=float(np.dot(x,y)/denom) if denom>0 else np.nan
    hs=hi=np.nan
    if HuberRegressor is not None and len(x)>=8:
        try:
            h=HuberRegressor().fit(x.reshape(-1,1),y); hs=float(h.coef_[0]); hi=float(h.intercept_)
        except Exception: pass
    return {"n":int(len(x)),"ratio_of_sums":_safe(ratio),"slope":_safe(slope),"intercept":_safe(intercept),"r2":_safe(r2),"rmse":_safe(rmse),"mae":_safe(mae),"through_origin_slope":_safe(through),"huber_slope":_safe(hs),"huber_intercept":_safe(hi)}


def _energy_daily(interval: pd.DataFrame, hcol: str, lecol: str) -> pd.DataFrame:
    d=interval.copy(); d["date"]=pd.to_datetime(d["datetime"]).dt.floor("D")
    d["AE"]=pd.to_numeric(d["rn_phase3"],errors="coerce")-pd.to_numeric(d["g_phase3_surface"],errors="coerce")
    d["TE"]=pd.to_numeric(d[hcol],errors="coerce")+pd.to_numeric(d[lecol],errors="coerce")
    d["residual"]=d["AE"]-d["TE"]
    daylight=pd.to_numeric(d["phase3_daylight"],errors="coerce").fillna(0).astype(bool)
    rows=[]
    for day,g in d.groupby("date"):
        m=daylight.loc[g.index] & g["AE"].notna() & g["TE"].notna()
        ae=g.loc[m,"AE"]; te=g.loc[m,"TE"]
        rows.append({"date":day,"n":int(m.sum()),"available_energy_sum_Wm2_intervals":_safe(ae.sum(min_count=1)),"turbulent_energy_sum_Wm2_intervals":_safe(te.sum(min_count=1)),"EBR":_safe(te.sum()/ae.sum()) if len(ae) and abs(ae.sum())>1e-12 else None,"mean_residual_Wm2":_safe(g.loc[m,"residual"].mean())})
    return pd.DataFrame(rows)


def _energy_aggregate(interval: pd.DataFrame, freq: str, hcol: str, lecol: str) -> pd.DataFrame:
    d=interval.copy(); dt=pd.to_datetime(d["datetime"])
    if freq=="M": key=dt.dt.to_period("M").astype(str)
    elif freq=="Y": key=dt.dt.year.astype(str)
    else: key=dt.dt.floor("D").astype(str)
    d["period"]=key; d["AE"]=d["rn_phase3"]-d["g_phase3_surface"]; d["TE"]=d[hcol]+d[lecol]; d["residual"]=d["AE"]-d["TE"]
    rows=[]
    for period,g in d.groupby("period"):
        m=g["phase3_daylight"].astype(bool)&g["AE"].notna()&g["TE"].notna()
        ae=g.loc[m,"AE"]; te=g.loc[m,"TE"]
        rows.append({"period":period,"n":int(m.sum()),"EBR":_safe(te.sum()/ae.sum()) if len(ae) and abs(ae.sum())>1e-12 else None,"AE_mean_Wm2":_safe(ae.mean()),"TE_mean_Wm2":_safe(te.mean()),"residual_mean_Wm2":_safe((ae-te).mean())})
    return pd.DataFrame(rows)


def _mauder_daily(interval: pd.DataFrame, h: pd.Series, le: pd.Series) -> Tuple[pd.Series,pd.Series,pd.DataFrame]:
    out_h=h.copy(); out_le=le.copy(); dt=pd.to_datetime(interval["datetime"]); days=dt.dt.floor("D")
    ae=interval["rn_phase3"]-interval["g_phase3_surface"]; daymask=interval["phase3_daylight"].astype(bool)
    rows=[]
    for day,idx in days.groupby(days).groups.items():
        ix=pd.Index(idx); m=daymask.loc[ix]&ae.loc[ix].notna()&h.loc[ix].notna()&le.loc[ix].notna()
        n=int(m.sum()); ebr=np.nan; factor=np.nan
        if n>=2:
            sae=float(ae.loc[ix][m].sum()); ste=float((h.loc[ix][m]+le.loc[ix][m]).sum())
            if sae>0 and ste>0: ebr=ste/sae; factor=1.0/ebr if ebr>0 else np.nan
        if np.isfinite(factor):
            apply=daymask.loc[ix]&h.loc[ix].notna()&le.loc[ix].notna(); sel=ix[apply.to_numpy()]
            out_h.loc[sel]=h.loc[sel]*factor; out_le.loc[sel]=le.loc[sel]*factor
        rows.append({"date":day,"n":n,"EBR_before":_safe(ebr),"correction_factor":_safe(factor)})
    return out_h,out_le,pd.DataFrame(rows)


def _charuchittipan(interval: pd.DataFrame, h: pd.Series, le: pd.Series, temp_c: pd.Series) -> Tuple[pd.Series,pd.Series,dict]:
    ae=interval["rn_phase3"]-interval["g_phase3_surface"]; t=temp_c+273.15
    hc=h.copy(); lec=le.copy(); valid=ae.notna()&h.notna()&le.notna()&t.notna()
    # Iterate because the corrected Bowen ratio appears in the buoyancy partition.
    frac=pd.Series(np.nan,index=interval.index)
    for i in range(8):
        bo=hc/lec.replace(0,np.nan)
        lam=latent_heat_j_kg(temp_c)
        f=1.0/(1.0 + (0.61*t*CP_AIR)/(lam*bo))
        # Pathological/negative Bowen-ratio intervals do not provide a stable physical partition.
        f=f.where(np.isfinite(f)&f.between(0,1))
        res=ae-(h+le)
        new_h=h+f*res; new_le=le+(1-f)*res
        hc=hc.where(~(valid&f.notna()),new_h); lec=lec.where(~(valid&f.notna()),new_le); frac=f
    applied=valid&frac.notna()
    return hc,lec,{"n_applied":int(applied.sum()),"n_skipped_partition":int((valid&~frac.notna()).sum()),"note":"Residual partition follows the buoyancy-flux-ratio formulation; unstable/pathological partition fractions are left uncorrected."}


def _derive_wstar(interval: pd.DataFrame, h: pd.Series, le: pd.Series, temp_c: pd.Series, pbl: pd.Series) -> Tuple[pd.Series,dict]:
    supplied, col=_series(interval,("wstar","w_star"))
    if col is not None:
        return supplied.where(supplied>0),{"source":col,"derived":False}
    ustar,_=_series(interval,("ustar",))
    pa,pcol=_series(interval,("pa","pa_ref"))
    if pcol is None:
        return pd.Series(np.nan,index=interval.index),{"source":None,"derived":False,"reason":"Air pressure is needed to derive w*."}
    tk=temp_c+273.15; lam=latent_heat_j_kg(temp_c)
    rho=(pa*1000.0)/(RD*tk)
    kin_virtual=h/(rho*CP_AIR)+0.61*tk*le/(rho*lam)
    buoy=(G_GRAV/tk)*kin_virtual
    x=buoy*pbl
    w=np.cbrt(x.where(x>0))
    return w,{"source":"derived","derived":True,"pressure_source":pcol,"equation":"w*=[g/Theta_v * (w'Theta_v') * h_PBL]^(1/3)"}


def _deroo(interval: pd.DataFrame, h: pd.Series, le: pd.Series, temp_c: pd.Series, project: Mapping[str,object], config: Mapping[str,object], rescaled=False):
    z=config.get("effective_measurement_height_m")
    if z in (None,""): z=project.get("measurement_height")
    try: z=float(z)
    except Exception:
        return h.copy(),le.copy(),{"status":"needs_data","reason":"Effective measurement height is required."}
    if z<=0: return h.copy(),le.copy(),{"status":"needs_data","reason":"Effective measurement height must be positive."}

    # Prefer time-varying PBL height. Constant is an explicit user-entered sensitivity input.
    pbl,pbl_col=_series(interval,("pbl_height",))
    if pbl_col is None:
        try:
            const=float(config.get("pbl_height_m"))
            pbl=pd.Series(const,index=interval.index) if const>z else pd.Series(np.nan,index=interval.index)
            pbl_col="user_constant" if const>z else None
        except Exception:
            pbl_col=None
    if pbl_col is None:
        return h.copy(),le.copy(),{"status":"needs_data","reason":"Time-varying PBL height or an explicit PBL-height sensitivity value is required."}
    ustar,ucol=_series(interval,("ustar",))
    if ucol is None: return h.copy(),le.copy(),{"status":"needs_data","reason":"Friction velocity (u*) is required."}
    L,lcol=_series(interval,("obukhov_length",))
    if lcol is None: return h.copy(),le.copy(),{"status":"needs_data","reason":"Monin-Obukhov length is required to identify convective periods."}
    wstar,wmeta=_derive_wstar(interval,h,le,temp_c,pbl)
    if wstar.notna().sum()==0: return h.copy(),le.copy(),{"status":"needs_data","reason":wmeta.get("reason","Convective velocity scale w* is unavailable."),"wstar":wmeta}

    ratio=(ustar/wstar).replace([np.inf,-np.inf],np.nan)
    f1h=0.197*np.exp(-17.0*ratio)+0.156
    f1e=0.224*np.exp(-14.0*ratio)+0.071
    zzi=z/pbl
    f2h=0.21+10.69*zzi
    f2e=0.27+9.99*zzi
    ph=f1h*f2h; pe=f1e*f2e
    hdisp=(ph/(1.0-ph))*h
    ledisp=(pe/(1.0-pe))*le
    valid=(L<0)&h.notna()&le.notna()&pbl.gt(z)&ustar.notna()&wstar.notna()&ph.between(0,0.95)&pe.between(0,0.95)
    hc=h.copy(); lec=le.copy()
    if not rescaled:
        hc.loc[valid]=(h+hdisp).loc[valid]; lec.loc[valid]=(le+ledisp).loc[valid]
    else:
        dt=pd.to_datetime(interval["datetime"]); day=dt.dt.floor("D"); ae=interval["rn_phase3"]-interval["g_phase3_surface"]
        for d,idx in day.groupby(day).groups.items():
            ix=pd.Index(idx); m=interval.loc[ix,"phase3_daylight"].astype(bool)&ae.loc[ix].notna()&h.loc[ix].notna()&le.loc[ix].notna()
            if int(m.sum())<2: continue
            sae=float(ae.loc[ix][m].sum()); ste=float((h.loc[ix][m]+le.loc[ix][m]).sum())
            if sae<=0 or ste<=0: continue
            ebr=ste/sae; residual_day=(h.loc[ix]+le.loc[ix])*(1.0/ebr-1.0)
            denom=(hdisp.loc[ix]+ledisp.loc[ix]).replace(0,np.nan)
            shareh=hdisp.loc[ix]/denom; sharee=ledisp.loc[ix]/denom
            vm=valid.loc[ix]&shareh.notna()&sharee.notna()
            sel=ix[vm.to_numpy()]
            hc.loc[sel]=(h.loc[sel]+shareh.loc[sel]*residual_day.loc[sel])
            lec.loc[sel]=(le.loc[sel]+sharee.loc[sel]*residual_day.loc[sel])
    return hc,lec,{"status":"ready","n_applied":int(valid.sum()),"effective_measurement_height_m":z,"pbl_source":pbl_col,"wstar":wmeta,"low_height_warning":bool(z<20),"note":"De Roo sensitivity correction is applied only for convective periods (L<0) with valid scaling variables."}


def _et_from_le(le: pd.Series, temp: pd.Series, step_seconds: float):
    return pd.to_numeric(le,errors="coerce")*float(step_seconds)/latent_heat_j_kg(temp)


def _water_summaries(interval: pd.DataFrame, config: Mapping[str,object]):
    dt=pd.to_datetime(interval["datetime"]); step=infer_timestep(dt); step_seconds=float(step/pd.Timedelta(seconds=1)); expected=max(1,int(round(pd.Timedelta(days=1)/step)))
    temp,tcol=_series(interval,("at","at_ref"))
    fallback_used=False
    if tcol is None:
        fallback=float(config.get("et_temperature_fallback_c",20.0)); temp=pd.Series(fallback,index=interval.index); tcol="constant_fallback"; fallback_used=True
    else:
        missing=temp.isna();
        if missing.any() and bool(config.get("allow_et_temperature_fallback",True)):
            fallback=float(config.get("et_temperature_fallback_c",20.0)); temp=temp.fillna(fallback); fallback_used=True
    base_le=interval["LE_phase3_base"]
    interval["ET_phase3_mm_interval"]=_et_from_le(base_le,temp,step_seconds)
    for key,col in (("mauder","LE_corr_mauder"),("charuchittipan","LE_corr_charuchittipan"),("deroo_direct","LE_corr_deroo_direct"),("deroo_rescaled","LE_corr_deroo_rescaled")):
        if col in interval: interval[f"ET_{key}_mm_interval"]=_et_from_le(interval[col],temp,step_seconds)
    # Convert empirical LE bounds to gap-reconstruction ET bounds. Measured rows are exact wrt this reconstruction interval only.
    if "LE_empirical_lower95" in interval and "LE_empirical_upper95" in interval:
        lo=_num(interval["LE_empirical_lower95"]); hi=_num(interval["LE_empirical_upper95"])
        measured=(interval.get("LE_source",pd.Series("measured",index=interval.index)).astype(str)=="measured")
        lo=lo.where(~measured,base_le); hi=hi.where(~measured,base_le)
        interval["ET_gap_lower95_mm_interval"]=_et_from_le(lo,temp,step_seconds)
        interval["ET_gap_upper95_mm_interval"]=_et_from_le(hi,temp,step_seconds)

    eto,etocol=_series(interval,("ETo","eto_ref")); rain,raincol=_series(interval,("rain","rain_ref")); irr,irrcol=_series(interval,("irrigation","irrigation_ref")); swc,swccol=_series(interval,("swc","swc_ref"))
    if etocol: interval["ETo_phase3_mm_interval"]=eto
    if raincol: interval["rain_phase3_mm_interval"]=rain
    if irrcol: interval["irrigation_phase3_mm_interval"]=irr
    if swccol: interval["swc_phase3"]=swc

    day=dt.dt.floor("D"); threshold=float(config.get("daily_coverage_threshold_pct",90.0)); rows=[]
    etcols=[c for c in interval.columns if c.startswith("ET_") and c.endswith("_mm_interval")]
    for d,idx in day.groupby(day).groups.items():
        ix=pd.Index(idx); rec={"date":d,"expected_intervals":expected}
        valid=interval.loc[ix,"ET_phase3_mm_interval"].notna(); cov=100.0*float(valid.sum())/expected; rec["coverage_pct"]=cov; rec["complete_enough"]=bool(cov>=threshold)
        for c in etcols:
            total=interval.loc[ix,c].sum(min_count=1); rec[c.replace("_mm_interval","_mm_day_raw")]=_safe(total)
            rec[c.replace("_mm_interval","_mm_day")]=_safe(total) if cov>=threshold else None
        for c,outname in (("ETo_phase3_mm_interval","ETo_mm_day"),("rain_phase3_mm_interval","rain_mm_day"),("irrigation_phase3_mm_interval","irrigation_mm_day")):
            rec[outname]=_safe(interval.loc[ix,c].sum(min_count=1)) if c in interval else None
        rec["water_input_mm_day"]=_safe((rec.get("rain_mm_day") or 0)+(rec.get("irrigation_mm_day") or 0)) if (raincol or irrcol) else None
        rec["ET_over_ETo"]=_safe(rec.get("ET_phase3_mm_day")/rec["ETo_mm_day"]) if rec.get("ET_phase3_mm_day") is not None and rec.get("ETo_mm_day") not in (None,0) and rec["ETo_mm_day"]>0.05 else None
        rec["water_input_minus_ET_mm"]=_safe(rec["water_input_mm_day"]-rec["ET_phase3_mm_day"]) if rec.get("water_input_mm_day") is not None and rec.get("ET_phase3_mm_day") is not None else None
        rec["SWC_mean"]=_safe(swc.loc[ix].mean()) if swccol else None
        rows.append(rec)
    daily=pd.DataFrame(rows)
    daily["cumulative_ET_mm"]=pd.to_numeric(daily.get("ET_phase3_mm_day"),errors="coerce").fillna(0).cumsum()
    if "ETo_mm_day" in daily: daily["cumulative_ETo_mm"]=pd.to_numeric(daily["ETo_mm_day"],errors="coerce").fillna(0).cumsum()
    if "water_input_mm_day" in daily: daily["cumulative_water_input_mm"]=pd.to_numeric(daily["water_input_mm_day"],errors="coerce").fillna(0).cumsum()

    def agg_period(period):
        if daily.empty: return pd.DataFrame()
        d=daily.copy()
        if period=="month": d["period"]=pd.to_datetime(d["date"]).dt.to_period("M").astype(str)
        else: d["period"]=pd.to_datetime(d["date"]).dt.year.astype(str)
        rr=[]
        for p,g in d.groupby("period"):
            good=g["complete_enough"].fillna(False); n=len(g); ng=int(good.sum())
            rec={"period":p,"days":n,"accepted_days":ng,"days_coverage_pct":100*ng/n if n else np.nan}
            for c in [x for x in d.columns if x.endswith("_mm_day") or x in {"water_input_mm_day"}]: rec[c.replace("_day","")]=_safe(pd.to_numeric(g.loc[good,c],errors="coerce").sum(min_count=1))
            rec["SWC_mean"]=_safe(pd.to_numeric(g.get("SWC_mean"),errors="coerce").mean()) if "SWC_mean" in g else None
            rr.append(rec)
        return pd.DataFrame(rr)
    monthly=agg_period("month"); annual=agg_period("year")

    measured_et,measured_et_col=_series(interval,("ET",))
    compare={"source":measured_et_col,"n":0,"bias_mm_interval":None,"rmse_mm_interval":None}
    if measured_et_col:
        derived=interval["ET_phase3_mm_interval"]; ok=measured_et.notna()&derived.notna(); compare["n"]=int(ok.sum())
        if ok.any():
            diff=derived[ok]-measured_et[ok]; compare["bias_mm_interval"]=_safe(diff.mean()); compare["rmse_mm_interval"]=_safe(np.sqrt(np.mean(diff**2)))
    meta={"temperature_source":tcol,"temperature_fallback_used":fallback_used,"daily_coverage_threshold_pct":threshold,"expected_intervals_per_day":expected,"ETo_source":etocol,"rain_source":raincol,"irrigation_source":irrcol,"SWC_source":swccol,"input_ET_comparison":compare,
          "water_balance_note":"Water input minus ET is a simple diagnostic only; it does not account for runoff, drainage, lateral flow or soil-water storage change.",
          "uncertainty_note":"ET bounds derived from empirical LE residual intervals represent gap-reconstruction uncertainty only, not total EC measurement uncertainty."}
    return interval,daily,monthly,annual,meta


def _downsample(frame: pd.DataFrame, cols, limit=1800):
    if frame.empty: return []
    idx=np.arange(len(frame)) if len(frame)<=limit else np.unique(np.linspace(0,len(frame)-1,limit).round().astype(int))
    rows=[]
    for i in idx:
        rec={"datetime":pd.Timestamp(frame.iloc[i]["datetime"]).isoformat()}
        for c in cols:
            if c in frame: rec[c]=_safe(frame.iloc[i][c])
        rows.append(rec)
    return rows


def run_phase3_analysis(data: pd.DataFrame, project_meta: Optional[Mapping[str,object]]=None, config: Optional[Mapping[str,object]]=None):
    project=dict(project_meta or {}); config=dict(config or {}); out=data.copy(); warnings=[]
    if "datetime" not in out: raise ValueError("Phase 3 requires a standardized datetime column.")
    out["datetime"]=pd.to_datetime(out["datetime"])

    rn,rncol=_series(out,("rn","rn_ref")); out["rn_phase3"]=rn
    g_surface,g_storage,gmeta=soil_heat_storage_correction(out,config); out["g_phase3_surface"]=g_surface; out["g_storage_phase3"]=g_storage
    hbase,hcol=_series(out,("H",),prefer_filled=True); lebase,lecol=_series(out,("LE",),prefer_filled=True)
    out["H_phase3_base"]=hbase; out["LE_phase3_base"]=lebase
    daylight,day_source=_daylight_mask(out,rn,float(config.get("radiation_threshold_wm2",20.0))); out["phase3_daylight"]=daylight
    temp,tcol=_series(out,("at","at_ref"));
    if tcol is None: temp=pd.Series(float(config.get("et_temperature_fallback_c",20.0)),index=out.index)

    energy_ready = rncol is not None and g_surface.notna().any() and hcol is not None and lecol is not None
    measured_h=_num(out.get("H"),out.index); measured_le=_num(out.get("LE"),out.index)
    out["H_phase3_measured"]=measured_h; out["LE_phase3_measured"]=measured_le
    ae=rn-g_surface; measured_te=measured_h+measured_le; base_te=hbase+lebase
    primary=daylight&ae.gt(float(config.get("minimum_available_energy_wm2",20.0)))&measured_h.notna()&measured_le.notna()&rn.notna()&g_surface.notna()
    finalmask=daylight&ae.gt(float(config.get("minimum_available_energy_wm2",20.0)))&hbase.notna()&lebase.notna()&rn.notna()&g_surface.notna()
    energy_summary={"status":"ready" if energy_ready else "needs_data","rn_source":rncol,"g":gmeta,"daylight_source":day_source,"H_base_source":hcol,"LE_base_source":lecol,
                    "radiation_threshold_wm2":float(config.get("radiation_threshold_wm2",20.0)),
                    "minimum_available_energy_wm2":float(config.get("minimum_available_energy_wm2",20.0)),
                    "measured_primary":_regression_metrics(ae,measured_te,primary) if energy_ready else {},"reconstructed_primary":_regression_metrics(ae,base_te,finalmask) if energy_ready else {}}
    if not energy_ready:
        missing=[]
        if rncol is None: missing.append("net radiation")
        if not g_surface.notna().any():
            missing.append(gmeta.get("reason") or "soil heat flux")
        if hcol is None: missing.append("H")
        if lecol is None: missing.append("LE")
        energy_summary["missing_required"]=missing

    corrections=[]; mauder_daily=pd.DataFrame()
    if energy_ready:
        hm,lm,mauder_daily=_mauder_daily(out,hbase,lebase); out["H_corr_mauder"]=hm; out["LE_corr_mauder"]=lm
        corrections.append({"method":"Mauder2013_daily_Bowen","status":"ready","n_applied":int((hm!=hbase).fillna(False).sum()),"description":"Daily closure factor; preserves interval Bowen ratio on radiation-active periods."})
        hc,lc,cmeta=_charuchittipan(out,hbase,lebase,temp); out["H_corr_charuchittipan"]=hc; out["LE_corr_charuchittipan"]=lc
        corrections.append({"method":"Charuchittipan2014_buoyancy","status":"ready",**cmeta})
        hd,ld,dmeta=_deroo(out,hbase,lebase,temp,project,config,rescaled=False); out["H_corr_deroo_direct"]=hd; out["LE_corr_deroo_direct"]=ld; corrections.append({"method":"DeRoo2018_direct",**dmeta})
        hr,lr,rmeta=_deroo(out,hbase,lebase,temp,project,config,rescaled=True); out["H_corr_deroo_rescaled"]=hr; out["LE_corr_deroo_rescaled"]=lr; corrections.append({"method":"DeRoo2018_EBR_rescaled",**rmeta})
        # Diagnostic closure after each sensitivity correction.
        for c in corrections:
            key=c["method"]
            mapping={"Mauder2013_daily_Bowen":("H_corr_mauder","LE_corr_mauder"),"Charuchittipan2014_buoyancy":("H_corr_charuchittipan","LE_corr_charuchittipan"),"DeRoo2018_direct":("H_corr_deroo_direct","LE_corr_deroo_direct"),"DeRoo2018_EBR_rescaled":("H_corr_deroo_rescaled","LE_corr_deroo_rescaled")}
            hcname,lcname=mapping[key]; m=daylight&ae.gt(20)&out[hcname].notna()&out[lcname].notna(); c["closure"]=_regression_metrics(ae,out[hcname]+out[lcname],m)

    out,daily,monthly,annual,watermeta=_water_summaries(out,config) if lecol is not None else (out,pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),{"status":"needs_data","reason":"LE is required for ET analysis."})
    energy_daily=_energy_daily(out,"H_phase3_measured","LE_phase3_measured") if energy_ready else pd.DataFrame()
    energy_monthly=_energy_aggregate(out,"M","H_phase3_measured","LE_phase3_measured") if energy_ready else pd.DataFrame()
    energy_annual=_energy_aggregate(out,"Y","H_phase3_measured","LE_phase3_measured") if energy_ready else pd.DataFrame()
    energy_daily_reconstructed=_energy_daily(out,"H_phase3_base","LE_phase3_base") if energy_ready else pd.DataFrame()

    if gmeta.get("status")!="ready": warnings.append(gmeta.get("reason","Ground heat flux correction unavailable."))
    for c in corrections:
        if c.get("status")!="ready": warnings.append(f"{c['method']}: {c.get('reason','not available')}")
        if c.get("low_height_warning"): warnings.append("De Roo direct correction has limited applicability at low effective measurement height; inspect the EBR-rescaled sensitivity product as well.")
    if watermeta.get("temperature_fallback_used"): warnings.append("Some ET conversion used the explicit temperature fallback because air temperature was unavailable for those records.")
    # Keep the UI/report concise when multiple correction products raise the same caveat.
    warnings=list(dict.fromkeys(warnings))

    # Core interval outputs first, followed by correction/water products.
    keep=["datetime","rn_phase3","g_phase3_surface","g_storage_phase3","phase3_daylight","H_phase3_measured","LE_phase3_measured","H_phase3_base","LE_phase3_base"]
    keep += [c for c in out.columns if c.startswith(("H_corr_","LE_corr_","ET_")) or c in {"ETo_phase3_mm_interval","rain_phase3_mm_interval","irrigation_phase3_mm_interval","swc_phase3"}]
    interval_export=out[[c for c in dict.fromkeys(keep) if c in out]].copy()

    flat_corrections=[]
    for c in corrections:
        row={k:v for k,v in c.items() if k not in {"closure","wstar"}}
        closure=c.get("closure") or {}
        for k,v in closure.items(): row[f"closure_{k}"]=v
        wmeta=c.get("wstar") or {}
        for k,v in wmeta.items(): row[f"wstar_{k}"]=v
        flat_corrections.append(row)
    correction_summary=pd.DataFrame(flat_corrections)
    result={
        "summary":{"energy":energy_summary,"water":watermeta,"corrections":corrections,"warnings":warnings},
        "energy_daily":energy_daily,"energy_monthly":energy_monthly,"energy_annual":energy_annual,"energy_daily_reconstructed":energy_daily_reconstructed,
        "correction_daily":mauder_daily,"correction_summary":correction_summary,
        "water_daily":daily,"water_monthly":monthly,"water_annual":annual,
        "interval":interval_export,
        "plot_energy":_downsample(out,["rn_phase3","g_phase3_surface","phase3_daylight","H_phase3_measured","LE_phase3_measured","H_phase3_base","LE_phase3_base"]),
    }
    return out,result


def report_markdown(result: Mapping[str,object], project: Optional[Mapping[str,object]]=None) -> str:
    project=dict(project or {}); s=result.get("summary",{}); e=s.get("energy",{}); w=s.get("water",{})
    lines=["# FluxGapFill Energy and Water Report","",f"Project: **{project.get('project_name') or project.get('site_name') or 'Untitled'}**","",
           "## Energy-balance diagnostics","",f"Status: **{e.get('status','unknown')}**",f"Net radiation source: `{e.get('rn_source')}`",f"Ground heat flux mode: `{(e.get('g') or {}).get('mode','unavailable')}`",""]
    m=e.get("measured_primary") or {}
    if m:
        lines += [f"Measured-only daytime EBR (ratio of sums): **{m.get('ratio_of_sums')}**",f"OLS slope / intercept / R²: **{m.get('slope')} / {m.get('intercept')} / {m.get('r2')}**",f"N intervals: **{m.get('n')}**",""]
    lines += ["## Correction sensitivity products","","These products are kept separate from the raw/reconstructed fluxes; Phase 3 never overwrites the original H or LE.",""]
    for c in s.get("corrections",[]): lines.append(f"- **{c.get('method')}** — {c.get('status')}" + (f"; {c.get('reason')}" if c.get('reason') else ""))
    lines += ["","## ET / water analysis","",f"Temperature source: `{w.get('temperature_source')}`",f"Daily completeness threshold: **{w.get('daily_coverage_threshold_pct')}%**",f"ETo source: `{w.get('ETo_source')}`",f"Rain source: `{w.get('rain_source')}`",f"Irrigation source: `{w.get('irrigation_source')}`","",w.get("water_balance_note","") ,"",w.get("uncertainty_note","")]
    warns=s.get("warnings",[])
    if warns:
        lines += ["","## Warnings",""]+[f"- {x}" for x in warns]
    lines += ["","## Method notes","","Energy-balance correction outputs are sensitivity scenarios. They should not be interpreted as proof that a specific physical mechanism caused the observed non-closure.",""]
    return "\n".join(lines)
