from __future__ import annotations
import json, math
from typing import Optional
import numpy as np
import pandas as pd
from scipy.optimize import least_squares

T0_C=-46.02; TREF_C=15.0; KAPPA=0.4

def _num(df,names):
    for n in names:
        if n in df:
            s=pd.to_numeric(df[n],errors='coerce')
            if s.notna().any(): return s,n
    return pd.Series(np.nan,index=df.index,dtype=float),None

def _season(dt):
    m=pd.DatetimeIndex(dt).month
    return np.select([np.isin(m,[12,1,2]),np.isin(m,[3,4,5]),np.isin(m,[6,7,8]),np.isin(m,[9,10,11])],['DJF','MAM','JJA','SON'],default='UNK')

def _night_mask(df,thr=20):
    sr,src=_num(df,['sr','sr_ref'])
    if src:return sr.lt(float(thr))&sr.notna(),f'{src} < {thr:g} W m-2'
    dt=pd.to_datetime(df.datetime);h=dt.dt.hour+dt.dt.minute/60
    return ((h<6)|(h>=18)),'clock-hour fallback'

def _nee(df,sign='positive_to_atmosphere'):
    s,src=_num(df,['NEE_filled','NEE'])
    if sign=='positive_to_uptake':s=-s
    return s,src

def _mpt_one(f,temp_classes=7,ustar_classes=20,tol=.05):
    f=f.dropna(subset=['nee','ustar','at']).copy()
    if len(f)<120:return np.nan
    try:f['tbin']=pd.qcut(f.at,q=min(temp_classes,max(2,len(f)//30)),duplicates='drop')
    except Exception:f['tbin']=0
    vals=[]
    for _,g in f.groupby('tbin',observed=True):
        if len(g)<50:continue
        try:g=g.assign(ubin=pd.qcut(g.ustar,q=min(ustar_classes,max(6,len(g)//12)),duplicates='drop'))
        except Exception:continue
        b=g.groupby('ubin',observed=True).agg(u=('ustar','mean'),y=('nee','mean')).dropna().sort_values('u')
        if len(b)<6:continue
        u=b.u.to_numpy();y=b.y.to_numpy();found=np.nan
        for i in range(1,len(b)-3):
            plateau=np.nanmean(y[i+1:]);scale=max(abs(plateau),np.nanstd(y),1e-6)
            slope=np.polyfit(u[i:],y[i:],1)[0] if len(u[i:])>=3 else np.inf
            ns=abs(slope)*max(np.ptp(u[i:]),1e-6)/scale
            if abs(y[i]-plateau)/scale<=tol and ns<=.15:found=float(u[i]);break
        if np.isfinite(found):vals.append(found)
    return float(np.median(vals)) if vals else np.nan

def _cpd_one(f,ustar_classes=20):
    f=f.dropna(subset=['nee','ustar']).copy()
    if len(f)<120:return np.nan,[]
    try:f['ubin']=pd.qcut(f.ustar,q=min(ustar_classes,max(8,len(f)//15)),duplicates='drop')
    except Exception:return np.nan,[]
    b=f.groupby('ubin',observed=True).agg(u=('ustar','mean'),y=('nee','mean')).dropna().sort_values('u')
    if len(b)<8:return np.nan,[]
    x=b.u.to_numpy();y=b.y.to_numpy();best=None
    for k in range(3,len(x)-3):
        A=np.c_[x[:k+1],np.ones(k+1)];coef=np.linalg.lstsq(A,y[:k+1],rcond=None)[0];pred=A@coef;level=float(np.mean(y[k+1:]))
        sse=float(np.sum((y[:k+1]-pred)**2)+np.sum((y[k+1:]-level)**2))
        if best is None or sse<best[0]:best=(sse,k)
    k=best[1];return float(x[k]),[{'ustar':float(a),'nee':float(b)} for a,b in zip(x,y)]

def run_ustar_analysis(df,config):
    nee,ns=_nee(df,str(config.get('nee_sign','positive_to_atmosphere')));us,usrc=_num(df,['ustar']);at,ats=_num(df,['at','at_ref'])
    if not(ns and usrc and ats):return {'status':'needs_data','missing':[x for x,v in [('NEE',ns),('u*',usrc),('air temperature',ats)] if not v]}
    night,basis=_night_mask(df,float(config.get('radiation_threshold',20)));f=pd.DataFrame({'datetime':pd.to_datetime(df.datetime),'nee':nee,'ustar':us,'at':at});f['season']=_season(f.datetime);f=f[night.to_numpy()].dropna();f=f[f.ustar>=0]
    if len(f)<200:return {'status':'insufficient_data','n_night':int(len(f)),'night_basis':basis}
    tc=int(config.get('temp_classes',7));uc=int(config.get('ustar_classes',20));boot=int(config.get('bootstraps',100));rng=np.random.default_rng(int(config.get('seed',42)))
    seasonal={}
    for s in ['DJF','MAM','JJA','SON']:
        g=f[f.season==s];m=_mpt_one(g,tc,uc);c,_=_cpd_one(g,uc);seasonal[s]={'n':int(len(g)),'mpt':float(m) if np.isfinite(m) else None,'cpd':float(c) if np.isfinite(c) else None}
    mb=[];cb=[]
    for _ in range(max(0,boot)):
        ms=[];cs=[]
        for s in ['DJF','MAM','JJA','SON']:
            g=f[f.season==s]
            if len(g)<120:continue
            q=g.iloc[rng.integers(0,len(g),len(g))];m=_mpt_one(q,tc,uc);c,_=_cpd_one(q,uc)
            if np.isfinite(m):ms.append(m)
            if np.isfinite(c):cs.append(c)
        if ms:mb.append(max(ms))
        if cs:cb.append(max(cs))
    def st(a):
        if not a:return {'n':0,'median':None,'p05':None,'p95':None}
        x=np.asarray(a);return {'n':len(a),'median':float(np.median(x)),'p05':float(np.quantile(x,.05)),'p95':float(np.quantile(x,.95))}
    M,C=st(mb),st(cb)
    if M['median'] is None:
        v=[x['mpt'] for x in seasonal.values() if x['mpt'] is not None];M['median']=max(v) if v else None
    if C['median'] is None:
        v=[x['cpd'] for x in seasonal.values() if x['cpd'] is not None];C['median']=max(v) if v else None
    _,curve=_cpd_one(f,uc);method=str(config.get('selected_method','MPT')).upper();thr=(M if method=='MPT' else C).get('median') or M.get('median') or C.get('median')
    return {'status':'ready','n_night':int(len(f)),'night_basis':basis,'seasonal':seasonal,'MPT':M,'CPD':C,'selected_method':method,'selected_threshold':thr,'overall_curve':curve}

def _lt(T,R,E0):
    T=np.asarray(T,float);return R*np.exp(E0*(1/(TREF_C-T0_C)-1/np.maximum(T-T0_C,1e-4)))

def run_carbon_partition(df,project,config,ures=None):
    nee,ns=_nee(df,str(config.get('nee_sign','positive_to_atmosphere')));at,ats=_num(df,['at','at_ref']);us,usrc=_num(df,['ustar'])
    if not(ns and ats):return {'status':'needs_data','missing':[x for x,v in [('NEE',ns),('air temperature',ats)] if not v]}
    night,basis=_night_mask(df,float(config.get('radiation_threshold',20)));uth=config.get('ustar_threshold')
    if uth in ('',None):uth=(ures or {}).get('selected_threshold')
    uth=float(uth) if uth not in ('',None) else None
    valid=night&nee.notna()&at.notna()
    if uth is not None and usrc:valid&=us.ge(uth)
    f=pd.DataFrame({'datetime':pd.to_datetime(df.datetime),'nee':nee,'at':at})[valid.to_numpy()].dropna();f=f[(f['at']>T0_C+2)&(f['at']<60)]
    if len(f)<100:return {'status':'insufficient_data','n_night_used':int(len(f))}
    T=f['at'].to_numpy();Y=f.nee.to_numpy();r0=max(float(np.nanmedian(Y)),.2)
    fit=least_squares(lambda p:_lt(T,p[0],p[1])-Y,[r0,120],bounds=([1e-6,30],[max(200,np.nanpercentile(abs(Y),99)*10),450]),loss='soft_l1',max_nfev=1500)
    R,E0=map(float,fit.x);dt=pd.to_datetime(df.datetime);centers=pd.date_range(dt.min().floor('D'),dt.max().ceil('D'),freq=f"{int(config.get('window_step_days',5))}D");half=pd.Timedelta(days=float(config.get('window_days',15))/2);pts=[]
    for c in centers:
        g=f[(f.datetime>=c-half)&(f.datetime<=c+half)]
        if len(g)<30:continue
        phi=np.exp(E0*(1/(TREF_C-T0_C)-1/np.maximum(g['at'].to_numpy()-T0_C,1e-4)));rat=g.nee.to_numpy()/phi;rat=rat[np.isfinite(rat)&(rat>0)]
        if len(rat)>=15:pts.append((c,float(np.median(rat))))
    if pts:
        xp=np.array([pd.Timestamp(x[0]).value for x in pts],float);fp=np.array([x[1] for x in pts]);Rdyn=np.interp(dt.astype('int64').to_numpy(float),xp,fp,left=fp[0],right=fp[-1])
    else:Rdyn=np.full(len(df),R)
    reco=_lt(at.to_numpy(),Rdyn,E0);gpp=reco-nee.to_numpy();step=float(pd.Series(dt).diff().median()/pd.Timedelta(seconds=1));step=step if np.isfinite(step) and step>0 else 1800;conv=step*12e-6
    out=pd.DataFrame({'datetime':dt,'NEE_for_partition':nee,'Reco':reco,'GPP':gpp,'Rref_dynamic':Rdyn});out['date']=out.datetime.dt.floor('D');daily=out.groupby('date').agg(NEE_gC_m2_day=('NEE_for_partition',lambda s:float(np.nansum(s)*conv)),Reco_gC_m2_day=('Reco',lambda s:float(np.nansum(s)*conv)),GPP_gC_m2_day=('GPP',lambda s:float(np.nansum(s)*conv))).reset_index()
    pred=_lt(T,R,E0);r2=float(1-np.sum((pred-Y)**2)/np.sum((Y-Y.mean())**2)) if np.sum((Y-Y.mean())**2)>0 else np.nan
    return {'status':'ready','summary':{'nee_source':ns,'temperature_source':ats,'night_basis':basis,'ustar_threshold':uth,'n_night_used':int(len(f)),'E0_K':E0,'Rref_global_umol_m2_s':R,'night_model_r2':r2,'night_model_rmse':float(np.sqrt(np.mean((pred-Y)**2)))},'interval':out,'daily':daily}

def _ffp(E,N,wd,zm,h,ol,sv,us,umean):
    if not all(np.isfinite([wd,zm,h,ol,sv,us,umean])) or min(zm,h-zm,sv,us,umean)<=0:return None
    th=np.deg2rad(wd);x=E*np.sin(th)+N*np.cos(th);y=E*np.cos(th)-N*np.sin(th);den=(umean/us)*KAPPA
    xs=(x/zm)*(1-zm/h)/den;mask=xs>0.1359;f=np.zeros_like(E,float)
    if not np.any(mask):return f
    q=xs[mask];fstar=1.4524*np.power(q-.1359,-1.9914)*np.exp(-1.4622/(q-.1359));fci=fstar/zm*(1-zm/h)/den;sy=2.17*np.sqrt(1.66*q*q/(1+20*q))*zm*sv/us;v=np.zeros_like(q);ok=sy>0;v[ok]=fci[ok]/(np.sqrt(2*np.pi)*sy[ok])*np.exp(-(y[mask][ok]**2)/(2*sy[ok]**2));f[mask]=v;return f

def _poly_mask(X,Y,p):
    p=np.asarray(p,float);x=X.ravel();y=Y.ravel();inside=np.zeros(len(x),bool)
    if len(p)<3:return inside.reshape(X.shape)
    xj,yj=p[-1]
    for xi,yi in p:
        inside^=((yi>y)!=(yj>y))&(x<(xj-xi)*(y-yi)/(yj-yi+1e-300)+xi);xj,yj=xi,yi
    return inside.reshape(X.shape)

def _aois(text,lat0=None,lon0=None):
    if not text:return []
    o=json.loads(text) if isinstance(text,str) else text;out=[]
    for i,ft in enumerate(o.get('features',[])):
        g=ft.get('geometry') or {};pr=ft.get('properties') or {};rings=[]
        if g.get('type')=='Polygon' and g.get('coordinates'):rings=[g['coordinates'][0]]
        elif g.get('type')=='MultiPolygon':rings=[p[0] for p in g.get('coordinates',[]) if p]
        for ring in rings:
            a=np.asarray(ring,float)
            if lat0 is not None and lon0 is not None and np.nanmax(abs(a[:,0]))<=180 and np.nanmax(abs(a[:,1]))<=90:
                e=(a[:,0]-float(lon0))*111320*np.cos(np.deg2rad(float(lat0)));n=(a[:,1]-float(lat0))*110540;a=np.c_[e,n]
            out.append({'name':str(pr.get('name') or pr.get('class') or f'AOI {i+1}'),'exclude':bool(pr.get('exclude',False)),'polygon':a[:,:2].tolist()})
    return out

def run_footprint(df,project,config,geojson=None):
    us,usrc=_num(df,['ustar']);wd,wsrc=_num(df,['wind_dir','wind_dir_ref']);ol,osrc=_num(df,['obukhov_length']);sv,ssrc=_num(df,['sigma_v']);ws,wssrc=_num(df,['ws','ws_ref']);pbl,psrc=_num(df,['pbl_height'])
    mh=config.get('measurement_height') or project.get('measurement_height');ch=config.get('canopy_height') if config.get('canopy_height') not in ('',None) else project.get('canopy_height');const=config.get('pbl_height')
    missing=[n for n,v in [('u*',usrc),('wind direction',wsrc),('Monin-Obukhov length',osrc),('sigma_v',ssrc),('wind speed',wssrc)] if not v]
    if mh in ('',None):missing.append('measurement height')
    if not psrc and const in ('',None):missing.append('PBL height')
    if missing:return {'status':'needs_data','missing':missing}
    mh=float(mh);ch=float(ch) if ch not in ('',None) else 0.;d=config.get('displacement_height');d=float(d) if d not in ('',None) else .67*ch;zm=mh-d;h=pbl.fillna(float(const)) if psrc and const not in ('',None) else (pbl if psrc else pd.Series(float(const),index=df.index))
    ext=float(config.get('domain_extent_m',1000));ng=max(51,min(int(config.get('grid_points',101)),201));x=np.linspace(-ext,ext,ng);y=np.linspace(-ext,ext,ng);E,N=np.meshgrid(x,y);dx=x[1]-x[0];dy=y[1]-y[0];stride=max(1,int(math.ceil(len(df)/max(1,int(config.get('max_intervals',1200))))));acc=np.zeros_like(E);valid=0;peaks=[];aois=_aois(geojson,project.get('latitude'),project.get('longitude'));masks=[_poly_mask(E,N,a['polygon']) for a in aois];asum=np.zeros(len(aois))
    for i in range(0,len(df),stride):
        f=_ffp(E,N,wd.iat[i],zm,h.iat[i],ol.iat[i],sv.iat[i],us.iat[i],ws.iat[i])
        if f is None:continue
        z=float(np.sum(f)*dx*dy)
        if not np.isfinite(z) or z<=0:continue
        f/=z;acc+=f;valid+=1;k=np.unravel_index(np.argmax(f),f.shape);peaks.append(float(np.hypot(E[k],N[k])))
        for j,m in enumerate(masks):asum[j]+=float(np.sum(f[m])*dx*dy)
    if not valid:return {'status':'insufficient_data','valid_footprints':0}
    clim=acc/valid;clim/=np.sum(clim)*dx*dy;flat=clim.ravel();order=np.argsort(flat)[::-1];cum=np.cumsum(flat[order]*dx*dy);areas={}
    for r in [.5,.8,.9]:k=int(np.searchsorted(cum,r))+1;areas[str(int(r*100))]={'area_m2':float(k*dx*dy),'equivalent_radius_m':float(np.sqrt(k*dx*dy/np.pi))}
    return {'status':'ready','summary':{'valid_footprints':valid,'median_peak_distance_m':float(np.median(peaks)),'source_areas':areas,'measurement_height_m':mh,'displacement_height_m':d,'zm_m':zm,'aoi_count':len(aois),'pbl_source':psrc or 'user constant'},'x':x.tolist(),'y':y.tolist(),'z':clim.tolist(),'aoi_summary':[{'name':a['name'],'exclude':a['exclude'],'mean_contribution_fraction':float(asum[j]/valid)} for j,a in enumerate(aois)]}

def run_phase4_analysis(df,project,config,geojson=None):
    u=run_ustar_analysis(df,config.get('ustar',{})) if config.get('run_ustar',True) else {'status':'skipped'}
    c=run_carbon_partition(df,project,config.get('carbon',{}),u if u.get('status')=='ready' else None) if config.get('run_carbon',True) else {'status':'skipped'}
    f=run_footprint(df,project,config.get('footprint',{}),geojson) if config.get('run_footprint',True) else {'status':'skipped'}
    return {'ustar':u,'carbon':c,'footprint':f}

def phase4_report(result,project):
    u=result.get('ustar',{});c=result.get('carbon',{});f=result.get('footprint',{});lines=['# FluxGapFill Carbon and Footprint Report','',f"Project: **{project.get('project_name') or project.get('site_name') or 'Untitled'}**",'',f"u* status: {u.get('status')}",f"Selected u*: {u.get('selected_threshold')}",f"Carbon status: {c.get('status')}",f"Footprint status: {f.get('status')}",'','Methods: independent MPT/CPD-style threshold implementations; Reichstein-style nighttime Lloyd–Taylor carbon partitioning; Kljun et al. (2015) FFP footprint parameterisation.','']
    return '\n'.join(lines)
