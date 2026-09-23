let pyodide=null,ready=false,initPromise=null;
const BUILD='20260923v100';
const RUNTIMES=[
 {module:'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs',base:'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/'},
 {module:'https://cdn.jsdelivr.net/pyodide/v314.0.6/full/pyodide.mjs',base:'https://cdn.jsdelivr.net/pyodide/v314.0.6/full/'},
 {module:'https://unpkg.com/pyodide@314.0.7/pyodide.mjs',base:'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/'}
];
const PYFILES=['data_processor.py','scientific_qc.py','calibration_utils.py','gap_filler.py','advanced_ml_gap_filler.py','external_sources.py','universal_import.py','phase2_qc.py','validation_engine.py','phase3_energy_water.py','phase4_carbon_footprint.py','browser_engine.py'];
function send(type,payload={}){self.postMessage({type,...payload})}
async function loadRuntime(){const errors=[];for(const rt of RUNTIMES){try{send('progress',{value:2,message:'Connecting to scientific Python runtime'});const mod=await import(rt.module);if(!mod||typeof mod.loadPyodide!=='function')throw new Error('loadPyodide export not found');const instance=await mod.loadPyodide({indexURL:rt.base,packageBaseUrl:rt.base});return {instance,base:rt.base}}catch(err){errors.push(`${rt.module}: ${err?.message||err}`)}}throw new Error('Unable to load Pyodide from available mirrors. '+errors.join(' | '))}
async function init(){if(ready)return;if(initPromise)return initPromise;initPromise=(async()=>{send('progress',{value:2,message:'Starting Python scientific runtime'});const loaded=await loadRuntime();pyodide=loaded.instance;send('progress',{value:9,message:'Loading NumPy, pandas, SciPy, scikit-learn, XGBoost and Excel support'});await pyodide.loadPackage(['numpy','pandas','scipy','scikit-learn','xgboost','tzdata','python-calamine']);send('progress',{value:42,message:'Loading FluxGapFill 1.0 scientific engine'});for(const file of PYFILES){const response=await fetch('/py/'+file+'?v='+BUILD,{cache:'no-store'});if(!response.ok)throw new Error(`Failed to load /py/${file} (${response.status})`);const text=await response.text();pyodide.FS.writeFile('/home/pyodide/'+file,text,{encoding:'utf8'})}pyodide.runPython("import sys; sys.path.insert(0, '/home/pyodide'); import browser_engine");ready=true;send('progress',{value:100,message:'Scientific runtime ready'});send('ready')})();try{await initPromise}catch(err){initPromise=null;throw err}}
function cleanName(name){return String(name||'input').replace(/[^A-Za-z0-9._-]/g,'_')}
async function writeUpload(file,prefix='input'){if(!file)return null;const path='/tmp/'+prefix+'_'+Date.now()+'_'+cleanName(file.name);const buf=new Uint8Array(await file.arrayBuffer());pyodide.FS.writeFile(path,buf);return path}
async function callPy(expr){const result=await pyodide.runPythonAsync(expr);return JSON.parse(result)}
self.onmessage=async event=>{const m=event.data||{};try{await init();
 if(m.cmd==='inspect'){
   const p=await writeUpload(m.primaryFile,'primary'),s=await writeUpload(m.supplementalFile,'supp');
   const primary=await callPy(`browser_engine.inspect_input(${JSON.stringify(p)}, 'primary')`);
   const supplemental=s?await callPy(`browser_engine.inspect_input(${JSON.stringify(s)}, 'supplemental')`):null;
   send('inspected',{primary,supplemental});
 }else if(m.cmd==='inspectWater'){
   const w=await writeUpload(m.waterFile,'water');
   const water=await callPy(`browser_engine.inspect_input(${JSON.stringify(w)}, 'water')`);send('waterInspected',{water});
 }else if(m.cmd==='loadWater'){
   const w=await writeUpload(m.waterFile,'water');const ws=JSON.stringify(JSON.stringify(m.waterSpec||{}));
   const res=await callPy(`browser_engine.add_water_input_file(${JSON.stringify(w)}, ${ws}, ${JSON.stringify(m.timestampConvention||'end')})`);send('waterLoaded',{result:res});
 }else if(m.cmd==='phase3'){
   const res=await callPy(`browser_engine.run_phase3(${JSON.stringify(JSON.stringify(m.config||{}))})`);send('phase3Done',{result:res});
 }else if(m.cmd==='exportPhase3'){
   const kind=JSON.stringify(m.kind||'interval');const csv=await pyodide.runPythonAsync(`browser_engine.phase3_csv(${kind})`);send('exportReady',{text:csv,filename:`fluxgapfill_energy_water_${m.kind||'interval'}.csv`,mime:'text/csv'});
 }else if(m.cmd==='exportPhase3Report'){
   const text=await pyodide.runPythonAsync('browser_engine.phase3_report_md()');send('exportReady',{text,filename:'fluxgapfill_energy_water_report.md',mime:'text/markdown'});
 }else if(m.cmd==='loadAoi'){
   const p=await writeUpload(m.aoiFile,'aoi');const res=await callPy(`browser_engine.load_phase4_geojson(${JSON.stringify(p)})`);send('aoiLoaded',{result:res});
 }else if(m.cmd==='phase4'){
   const res=await callPy(`browser_engine.run_phase4(${JSON.stringify(JSON.stringify(m.config||{}))})`);send('phase4Done',{result:res});
 }else if(m.cmd==='exportPhase4'){
   const kind=JSON.stringify(m.kind||'carbon_daily');const csv=await pyodide.runPythonAsync(`browser_engine.phase4_csv(${kind})`);send('exportReady',{text:csv,filename:`fluxgapfill_carbon_footprint_${m.kind||'carbon_daily'}.csv`,mime:'text/csv'});
 }else if(m.cmd==='exportPhase4Report'){
   const text=await pyodide.runPythonAsync('browser_engine.phase4_report_md()');send('exportReady',{text,filename:'fluxgapfill_carbon_footprint_report.md',mime:'text/markdown'});
 }else if(m.cmd==='loadUniversal'){
   const p=await writeUpload(m.primaryFile,'primary'),s=await writeUpload(m.supplementalFile,'supp');
   const ps=JSON.stringify(JSON.stringify(m.primarySpec||{})),ss=m.supplementalSpec?JSON.stringify(JSON.stringify(m.supplementalSpec)):"None",pj=JSON.stringify(JSON.stringify(m.project||{}));
   const res=await callPy(`browser_engine.load_universal_project(${JSON.stringify(p)}, ${ps}, ${s?JSON.stringify(s):'None'}, ${ss}, ${pj})`);send('loaded',{result:res});
 }else if(m.cmd==='updateProject'){
   const res=await callPy(`browser_engine.update_project_metadata(${JSON.stringify(JSON.stringify(m.project||{}))})`);send('capabilities',{result:res});
 }else if(m.cmd==='qc'){
   const res=await callPy(`browser_engine.run_qc(${JSON.stringify(JSON.stringify(m.config||{}))})`);send('qcDone',{result:res});
 }else if(m.cmd==='benchmark'){
   const res=await callPy(`browser_engine.run_benchmark(${JSON.stringify(JSON.stringify(m.targets))}, ${JSON.stringify(JSON.stringify(m.durations))}, ${Number(m.windows||2)}, ${JSON.stringify(JSON.stringify(m.candidates))})`);send('benchmarkDone',{result:res});
 }else if(m.cmd==='fill'){
   const res=await callPy(`browser_engine.run_gap_fill(${JSON.stringify(JSON.stringify(m.targets))}, ${JSON.stringify(m.mode)}, ${JSON.stringify(m.fixedModel)}, ${Number(m.maxGapDays||60)})`);send('fillDone',{result:res});
 }else if(m.cmd==='export'){
   const csv=await pyodide.runPythonAsync(`browser_engine.export_csv(${m.compact?'True':'False'})`);send('exportReady',{text:csv,filename:m.compact?'fluxgapfill_results_compact.csv':'fluxgapfill_full_provenance.csv',mime:'text/csv'});
 }else if(m.cmd==='exportBenchmark'){
   const kind=JSON.stringify(m.kind||'detail');const csv=await pyodide.runPythonAsync(`browser_engine.benchmark_csv(${kind})`);send('exportReady',{text:csv,filename:`fluxgapfill_validation_${m.kind||'detail'}.csv`,mime:'text/csv'});
 }else if(m.cmd==='exportQc'){
   const csv=await pyodide.runPythonAsync('browser_engine.qc_audit_csv()');send('exportReady',{text:csv,filename:'fluxgapfill_qc_audit.csv',mime:'text/csv'});
 }else if(m.cmd==='exportReport'){
   const text=await pyodide.runPythonAsync('browser_engine.validation_report_md()');send('exportReady',{text,filename:'fluxgapfill_validation_report.md',mime:'text/markdown'});
 }
 }catch(err){send('error',{message:String(err&&err.message||err),stack:String(err&&err.stack||'')})}}
init().catch(err=>send('error',{message:'Scientific runtime failed to initialize: '+(err?.message||err),stack:err?.stack||''}));
