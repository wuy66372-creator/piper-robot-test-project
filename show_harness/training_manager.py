"""Bounded local LoRA jobs. Never accepts shell commands or external model paths."""
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from collection import write_json
from dataset_store import ROOT,JOBS

CACHE=Path.home()/'.cache/hackathon'
TRAIN_ENV=CACHE/'show-harness-train-venv'
BASES={'qwen3_5_0_8b':'Qwen3.5-0.8B','qwen3_5_2b':'Qwen3.5-2B','qwen3_5_4b':'Qwen3.5-4B'}
JOB_ID=re.compile(r'job_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}')
ACTIVE=('preparing','starting','training','cancelling')


def base_path(profile):
    if profile not in BASES:raise ValueError('Choose an installed Qwen base model')
    path=CACHE/'show-harness-models/huggingface/hub/Qwen'/BASES[profile]
    if not (path/'config.json').is_file():raise ValueError('Base weights are not installed')
    return path


def parameters(body):
    profile=body.get('profile','qwen3_5_0_8b')
    base_path(profile)
    mode=body.get('mode','left_v3')
    if mode not in ('left_v3','right_v3','dual_v4'):raise ValueError('Invalid prompt/arm contract')
    result=dict(profile=profile,mode=mode)
    grasp_repeat=body.get('grasp_repeat',5)
    if type(grasp_repeat) is not int or not 1<=grasp_repeat<=20:raise ValueError('GRASP采样倍数必须为1–20的整数')
    result['grasp_repeat']=grasp_repeat
    initialization=body.get('initialization','base')
    if initialization not in ('base','upstream_lora'):raise ValueError('Invalid training initialization')
    result['initialization']=initialization
    if initialization=='upstream_lora':
        from training_initialization import upstream
        if mode=='dual_v4':raise ValueError('本机上游LoRA为单臂v3；双臂v4请从基座训练，不静默混用提示格式')
        upstream(profile)
    for name,default,lo,hi in (('epochs',3.,.1,100.),('learning_rate',2e-5 if initialization=='upstream_lora' else 1e-4,1e-6,1e-3)):
        value=float(body.get(name,default))
        if not math.isfinite(value) or not lo<=value<=hi:raise ValueError('Invalid '+name)
        result[name]=value
    rank=body.get('rank',64 if initialization=='upstream_lora' else 8)
    if type(rank) is not int or rank not in (4,8,16,32,64):raise ValueError('Invalid LoRA rank')
    if initialization=='upstream_lora' and rank!=64:raise ValueError('继续上游LoRA必须保留原Rank 64和目标模块')
    steps=body.get('max_steps',-1)
    if type(steps) is not int or not (steps==-1 or 1<=steps<=100000):raise ValueError('Invalid max_steps')
    quant=body.get('quantization',4 if profile=='qwen3_5_4b' else 0)
    if quant not in (0,4) or type(quant) is not int:raise ValueError('Choose BF16 LoRA or 4-bit QLoRA')
    if profile=='qwen3_5_4b' and quant!=4:raise ValueError('16 GB GPU: 4B training requires 4-bit QLoRA')
    return dict(result,rank=rank,max_steps=steps,quantization=quant)


