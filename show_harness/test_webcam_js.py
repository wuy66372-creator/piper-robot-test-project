"""Lifecycle tests with an explicit fake browser; no real camera permissions are changed."""
from pathlib import Path
import json
import quickjs

script = (Path(__file__).parent / 'webcam.js').read_text()
ctx = quickjs.Context()
ctx.eval('new Function(' + json.dumps(script) + ')')
ctx.eval('''
let pendingPermission,pendingBlob,stopped=0,sent=[],frameCallback=null,visibleEvents={},blobCalls=0;
const elements={};
function element(id){return elements[id]||(elements[id]={disabled:false,textContent:'',srcObject:null,
 addEventListener:(kind,fn)=>{elements[id][kind]=fn;},
 videoWidth:640,videoHeight:480,play:()=>Promise.resolve(),requestVideoFrameCallback:fn=>{frameCallback=fn;return 1;},
 cancelVideoFrameCallback:()=>{frameCallback=null;}});}
const document={hidden:false,getElementById:element,createElement:()=>({
 getContext:()=>({fillRect:()=>{},drawImage:()=>{}}),toBlob:fn=>{pendingBlob=fn;blobCalls++;}}),
 addEventListener:(kind,fn)=>{visibleEvents[kind]=fn;}};
const window={isSecureContext:true,addEventListener:(kind,fn)=>{visibleEvents[kind]=fn;}};
const crypto={randomUUID:()=>"fake-browser-uuid"};
const navigator={mediaDevices:{getUserMedia:options=>{
 if(options.audio!==false)throw Error('Microphone must not be opened');
 return new Promise(resolve=>{pendingPermission=resolve;});}}};
const AbortSignal={timeout:()=>null};
let failUpload=false,failStart=false,sessionCount=0;
function fetch(path,options){sent.push({path,body:options.body,headers:options.headers});
 if(failUpload&&path==='/api/webcam/frame')return Promise.reject({name:'TimeoutError',message:'signal timed out'});
 if(failStart&&path==='/api/webcam/start')return Promise.reject({name:'AbortError',message:'Fetch is aborted'});
 const token=path==='/api/webcam/start'?'test-token-'+(++sessionCount):options.headers['X-Webcam-Token'];
 return Promise.resolve({ok:true,
 json:()=>Promise.resolve({ok:true,token,ticket:'test-ticket'})});}
const fakeTrack={stop:()=>{stopped++;},addEventListener:()=>{}};
const fakeStream={getTracks:()=>[fakeTrack],getVideoTracks:()=>[fakeTrack]};
''')
ctx.eval(script)


def drain():
    while ctx.execute_pending_job(): pass


# Cancelling a pending permission cannot turn the camera on after the dialog resolves.
ctx.eval('elements["webcam-start"].click();elements["webcam-stop"].click();pendingPermission(fakeStream);')
drain()
assert ctx.eval('stopped') == 1
assert ctx.eval('sent.length') == 0
assert ctx.eval('elements["webcam-video"].srcObject === null')

# A successful permission starts one webcam-only session; pagehide revokes it and the track.
ctx.eval('elements["webcam-start"].click();pendingPermission(fakeStream);')
drain()
assert ctx.eval('elements["webcam-stop"].disabled') is False
assert ctx.eval('frameCallback !== null')
ctx.eval('frameCallback(0,{mediaTime:0});')
drain()
# One advancing video frame is uploaded; a pending JPEG is dropped after pagehide.
ctx.eval('frameCallback(100,{mediaTime:.1});pendingBlob({fakeJpeg:true});')
drain()
uploads=[s for s in json.loads(ctx.eval('JSON.stringify(sent)')) if s['path']=='/api/webcam/frame']
assert len(uploads)==1
assert uploads[0]['headers']['X-Webcam-Media-Time']=='.1' or uploads[0]['headers']['X-Webcam-Media-Time']=='0.1'
assert ctx.eval('elements["webcam-video"].hidden === false && elements["img-front"].hidden === true')

# A timed-out upload keeps the camera open, backs off, renews the ticket and sends a NEW frame.
ctx.eval('failUpload=true;frameCallback(200,{mediaTime:.15});pendingBlob({fakeJpeg:true});')
drain()
assert ctx.eval('stopped') == 1
assert '重试' in ctx.eval('elements["webcam-status"].textContent')
assert ctx.eval('elements["webcam-stop"].disabled') is False
ctx.eval('failUpload=false;pendingBlob=null;frameCallback(260,{mediaTime:.2});frameCallback(500,{mediaTime:.3});')
drain()
assert ctx.eval('pendingBlob === null')
starts=len([s for s in json.loads(ctx.eval('JSON.stringify(sent)')) if s['path']=='/api/webcam/start'])
ctx.eval('frameCallback(800,{mediaTime:.5});')
drain()
assert len([s for s in json.loads(ctx.eval('JSON.stringify(sent)')) if s['path']=='/api/webcam/start'])==starts+1
ctx.eval('frameCallback(950,{mediaTime:.6});pendingBlob({fakeJpeg:true});')
drain()
uploads=[s for s in json.loads(ctx.eval('JSON.stringify(sent)')) if s['path']=='/api/webcam/frame']
assert len(uploads)==3 and uploads[-1]['headers']['X-Webcam-Sequence']=='1'
assert uploads[-1]['headers']['X-Webcam-Media-Time'] in ('.6','0.6')
assert int(uploads[-1]['headers']['X-Webcam-Frame']) > int(uploads[0]['headers']['X-Webcam-Frame'])
assert '已上传' in ctx.eval('elements["webcam-status"].textContent')

