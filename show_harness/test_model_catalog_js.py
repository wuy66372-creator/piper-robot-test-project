"""Browser-free catalog UI behavior; no model or robot requests are made."""
import json
from pathlib import Path
import shutil
import subprocess
script=(Path(__file__).parent/'model_catalog.js').read_text()
setup='''
const assert=require('node:assert/strict');
let handler,poll;
const select={disabled:false,value:'qwen3_5_2b',items:[],addEventListener:(kind,fn)=>{handler=fn;},
 replaceChildren(...items){this.items=items;this.value=items[0].value;},
 get selectedOptions(){return this.items.filter(o=>o.value===this.value);}};
const arm={value:'left'};
const document={getElementById:id=>id==='model-profile'?select:arm,createElement:()=>({dataset:{}})};
const window={};const location={search:'?lora=job_20261021_180000_1234abcd'};
const data={ok:true,models:[{id:'qwen3_5_2b',name:'2B',arm:null},{id:'job_20261021_180000_1234abcd',name:'dual v4 <safe text>',arm:'dual'}]};
const fetch=async()=>({ok:true,json:async()=>data});
const setInterval=fn=>{poll=fn;};
'''
checks='''
setImmediate(async()=>{
 assert.equal(select.value,data.models[1].id);assert.equal(arm.value,'dual');
 assert.equal(select.items[1].textContent,'dual v4 <safe text>');
 select.value='qwen3_5_2b';handler();assert.equal(arm.value,'left');
 await poll();assert.equal(select.value,'qwen3_5_2b');
 data.models.push({id:'new-job',name:'new',arm:'right'});
 select.disabled=true;await poll();assert.equal(select.items.length,2);
 select.disabled=false;await poll();assert.equal(select.items.length,3);assert.equal(select.value,'qwen3_5_2b');
 select.value='new-job';handler();assert.equal(arm.value,'right');
 console.log('Model catalog JS passed: deep link, dual/right arm, safe text, stable selection and busy refresh.');
});
'''
subprocess.run([shutil.which('node') or 'node','-e',setup+'\n'+script+'\n'+checks],check=True)
