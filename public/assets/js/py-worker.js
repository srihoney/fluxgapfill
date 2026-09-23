let pyodide = null;
let ready = false;
const PYODIDE_BASE = 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/';
const PYFILES = ['data_processor.py','scientific_qc.py','calibration_utils.py','gap_filler.py','advanced_ml_gap_filler.py','external_sources.py','browser_engine.py'];

function send(type, payload={}){ self.postMessage({type, ...payload}); }
async function init(){
  if(ready) return;
  send('progress',{value:2,message:'Starting Python scientific runtime'});
  importScripts(PYODIDE_BASE+'pyodide.js');
  pyodide = await loadPyodide({indexURL:PYODIDE_BASE});
  send('progress',{value:8,message:'Loading NumPy, pandas, scikit-learn and XGBoost'});
  await pyodide.loadPackage(['numpy','pandas','scipy','scikit-learn','xgboost','tzdata']);
  for(const file of PYFILES){
    const text = await (await fetch('/py/'+file,{cache:'no-store'})).text();
    pyodide.FS.writeFile('/home/pyodide/'+file,text,{encoding:'utf8'});
  }
  pyodide.runPython("import sys; sys.path.insert(0, '/home/pyodide'); import browser_engine");
  ready=true;
  send('ready');
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
  }catch(err){ send('error',{message:String(err&&err.message||err),stack:String(err&&err.stack||'')}); }
};
init().catch(err=>send('error',{message:'Scientific runtime failed to initialize: '+err.message,stack:err.stack||''}));
