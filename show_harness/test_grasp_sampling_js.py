"""Exercise the actual training form callback without network or training."""
from pathlib import Path
import shutil
import subprocess

source=(Path(__file__).parent/'training.js').read_text()
start=source.index("  $('training-form').addEventListener('submit'")
end=source.index('  refresh();setInterval',start)
setup='''
const assert=require('node:assert/strict');
let submit, captured, submitting=false, logId;
const controls={'training-form':{addEventListener:(name,fn)=>submit=fn},base:{value:'qwen3_5_4b'},mode:{value:'left_v3'},initialization:{value:'upstream_lora'},quantization:{value:'4'},epochs:{value:'3'},rank:{value:'64'},lr:{value:'0.00002'},'max-steps':{value:'-1'},'grasp-repeat':{value:'5'},'release-inference':{checked:true},'log-panel':{hidden:true}};
const $=id=>controls[id],selected=new Set(['rollout_001']);
const selection=()=>{},message=()=>{},refresh=async()=>{};
const api=async(path,body)=>{captured={path,body};return {message:'test',job:{id:'fake'}};};
'''
checks='''
(async()=>{
 assert.equal(captured,undefined);
 await submit({preventDefault(){}});
 assert.equal(captured.path,'/api/training/start');assert.equal(captured.body.grasp_repeat,5);
 assert.equal(captured.body.initialization,'upstream_lora');assert.deepEqual(captured.body.episodes,['rollout_001']);
 controls['grasp-repeat'].value='1';await submit({preventDefault(){}});assert.equal(captured.body.grasp_repeat,1);
 console.log('GRASP sampling form passed: default 5x, adjustable 1x, no automatic submission.');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
subprocess.run([shutil.which('node') or 'node','-e',setup+source[start:end]+checks],check=True)