ctx.eval('frameCallback(1100,{mediaTime:.7});')
ctx.eval('visibleEvents.pagehide();')
ctx.eval('pendingBlob({fakeJpeg:true});')
drain()
assert ctx.eval('stopped') == 2
assert ctx.eval('frameCallback === null')
assert json.loads(ctx.eval('JSON.stringify(sent)'))[-1]['path'] == '/api/webcam/stop'
assert len([s for s in json.loads(ctx.eval('JSON.stringify(sent)')) if s['path']=='/api/webcam/frame'])==3
assert ctx.eval('elements["webcam-start"].disabled') is False
assert ctx.eval('elements["webcam-video"].hidden === true && elements["img-front"].hidden === false')

# Initial session failure also retries; the user's camera permission and preview survive.
ctx.eval('failStart=true;elements["webcam-start"].click();pendingPermission(fakeStream);')
drain()
ctx.eval('frameCallback(2000,{mediaTime:0});')
drain()
assert '重试' in ctx.eval('elements["webcam-status"].textContent')
assert ctx.eval('stopped') == 2
assert ctx.eval('elements["webcam-video"].hidden') is False
ctx.eval('failStart=false;frameCallback(2100,{mediaTime:.1});frameCallback(2600,{mediaTime:.2});')
drain()
ctx.eval('frameCallback(2750,{mediaTime:.3});pendingBlob({fakeJpeg:true});')
drain()
assert '已上传' in ctx.eval('elements["webcam-status"].textContent')

# Discarding an upload ticket after timeout must not lose the token needed to revoke the source.
ctx.eval('failUpload=true;frameCallback(2900,{mediaTime:.4});pendingBlob({fakeJpeg:true});')
drain()
ctx.eval('elements["webcam-stop"].click();failUpload=false;')
drain()
sent = json.loads(ctx.eval('JSON.stringify(sent)'))
assert sent[-1]['path']=='/api/webcam/stop'
assert json.loads(sent[-1]['body'])['token']==sent[-2]['headers']['X-Webcam-Token']

# An old pending JPEG cannot release the new generation's upload mutex.
ctx.eval('elements["webcam-start"].click();pendingPermission(fakeStream);')
drain()
ctx.eval('frameCallback(3000,{mediaTime:0});')
drain()
ctx.eval('frameCallback(3100,{mediaTime:.1});const oldBlob=pendingBlob;elements["webcam-stop"].click();')
ctx.eval('elements["webcam-start"].click();pendingPermission(fakeStream);')
drain()
ctx.eval('frameCallback(3200,{mediaTime:0});')
drain()
ctx.eval('frameCallback(3300,{mediaTime:.1});const newBlob=pendingBlob;const countBeforeOld=blobCalls;oldBlob({oldJpeg:true});')
drain()
ctx.eval('frameCallback(3400,{mediaTime:.2});')
assert ctx.eval('blobCalls===countBeforeOld && pendingBlob===newBlob')
ctx.eval('newBlob({newJpeg:true});')
drain()
assert '已上传' in ctx.eval('elements["webcam-status"].textContent')
ctx.eval('elements["webcam-stop"].click();')
drain()
# A server-owned D435i is visible without permission and cannot be overwritten.
ctx.eval('window.updateFrontCameraState({source:"realsense_d435i",available:true,width:640,height:480,fps:15});')
assert ctx.eval('elements["webcam-start"].disabled && elements["webcam-stop"].disabled')
assert 'D435i' in ctx.eval('elements["webcam-status"].textContent')
ctx.eval('pendingPermission=null;elements["webcam-start"].click();visibleEvents.pagehide();')
assert ctx.eval('pendingPermission === null && elements["webcam-start"].disabled')
assert ctx.eval('elements["webcam-video"].hidden === true && elements["img-front"].hidden === false')
ctx.eval('window.updateFrontCameraState({source:"realsense_d435i",available:false});')
assert '等待相机恢复' in ctx.eval('elements["webcam-status"].textContent')
print('Webcam JS passed: browser lifecycle and host D435i permission/override isolation.')
