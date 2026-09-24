/* Workspace presentation and session lifecycle. No scientific calculations. */
function runtimeFailure(message){
 const panel=document.getElementById('runtimeNotice');panel.classList.remove('hidden');
 document.getElementById('runtimeNoticeText').textContent='The analysis components could not be downloaded. Check your internet connection, then retry. You can continue exploring the workspace.';console.error(message);
}
function clearDomainResults(){
 state.phase4=null;resetPhase3UI();
 document.querySelectorAll('.p4export,#exportPhase4Report').forEach(b=>b.disabled=true);
 for(const id of ['energyClosureChart','monthlyEbrChart','dailyEtP3Chart','cumulativeWaterChart','waterSwcChart','ustarChart','carbonChart','footprintChart']){const g=document.getElementById(id);if(g?.data)Plotly.purge(g)}
 for(const id of ['p4Ustar','p4NightN','p4E0','p4CarbonR2','p4FootN','p4Peak'])document.getElementById(id).textContent='—';
 document.getElementById('phase4Status').innerHTML='<div class="callout">Run an analysis to view current module diagnostics.</div>';
 document.getElementById('aoiBody').innerHTML='<tr><td colspan="3">Run footprint analysis to view contributions.</td></tr>';
}
function clearDerivedResults(){
 clearDomainResults();state.benchmark=null;state.filled=false;
 document.querySelectorAll('#exportCompact,#exportFull,[data-export-benchmark],#exportSummary,#exportSegments,#exportCalibration,#exportReport').forEach(b=>b.disabled=true);
 for(const id of ['benchmarkChart','filledChart','etChart']){const g=document.getElementById(id);if(g?.data)Plotly.purge(g)}
 for(const [id,n] of [['benchBody',9],['segmentBody',7],['seasonBody',9]])document.getElementById(id).innerHTML=`<tr><td colspan="${n}">Run validation to view current diagnostics.</td></tr>`;
 document.getElementById('bestRules').innerHTML='<div class="callout">Run validation to select methods for this dataset.</div>';
 document.getElementById('sourceCounts').innerHTML='<div class="muted">Run gap filling to view method provenance.</div>';
 refreshFigureOptions();
}
function invalidateWorkspace(){
 if(!state.loaded)return;
 clearDerivedResults();state.loaded=false;state.qcApplied=false;state.meta=null;state.primaryInspection=null;state.suppInspection=null;
 document.querySelectorAll('#exportRawBtn,#exportQcTop,#exportQcBottom,#applyQcBtn,#benchmarkBtn,#fillBtn,#refreshCapabilities').forEach(b=>b.disabled=true);
 for(const id of ['rawChart','qcChart']){const g=document.getElementById(id);if(g?.data)Plotly.purge(g)}
}
const gates={
 mapping:{ready:()=>!!state.primaryInspection,title:'Map your variables',text:'Inspect a primary file to review its timestamps, variable names and measurement units.',go:'setup',action:'Choose a dataset'},
 overview:{ready:()=>state.loaded,title:'Understand your data',text:'Prepare the mapped dataset to view coverage, missing observations and gap durations.',go:'mapping',action:'Review variable mapping'},
 qc:{ready:()=>state.loaded,title:'Review data quality',text:'Prepare a dataset before configuring physical limits and optional spike screening.',go:'mapping',action:'Review variable mapping'},
 modules:{ready:()=>state.loaded,title:'Check analysis requirements',text:'Load a dataset to see which analyses have the required variables and site metadata.',go:'setup',action:'Start a project'},
 models:{ready:()=>state.loaded,title:'Validate reconstruction methods',text:'Load and review your dataset before comparing methods on contiguous artificial gaps.',go:'setup',action:'Start a project'},
 results:{ready:()=>state.filled,title:'Your reconstruction results',text:'Run gap filling to explore the reconstructed series, method provenance and available uncertainty estimates.',go:'models',action:'Open validation & filling'},
 energywater:{ready:()=>state.loaded,title:'Explore energy and water exchange',text:'Load a dataset first. This optional analysis checks the inputs available for energy closure and evapotranspiration.',go:'setup',action:'Start a project'},
 phase4:{ready:()=>state.loaded,title:'Explore carbon exchange and footprint',text:'Load a dataset to check the requirements for turbulence screening, carbon partitioning and footprint analysis.',go:'setup',action:'Start a project'},
 export:{ready:()=>state.loaded,title:'Keep your analysis together',text:'Once a dataset is prepared, export numerical products, diagnostics and figures from this workspace.',go:'setup',action:'Start a project'}
};
function updateWorkspace(){
 for(const [id,g] of Object.entries(gates)){
  const view=document.getElementById(id);view.classList.toggle('is-empty',!g.ready());
 }
 document.querySelectorAll('#runPhase3Btn,#runPhase4Btn').forEach(b=>b.disabled=!state.loaded);
 const active=document.querySelector('.view.active')?.id||'setup';
 document.querySelectorAll('.nav-btn').forEach(b=>{
  if(b.dataset.view===active)b.setAttribute('aria-current','step');else b.removeAttribute('aria-current');
  const completed={setup:!!state.primaryInspection,mapping:state.loaded,overview:state.loaded,qc:state.qcApplied,models:!!state.benchmark&&state.filled,results:state.filled,energywater:!!state.phase3,phase4:!!state.phase4};
  b.querySelector('.nav-state').textContent=completed[b.dataset.view]?'✓':'';
 });
 const steps=['setup','qc','models','export'],group=['setup','mapping','overview'].includes(active)?'setup':active==='qc'?'qc':active==='export'?'export':'models';
 document.querySelectorAll('.workflow-strip button').forEach(b=>{b.classList.toggle('current',b.dataset.go===group);b.classList.toggle('complete',steps.indexOf(b.dataset.go)<steps.indexOf(group))});
 document.querySelectorAll('.chart').forEach(g=>g.classList.toggle('chart-empty',!g.data?.length));
 document.querySelectorAll('.chart-card-action').forEach(b=>b.disabled=!document.getElementById(b.dataset.figureJump)?.data?.length);
}
for(const [id,g] of Object.entries(gates)){
 const panel=document.createElement('div');panel.className='empty-state';
 panel.innerHTML=`<div class="empty-symbol" aria-hidden="true">↗</div><h3>${g.title}</h3><p>${g.text}</p><button class="btn btn-primary" data-go="${g.go}">${g.action}</button>`;
 document.getElementById(id).appendChild(panel);
}
document.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click',()=>showView(b.dataset.go)));
document.getElementById('retryRuntime').onclick=()=>{state.worker?.terminate();state.ready=false;document.getElementById('runtimeNotice').classList.add('hidden');initWorker()};
document.getElementById('demoBtn').onclick=async()=>{
 const b=document.getElementById('demoBtn');b.disabled=true;b.textContent='Loading example…';
 try{const r=await fetch('/examples/fluxnet_style_example.csv');if(!r.ok)throw new Error('Example file unavailable.');const file=new File([await r.blob()],'fluxnet_style_example.csv',{type:'text/csv'});setFile(file,'primary');document.getElementById('projectName').value='Example flux dataset';document.getElementById('siteName').value='Synthetic demonstration';toast('Example selected. Inspect the file when the analysis runtime is ready.');updateCoach();updateWorkspace()}
 catch(e){toast(e.message)}finally{b.disabled=false;b.textContent='Load example dataset'}
};
// File pickers remain keyboard accessible even though the native control is hidden.
document.querySelectorAll('.dropzone').forEach(zone=>{zone.tabIndex=0;zone.setAttribute('role','button');zone.setAttribute('aria-label',zone.id==='primaryDrop'?'Choose primary dataset':'Choose supplemental dataset');zone.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();zone.querySelector('input').click()}})});
// Keep focus in the guide dialog, and return it to the launching control.
let helpOpener=null;const baseOpenHelp=openHelp,baseCloseHelp=closeHelp;
openHelp=function(key){helpOpener=document.activeElement;baseOpenHelp(key);document.getElementById('helpClose').focus()};
closeHelp=function(){baseCloseHelp();helpOpener?.focus()};
document.getElementById('helpModal').addEventListener('keydown',e=>{if(e.key!=='Tab')return;const controls=[...e.currentTarget.querySelectorAll('button,a[href],select,input')],first=controls[0],last=controls.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}});
updateWorkspace();

document.getElementById("helpClose").addEventListener("click",()=>helpOpener?.focus());
