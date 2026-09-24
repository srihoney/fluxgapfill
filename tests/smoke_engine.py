"""Native-Python regression check for import, QC, validation and reconstruction.
Requires numpy, pandas, scipy and scikit-learn; does not emulate Pyodide.
Run: python tests/smoke_engine.py
"""
import json
import sys
from pathlib import Path
import numpy as np
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'public/py'))
import browser_engine as engine
file=str(root/'public/examples/fluxnet_style_example.csv')
spec=json.loads(engine.inspect_input(file))
meta=json.loads(engine.load_universal_project(file,json.dumps(spec),project_json=json.dumps({'project_name':'Smoke check','timezone':'UTC','time_basis':'utc'})))
assert meta['records']>100
engine.STATE['phase4']={'obsolete':True}
qc=json.loads(engine.run_qc('{}'))
assert engine.STATE['phase4'] is None
original=engine.STATE['data']['LE'].copy()
validation=json.loads(engine.run_benchmark('["LE"]','[1]',1,'["RF_cross"]'))
assert validation
filled=json.loads(engine.run_gap_fill('["LE"]','fixed','RF_cross',7))
assert filled
csv=engine.export_csv(True)
assert 'LE' in csv and len(csv)>100
observed=original.notna()
# Export schema may retain the source and add a reconstruction column.
out=engine.STATE['filled']
assert np.allclose(out.loc[observed,'LE'],original[observed],equal_nan=True)
engine.STATE['phase4']={'obsolete':True}
engine.load_universal_project(file,json.dumps(spec))
assert engine.STATE['phase4'] is None
print('PASS: import, QC, blocked RF validation, filling, measured-value preservation, CSV export and derived-state invalidation')
