"""Node-only checks of training-start selection, without HTTP/GPU access."""
from pathlib import Path
import shutil
import subprocess
source=(Path(__file__).parent/'training.js').read_text()
start=source.index('  function syncInitialization(){');end=source.index('  function selection(){',start)
setup='''
const assert=require('node:assert/strict');
const upstreamOption={disabled:false};
const controls={base:{value:'qwen3_5_4b'},mode:{value:'left_v3'},initialization:{value:'upstream_lora',querySelector:()=>upstreamOption},rank:{value:'8',disabled:false},'initialization-note':{textContent:''}};
const $=id=>controls[id];
let state={upstream_adapters:[{profile:'qwen3_5_4b',rank:64,modes:['left_v3','right_v3']}]};
'''
checks='''
syncInitialization();assert.equal(controls.rank.value,'64');assert(controls.rank.disabled);assert(!upstreamOption.disabled);
controls.initialization.value='base';syncInitialization();assert(!controls.rank.disabled);assert.equal(controls.initialization.value,'base');
controls.initialization.value='upstream_lora';controls.mode.value='dual_v4';syncInitialization();assert(upstreamOption.disabled);assert.equal(controls.initialization.value,'base');assert(!controls.rank.disabled);
controls.mode.value='right_v3';syncInitialization();assert(!upstreamOption.disabled);
controls.base.value='qwen3_5_2b';syncInitialization();assert(upstreamOption.disabled);
console.log('Training initialization JS passed: upstream rank lock, explicit base choice, dual/missing compatibility.');
'''
subprocess.run([shutil.which('node') or 'node','-e',setup+source[start:end]+checks],check=True)
