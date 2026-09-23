let pyodide = null;
let ready = false;
let initPromise = null;

// Pyodide 314+ requires a module-type Web Worker. Classic importScripts()
// workers are not supported because pyodide.asm.mjs is an ES module.
const RUNTIMES = [
  {
    module: 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs',
    base: 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/'
  },
  {
    module: 'https://cdn.jsdelivr.net/pyodide/v314.0.6/full/pyodide.mjs',
    base: 'https://cdn.jsdelivr.net/pyodide/v314.0.6/full/'
  },
  {
    module: 'https://unpkg.com/pyodide@314.0.7/pyodide.mjs',
    base: 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/'
  }
];
const PYFILES = ['data_processor.py','scientific_qc.py','calibration_utils.py','gap_filler.py','advanced_ml_gap_filler.py','external_sources.py','browser_engine.py'];

function send(type, payload={}){ self.postMessage({type, ...payload}); }

async function loadRuntime(){
  const errors=[];
  for(const rt of RUNTIMES){
    try{
      send('progress',{value:2,message:'Connecting to scientific Python runtime'});
      const mod = await import(rt.module);
      if(!mod || typeof mod.loadPyodide !== 'function') throw new Error('loadPyodide export not found');
      const instance = await mod.loadPyodide({indexURL: rt.base, packageBaseUrl: rt.base});
      return {instance, base:rt.base};
    }catch(err){
      errors.push(`${rt.module}: ${err?.message || err}`);
    }
  }
  throw new Error('Unable to load Pyodide from the available runtime mirrors. ' + errors.join(' | '));
}

async function init(){
  if(ready) return;
  if(initPromise) return initPromise;
  initPromise=(async()=>{
    send('progress',{value:2,message:'Starting Python scientific runtime'});
    const loaded = await loadRuntime();
    pyodide = loaded.instance;
    send('progress',{value:9,message:'Loading NumPy, pandas, SciPy, scikit-learn and XGBoost'});
    await pyodide.loadPackage(['numpy','pandas','scipy','scikit-learn','xgboost','tzdata']);
    send('progress',{value:40,message:'Loading FluxGapFill scientific engine'});
    for(const file of PYFILES){
      const response = await fetch('/py/'+file+'?v=20260922b',{cache:'no-store'});
      if(!response.ok) throw new Error(`Failed to load /py/${file} (${response.status})`);
      const text = await response.text();
      pyodide.FS.writeFile('/home/pyodide/'+file,text,{encoding:'utf8'});
    }
    pyodide.runPython("import sys; sys.path.insert(0, '/home/pyodide'); import browser_engine");
    ready=true;
    send('progress',{value:100,message:'Scientific runtime ready'});
    send('ready');
  })();
  try { await initPromise; }
  catch(err){ initPromise=null; throw err; }
}

function cleanName(name){return String(name||'input').replace(/[^A-Za-z0-9._-]/g,'_')}
async function writeUpload(file){
  if(!file) return null;
  const path='/tmp/'+Date.now()+'_'+cleanName(file.name);
  const buf = new Uint8Array(await file.arrayBuffer());
  pyodide.FS.writeFile(path,buf);
  return path;
}
async function callPy(expr){
  const result = await pyodide.runPythonAsync(expr);
  return JSON.parse(result);
}
self.onmessage=async (event)=>{
  const m=event.data||{};
  try{
    await init();
    if(m.cmd==='load'){
      const ec=await writeUpload(m.ecFile), ci=await writeUpload(m.cimisFile);
      const res=await callPy(`browser_engine.load_project(${JSON.stringify(ec)}, ${ci?JSON.stringify(ci):'None'}, ${Number(m.maximumQc??1)})`);
      send('loaded',{result:res});
    }else if(m.cmd==='benchmark'){
      const res=await callPy(`browser_engine.run_benchmark(${JSON.stringify(JSON.stringify(m.targets))}, ${JSON.stringify(JSON.stringify(m.durations))}, ${Number(m.windows||1)}, ${JSON.stringify(JSON.stringify(m.candidates))})`);
      send('benchmarkDone',{result:res});
    }else if(m.cmd==='fill'){
      const res=await callPy(`browser_engine.run_gap_fill(${JSON.stringify(JSON.stringify(m.targets))}, ${JSON.stringify(m.mode)}, ${JSON.stringify(m.fixedModel)}, ${Number(m.maxGapDays||90)})`);
      send('fillDone',{result:res});
    }else if(m.cmd==='export'){
      const csv=await pyodide.runPythonAsync(`browser_engine.export_csv(${m.compact?'True':'False'})`);
      send('exportReady',{csv,filename:m.compact?'fluxgapfill_results_compact.csv':'fluxgapfill_results_full.csv'});
    }else if(m.cmd==='exportBenchmark'){
      const csv=await pyodide.runPythonAsync('browser_engine.benchmark_csv()');
      send('exportReady',{csv,filename:'fluxgapfill_blocked_validation.csv'});
    }
  }catch(err){
    send('error',{message:String(err&&err.message||err),stack:String(err&&err.stack||'')});
  }
};

init().catch(err=>send('error',{message:'Scientific runtime failed to initialize: '+(err?.message||err),stack:err?.stack||''}));
