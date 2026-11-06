"""Refresh-safe drafts/catalog preferences, using fake browser storage and GET only."""
import json
from pathlib import Path
import re
import shutil
import subprocess
root=Path(__file__).parent
html=(root/'gumi_index.html').read_text()
prefs=re.search(r'<script id="gumi-preferences">(.*?)</script>',html,re.S).group(1)
catalog=(root/'model_catalog.js').read_text()
program='''
const vm=require('node:vm'),assert=require('node:assert/strict');
const trained='job_20261021_183757_4313c246',dual='job_20261021_183757_abcd1234';
const models=[{id:'qwen3_5_2b',name:'2B',arm:null},{id:trained,name:'own left',arm:'left'},{id:dual,name:'own dual',arm:'dual'}];
function browser(storage={},query='',broken=false){
 const requests=[];
 const make=value=>({value,dataset:{},handlers:{},disabled:false,addEventListener(kind,fn){this.handlers[kind]=fn;}});
 const controls={'v-task':make(''),'model-task':make(''),'model-arm':make('left'),'model-profile':make('qwen3_5_2b')};
 const select=controls['model-profile'];select.items=[];
 select.replaceChildren=function(...items){this.items=items;this.value=items[0]?.value||'';};
 Object.defineProperty(select,'selectedOptions',{get(){return this.items.filter(o=>o.value===this.value);}});
 const location=new URL('http://127.0.0.1:8620/'+query),window={};
 window.history={state:null,replaceState(_state,_unused,url){const next=new URL(url,location);location.href=next.href;}};
 const document={activeElement:null,getElementById:id=>controls[id],createElement:()=>({dataset:{}})};
 const localStorage={getItem:key=>{if(broken)throw Error('disabled');return storage[key]??null;},setItem:(key,value)=>{if(broken)throw Error('quota');storage[key]=value;}};
 const context=vm.createContext({document,window,localStorage,URL,URLSearchParams,location,setInterval:()=>{},fetch:async(path,options)=>{requests.push({path,options});return {ok:true,json:async()=>({ok:true,models})};}});
 vm.runInContext(PREFS,context);vm.runInContext(CATALOG,context);
 return {controls,window,requests,location,storage};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const storage={};let b=browser(storage);await settle();
 for(const [id,value] of [['v-task','采集水瓶任务'],['model-task','inference bottle task']]){b.controls[id].value=value;b.controls[id].handlers.input();}
 b.controls['model-profile'].value=trained;b.controls['model-profile'].handlers.change();
 b=browser(storage);await settle();
 assert.equal(b.controls['v-task'].value,'采集水瓶任务');assert.equal(b.controls['model-task'].value,'inference bottle task');assert.equal(b.controls['model-profile'].value,trained);
 b.window.gumiPrefs.renderTask({task_text:'old backend task',recording:false});assert.equal(b.controls['v-task'].value,'采集水瓶任务');
 b.controls['v-task'].value='';b.controls['v-task'].handlers.input();b=browser(storage);await settle();
 b.window.gumiPrefs.renderTask({task_text:'old task',recording:false});assert.equal(b.controls['v-task'].value,'');
 b.window.gumiPrefs.renderTask({task_text:'active recording task',recording:true});assert.equal(b.controls['v-task'].value,'active recording task');
 b=browser(storage,'?lora='+dual);await settle();assert.equal(b.controls['model-profile'].value,dual);assert.equal(b.controls['model-arm'].value,'dual');assert.equal(b.location.search,'');
 b=browser(storage);await settle();assert.equal(b.controls['model-profile'].value,dual);assert.equal(b.controls['model-arm'].value,'dual');
 assert(b.requests.every(r=>r.path==='/api/model/catalog'&&!r.options));
 const invalid={'gumi.preferences.v1':JSON.stringify({profile:'job_invalid',task:123,arm:'bad'})};b=browser(invalid);await settle();assert.equal(b.controls['model-profile'].value,'qwen3_5_2b');
 b=browser({'gumi.preferences.v1':'broken JSON'});await settle();assert.equal(b.controls['v-task'].value,'');
 b=browser({},'',true);await settle();b.controls['v-task'].value='still works';b.controls['v-task'].handlers.input();
 b.window.gumiPrefs.renderTask({recording:false,task_text:'must not overwrite'});assert.equal(b.controls['v-task'].value,'still works');
 console.log('Preferences JS passed: refresh, both tasks, empty drafts, server precedence, LoRA/arm restore, deep links, corrupt/disabled storage, no POST.');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
program=program.replace('PREFS',json.dumps(prefs)).replace('CATALOG',json.dumps(catalog))
subprocess.run([shutil.which('node') or 'node','-e',program],check=True)