class TrainingManager:
    def __init__(self,backend,store,root=JOBS):
        self.backend,self.store,self.root=backend,store,Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.process=None
        self.active_id=None
        self.reserved=False
        self.restoring=False
        for p in self.root.glob('job_*/job.json'):
            try:
                job=json.loads(p.read_text())
                if job['status'] in ACTIVE:
                    job.update(status='interrupted',error='Web service restarted; inspect saved checkpoints')
                    write_json(p,job)
            except (OSError,ValueError,KeyError):continue

    def job_path(self,identifier):
        if not isinstance(identifier,str) or not JOB_ID.fullmatch(identifier):raise ValueError('Invalid job ID')
        path=(self.root/identifier).resolve()
        if path.parent!=self.root.resolve() or not path.is_dir():raise ValueError('Job not found')
        return path

    def jobs(self):
        result=[]
        for path in sorted(self.root.glob('job_*/job.json'),reverse=True):
            try:
                job=json.loads(path.read_text())
                progress=path.parent/'adapter/trainer_log.jsonl'
                if progress.is_file():
                    lines=progress.read_text().splitlines()
                    if lines:
                        try:job['progress']=json.loads(lines[-1])
                        except ValueError:pass
                result.append(job)
            except (OSError,ValueError):continue
        return result

    def state(self):
        from model_profiles import catalog
        from training_initialization import available
        return {'ok':True,'training_ready':(TRAIN_ENV/'training-ready.json').is_file(),
                'upstream_adapters':available(),
                'inference_models':catalog(),
                'models':[{'id':p,'name':n,'installed':(CACHE/'show-harness-models/huggingface/hub/Qwen'/n/'config.json').is_file()} for p,n in BASES.items()],
                'jobs':self.jobs(),'active_job':self.active_id,'reserved':self.reserved,'restoring_inference':self.restoring,
                'episodes':self.store.list(),'gpu_total_mib':16376}

    def start(self,body):
        with self.lock:
            if self.reserved:raise RuntimeError('A training job is already running')
            if not (TRAIN_ENV/'training-ready.json').is_file():raise RuntimeError('Training environment is not ready; inspect setup log')
            options=parameters(body)
            if body.get('release_inference') is not True:raise ValueError('Training needs exclusive GPU access; confirm inference unload')
            b=self.backend
            if b.model_service.state().get('phase')=='loading':raise RuntimeError('Wait for model switching to finish')
            if b.busy or b.recorder.active or b.model_run.active or b.lease.current():
                raise RuntimeError('Stop recording, model execution and held manual controls before training')
            if b.model_run.thread and b.model_run.thread.is_alive():raise RuntimeError('Wait for the previous model decision to finish')
            if not b.inference_lock.acquire(blocking=False):raise RuntimeError('Inference is still in progress')
            identifier='job_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
            path=self.root/identifier
            try:
                self.reserved=True;self.active_id=identifier
                b.paused=True
                for robot in b.session.robots.values():robot.paused.set()
                path.mkdir()
                initialization={'type':'base'}
                if options['initialization']=='upstream_lora':
                    from training_initialization import snapshot
                    initialization=snapshot(options['profile'],path/'source_adapter')
                selection=self.store.snapshot(body.get('episodes'),path/'episodes')
                job=dict(id=identifier,status='preparing',created=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                         options=options,episodes=[s['id'] for s in selection],
                         initialization=initialization,
                         restore_profile=b.model_service.state().get('profile'),
                         prompt_version='v4' if options['mode']=='dual_v4' else 'v3',
                         execution_token_swap=[] if options['mode']=='dual_v4' else ['MV_FWD','MV_BACK'],
                         step_m=.02,adapter_path=str(path/'adapter'))
                write_json(path/'job.json',job)
                log=(path/'train.log').open('ab')
                self.process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('training_worker.py')),str(path)],
                                               stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                log.close()
                threading.Thread(target=self._wait,args=(self.process,path),daemon=True).start()
                return {'ok':True,'job':job,'message':'训练已排队；推理会卸载，机械臂控制保持暂停'}
            except Exception:
                self.reserved=False;self.active_id=None;b.inference_lock.release()
                raise

    def _wait(self,process,path):
        try:
            code=process.wait()
            job=json.loads((path/'job.json').read_text())
            if job['status'] in ACTIVE:
                job.update(status='cancelled' if (path/'cancel.request').exists() else 'failed',error=f'Training worker exited {code}; see log')
                write_json(path/'job.json',job)
        finally:
            # Also restore after SIGTERM/SIGKILL; the worker cannot always run its finally block.
            try:
                job=json.loads((path/'job.json').read_text())
                profile=job.get('restore_profile')
                if profile:
                    from model_profiles import resolve
                    resolve(profile)
                    from model_switch import registered_models
                    self.restoring=True
                    with self.backend.model_service.lock:
                        self.backend.model_service.info={'phase':'loading','profile':profile,'message':'训练结束，正在恢复推理模型'}
                    restored=subprocess.run(['bash',str(ROOT/'show_harness/models.sh'),'start',profile],
                                            capture_output=True,text=True,timeout=35)
                    if restored.returncode:raise RuntimeError(restored.stderr or restored.stdout)
                    deadline=time.monotonic()+300
                    while time.monotonic()<deadline:
                        try:
                            if profile+'_showharness_ft' in registered_models():break
                        except Exception:pass
                        time.sleep(2)
                    else:raise RuntimeError('Inference restore timed out; load the model manually')
                    with self.backend.model_service.lock:
                        self.backend.model_service.info={'phase':'ready','profile':profile,'message':'推理模型已恢复；恢复控制后再操作'}
            except Exception as exc:
                with self.backend.model_service.lock:
                    self.backend.model_service.info={'phase':'error','profile':None,'message':'推理恢复失败：'+str(exc)}
            finally:
                with self.lock:
                    self.reserved=False;self.restoring=False;self.active_id=None;self.process=None
                    self.backend.inference_lock.release()

    def cancel(self,identifier):
        with self.lock:
            if identifier!=self.active_id or self.process is None:raise RuntimeError('This job is not running')
            path=self.job_path(identifier)
            (path/'cancel.request').touch()
            # Only the process group spawned by this manager can be signalled.
            process=self.process
            if process.poll() is not None:return {'ok':True,'message':'训练进程已结束'}
            os.killpg(process.pid,signal.SIGTERM)
            def terminate_later():
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if self.process is process and process.poll() is None:os.killpg(process.pid,signal.SIGKILL)
            threading.Thread(target=terminate_later,daemon=True).start()
            return {'ok':True,'message':'正在取消训练；已完成的检查点将保留'}

    def log(self,identifier):
        path=self.job_path(identifier)/'train.log'
        if not path.is_file():return ''
        with path.open('rb') as f:
            f.seek(0,2);size=f.tell();f.seek(max(0,size-48000))
            return f.read().decode('utf-8',errors='replace')

    def artifact(self,identifier):
        root=self.job_path(identifier)
        job=json.loads((root/'job.json').read_text())
        if job['status']!='completed':raise ValueError('Only completed adapters can be exported')
        destination=root/'adapter.zip'
        if not destination.exists():
            with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as output:
                for path in (root/'adapter').iterdir():
                    if path.is_file() and not path.is_symlink():output.write(path,path.name)
        return destination
