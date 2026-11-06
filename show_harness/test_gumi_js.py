"""Run with the machine-local js-check-venv (quickjs==1.19.4)."""
from pathlib import Path
import json
import quickjs

html=(Path(__file__).parent/'gumi_index.html').read_text()
script=html.split('<script>',1)[1].split('</script>',1)[0]
ctx=quickjs.Context()
ctx.eval('new Function('+json.dumps(script)+')')
start=script.index('/* ---- Continuous live lease:')
end=script.index('/* ---------------- compose-and-commit',start)
ctx.eval('''
let sent=[],interval=null,STATE={paused:false};
const events={},windowEvents={};
const elements={"live-mode":{checked:true,addEventListener:(name,fn)=>{events["mode:"+name]=fn;}}};
function $(id){return elements[id];}
function show(){} function clearQueue(){}
const document={hidden:false,addEventListener:(name,fn)=>{events[name]=fn;}};
const window={addEventListener:(name,fn)=>{windowEvents[name]=fn;}};
function setInterval(fn){interval=fn;return 1;}
function clearInterval(){interval=null;}
function fetch(path,options){sent.push(JSON.parse(options.body));return Promise.resolve({json:()=>Promise.resolve({ok:true})});}
''')
ctx.eval(script[start:end])
ctx.eval('livePress("KeyW","left","MV_UP");livePress("KeyU","right","MV_UP");interval();events.keyup({code:"KeyW"});events.keyup({code:"KeyU"});')
while ctx.execute_pending_job():pass
sent=json.loads(ctx.eval('JSON.stringify(sent)'))
assert sent[0]['left']=='MV_UP' and sent[0]['right']=='STILL'
assert sent[1]['left']==sent[1]['right']=='MV_UP'
assert sent[-2]['left']=='STILL' and sent[-2]['right']=='MV_UP'
assert sent[-1]['left']==sent[-1]['right']=='STILL'
assert ctx.eval('interval===null')
assert [s['sequence'] for s in sent]==sorted(set(s['sequence'] for s in sent))
ctx.eval('livePress("KeyW","left","MV_UP");windowEvents.blur();')
assert json.loads(ctx.eval('JSON.stringify(sent[sent.length-1])'))['left']=='STILL'
count=ctx.eval('sent.length')
ctx.eval('STATE.paused=true;livePress("KeyW","left","MV_UP");')
assert ctx.eval('sent.length')==count
print('JavaScript passed: syntax, dual hold, key release, heartbeat cleanup, blur stop, paused refusal.')

# Camera views keep at most one request in flight and pause while hidden.
views=quickjs.Context()
views.eval('''
let requests=[],timers=[],urls=0,revoked=0;
const document={hidden:false};
const performance={now:()=>0};
const URL={createObjectURL:()=>'blob:'+(++urls),revokeObjectURL:()=>{revoked++;}};
function setTimeout(fn){timers.push(fn);}
function fetch(path){return new Promise(resolve=>requests.push(()=>resolve({ok:true,blob:()=>Promise.resolve({})})));}
const img={hidden:false,dataset:{view:'/snapshot/agentview'},src:'',removeAttribute(){this.src=null;}};
''')
views.eval(script[script.index('function pollView'):script.index('for (const img of document.querySelectorAll("#views img")) pollView')])
views.eval('pollView(img);')
assert views.eval('requests.length')==1 and views.eval('timers.length')==0
views.eval('requests[0]();')
while views.execute_pending_job():pass
assert views.eval('img.src')=='blob:1' and views.eval('timers.length')==1
views.eval('img.hidden=true;timers[0]();')
while views.execute_pending_job():pass
assert views.eval('requests.length')==1 and views.eval('timers.length')==2
views.eval('img.hidden=false;timers[1]();requests[1]();')
while views.execute_pending_job():pass
assert views.eval('img.src')=='blob:2' and views.eval('revoked')==1
# A 503 (no fresh front source) blanks the view instead of freezing the last frame.
views.eval('fetch=()=>Promise.resolve({ok:false,arrayBuffer:()=>Promise.resolve()});timers[2]();')
while views.execute_pending_job():pass
assert views.eval('img.src') is None and views.eval('revoked')==2
print('JavaScript passed: camera views poll one frame at a time, skip while hidden, release old frames, blank a stale front.')

# Text entry, camera permission buttons and model submission must never reach teleop.
keyboard=script[script.index('function isEditingTarget'):script.index('/* ---------------- boot')]
editing=quickjs.Context()
editing.eval('''
let commands=0,focused=0,stops=0;const handlers={};
const document={addEventListener:(kind,fn)=>{handlers[kind]=fn;},body:{focus:()=>focused++}};
const SIDE_KEYS={ShiftLeft:['left','GRASP']},LEFT_KEYS={w:'MV_UP'},RIGHT_KEYS={},NO_REPEAT={};
const STATE={};let digit=0,cancelArmed=0;
function $(id){return {checked:true};}function helpOpen(){return false;}
function livePress(){commands++;}function queueAction(){commands++;}function commit(){commands++;}
function post(){commands++;}function show(){}function toggleHelp(){}function clearQueue(){}
function liveStop(){stops++;}
const input={isContentEditable:false,closest:()=>true,matches:()=>true};
const body={isContentEditable:false,closest:()=>false,matches:()=>false};
function event(target,key,code){return {target,key,code,preventDefault:()=>{}};}
''')
editing.eval(keyboard)
editing.eval('''
handlers.keydown(event(input,'w','KeyW'));
handlers.keydown(event(input,'Enter','Enter'));
handlers.keydown(event(input,'Shift','ShiftLeft'));
handlers.click({target:input});handlers.focusin({target:input});
''')
assert editing.eval('commands===0 && focused===0 && stops===1')
editing.eval("handlers.keydown(event(body,'w','KeyW'));handlers.click({target:body});")
assert editing.eval('commands===1 && focused===1')
print('JavaScript passed: task/camera/model focus blocks teleop, stops holds, keeps text focus; body keys still work.')
