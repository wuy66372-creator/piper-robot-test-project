"""v4 once: three ordered cameras, exact LEFT RIGHT tokens, 20 mm physical moves."""
import json
from pathlib import Path
import secrets
import threading
import time
import numpy as np
from model_profiles import config_for,resolve

ROOT=Path(__file__).resolve().parents[1]
SIDES=('left','right')
ACTIONS={'MV_FWD','MV_BACK','MV_LEFT','MV_RIGHT','MV_UP','MV_DOWN','GRASP','RELEASE','STILL'}

def exact_pair(raw):
    parts=raw.strip().split()
    if len(parts)!=2 or raw.strip()!=' '.join(parts) or any(t not in ACTIONS|{'DONE'} for t in parts):
        raise ValueError('双臂模型必须只输出 LEFT RIGHT 两个精确动作token')
    if 'DONE' in parts and parts!=['DONE','DONE']:raise ValueError('仅接受双臂同时 DONE；拒绝混合完成/运动')
    return dict(zip(SIDES,parts))

class DualPolicy:
    def __init__(self,profile):
        from core.config import load_yaml
        from core.vlm.vlm_client import VLMClient
        from core.vlm.dual_mvtoken_roles import DualMvTokenController
        settings=load_yaml(Path(__file__).with_name('live_piper.yaml'))
        config=config_for(settings,profile,'dual')
        self.client=VLMClient(**{k:config[k] for k in ('base_url','model','api_key','max_tokens',
            'temperature','chat_template_kwargs','provider','max_retries')},timeout_s=8)
        self.client.health_check()
        prompt=(ROOT/'Show-Harness/prompts/v4/dual_mvtoken_once.txt').read_text()
        self.agent=DualMvTokenController(self.client,prompt,'once')
        self.base_prompt=prompt
        self.use_gripper_state=bool(config.get('gripper_state'))
    def decide(self,task,history,front,left,right,gripper_state=None):
        if self.use_gripper_state:
            from gripper_context import add_context
            if gripper_state is None:raise ValueError('Missing dual gripper observation')
            self.agent.prompt_template=add_context(self.base_prompt,gripper_state,dual=True)
        result=self.agent.decide(task,', '.join(history['left']) or 'none',
            ', '.join(history['right']) or 'none',front,left,right,debug=True)
        tokens=exact_pair(result.raw_text)
        if tokens!=result.tokens:raise ValueError('Recovered/prose tokens are not allowed for live motion')
        return dict(tokens=tokens,token=' '.join(tokens[s] for s in SIDES),raw_text=result.raw_text,
                    prompt=self.agent.last_prompt,media=self.agent.last_media)

def views(obs,settings):
    from core.record.images import prepare_view
    if not obs.get('front_camera_available'):raise RuntimeError('Front camera unavailable or stale')
    result=[]
    for name in ('agentview','wrist_left','wrist_right'):
        image=np.asarray(obs[name])
        if image.ndim!=3 or image.shape[2]!=3 or image.dtype!=np.uint8:raise RuntimeError('Invalid camera: '+name)
        size=settings['agentview_square_size'] if name=='agentview' else settings['wrist_square_size']
        result.append(prepare_view(image,square_size=size))
    return result

class DualRunMixin:
    def _start_dual(self,body):
        profile=body['profile'];resolve(profile,'dual')
        task=body.get('task','');owner=body.get('client');count=body.get('max_steps',1)
        if body.get('arm')!='dual':raise ValueError('双臂v4 LoRA必须选择双臂模式')
        if not isinstance(task,str) or not 1<=len(task.strip())<=1000:raise ValueError('Enter a task')
        if not isinstance(owner,str) or not 8<=len(owner)<=80 or type(count) is not int or not 1<=count<=150:
            raise ValueError('Invalid owner or step limit')
        with self.lock:
            b=self.backend
            if self.active or (self.thread and self.thread.is_alive()):raise RuntimeError('Previous inference is still finishing')
            if b.paused or b.busy or b.lease.current() or b.recorder.active:raise RuntimeError('Resume and stop manual controls/recording first')
            if not b.inference_lock.acquire(blocking=False):raise RuntimeError('Inference is still in progress')
            try:
                b.model_service.require(profile)
                obs=b.session.get_observation();views(obs,self.settings)
                for side in SIDES:
                    c=b.model_controllers[side];c.sync_from_robot()
                    if not c._joint_stream_ok or abs(c.step_m-.02)>1e-9:raise RuntimeError('Invalid 20 mm controller: '+side)
                    robot=b.session.robots[side]
                    with robot.mutex:
                        f=robot._feedback();f.healthy(time.monotonic(),enabled=True)
                        if robot._frame(0x2A1)[0]!=1:raise RuntimeError('Both arms must be enabled in CAN mode')
                        f.validate_target(f.joints())
                run_id=secrets.token_urlsafe(18)
                destination=self.output_root/(time.strftime('%Y%m%d-%H%M%S')+'-'+run_id[:6]);destination.mkdir(parents=True)
                meta=dict(arm='dual',task=task.strip(),profile=profile,model=profile+'_showharness_ft',
                    step_m=.02,prompt='v4/dual_mvtoken_once.txt',execution_token_swap=[],
                    automatic_home=False,initial_release=False,success=None,max_steps=count)
                meta['gripper_command_scope']='run'
                (destination/'run.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
                self.profile=profile;self.execution_mapping={};self.history={s:[] for s in SIDES}
                self.run_id,self.client=run_id,owner;self.deadline=self.clock()+self.heartbeat_ttl_s;self.active=True
                self.info=dict(active=True,run_id=run_id,phase='starting',arm='dual',task=task.strip(),
                    steps=0,max_steps=count,message='准备双臂v4推理',log_dir=str(destination))
                self.thread=threading.Thread(target=self._run_dual,args=(run_id,task.strip(),count,destination),daemon=True,name='gumi-dual-v4')
                self.thread.start()
                return dict(ok=True,run_id=run_id,message='双臂v4模型运行已启动',model_run=self.state())
            except BaseException:
                self.active=False;b.inference_lock.release();raise

    def _run_dual(self,run_id,task,count,destination):
        from core.record.images import save_png
        from gripper_context import ScopedCommands
        commands=ScopedCommands()
        try:
            policy=DualPolicy(self.profile)
            for index in range(count):
                with self.lock:
                    self.require_valid(run_id);self.info.update(phase='observing',message='读取主视角和双腕图像')
                observed=self.clock();obs=self.backend.session.get_observation();images=views(obs,self.settings)
                for name,image in zip(('agentview','wrist_left','wrist_right'),images):save_png(destination/f'{index:03d}-{name}.png',image)
                with self.lock:
                    self.require_valid(run_id);self.info.update(phase='inferring',message='双臂v4 once推理')
                gripper_state={s:commands.observe(s,obs[s]) for s in SIDES}
                kwargs={'gripper_state':gripper_state} if getattr(policy,'use_gripper_state',False) else {}
                decision=policy.decide(task,self.history,*images,**kwargs);tokens=exact_pair(decision['raw_text'])
                elapsed=self.clock()-observed
                before={s:np.asarray(obs[s]['ee_pose']).tolist() for s in SIDES}
                record=dict(decision,step=index,elapsed_s=elapsed,before_pose=before,front_camera=obs['front_camera'])
                record['observed_gripper_state']=gripper_state
                (destination/f'{index:03d}-decision.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
                with self.lock:
                    self.require_valid(run_id)
                    if elapsed+obs['front_camera']['age_upper_bound_s']>self.max_decision_age_s:raise RuntimeError('Stale inference; discarded pair')
                    self.info.update(raw_token=decision['token'],execution_token=decision['token'],elapsed_s=elapsed)
                if all(t=='DONE' for t in tokens.values()):
                    with self.lock:self.active=False;self.info.update(phase='done',message='双臂 DONE；请现场确认任务结果')
                    break
                payload=dict(run_id=run_id,arm='dual',tokens=tokens,observed=observed,
                    camera_age=obs['front_camera']['age_upper_bound_s'],before_pose=before)
                result=self.backend.submit('model_step',payload,timeout=30.)
                (destination/f'{index:03d}-execution.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                if not result.get('ok'):raise RuntimeError(result.get('message','Dual execution failed'))
                commands.completed(tokens)
                for side in SIDES:
                    if tokens[side].startswith('MV_'):
                        if np.linalg.norm(np.asarray(result['after_pose'][side])[:3]-np.asarray(before[side])[:3])<.001:
                            raise RuntimeError(side+': 实际位移不足1 mm，已停止双臂')
                        self.history[side].insert(0,tokens[side]);del self.history[side][5:]
                    elif tokens[side]=='STILL' and getattr(policy,'use_gripper_state',False):
                        # New state-aware contracts also match upstream dual training history.
                        self.history[side].insert(0,'STILL');del self.history[side][5:]
                with self.lock:
                    self.require_valid(run_id);self.info.update(steps=index+1,execution=result,phase='observing')
            else:
                with self.lock:self.active=False;self.info.update(phase='complete',message='已达到双臂步数上限')
        except Exception as exc:
            with self.lock:
                needs_hold=self.active
                if self.active:self.cancel(str(exc))
                self.info['error']=str(exc)
            if needs_hold:self.backend.submit('hold')
        finally:
            try:(destination/'summary.json').write_text(json.dumps(self.state(),ensure_ascii=False,indent=2)+'\n')
            finally:self.backend.inference_lock.release()

    def _execute_dual(self,payload):
        tokens=payload['tokens'];b=self.backend
        if set(tokens)!=set(SIDES) or any(t not in ACTIONS for t in tokens.values()):raise ValueError('Invalid dual action pair')
        if resolve(self.profile,'dual')['arm']!='dual':raise ValueError('A dual v4 profile is required')
        with self.lock:
            self.require_valid(payload['run_id'])
            if b.paused or self.clock()-payload['observed']+payload['camera_age']>self.max_decision_age_s:raise RuntimeError('Paused or stale inference')
            obs=b.session.get_observation();views(obs,self.settings)
            for side in SIDES:
                if np.max(np.abs(np.asarray(obs[side]['ee_pose'])-payload['before_pose'][side]))>.01:raise RuntimeError('Arm pose changed: '+side)
                robot=b.session.robots[side]
                with robot.mutex:
                    f=robot._feedback();f.healthy(time.monotonic(),enabled=True)
                    if robot._frame(0x2A1)[0]!=1:raise RuntimeError('CAN mode lost: '+side)
                    f.validate_target(f.joints())
                if abs(b.model_controllers[side].step_m-.02)>1e-9:raise RuntimeError('20 mm controller required')
            b.busy=True
            for robot in b.session.robots.values():robot.paused.clear();robot.motion_deadline=time.monotonic()+20.
            self.info.update(phase='executing',message='执行双臂动作对（20 mm）')
        errors=[];results={}
        def act(side):
            try:
                with self.lock:self.require_valid(payload['run_id'])
                if tokens[side]!='STILL':results[side]=b.model_controllers[side].step(tokens[side])
            except Exception as exc:
                errors.append(side+': '+str(exc))
                for robot in b.session.robots.values():robot.paused.set()
                for robot in b.session.robots.values():
                    try:robot.hold()
                    except Exception:pass
        try:
            threads=[threading.Thread(target=act,args=(s,)) for s in SIDES]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
            if errors:raise RuntimeError('; '.join(errors))
            with self.lock:self.require_valid(payload['run_id'])
            for side in SIDES:b.controllers[side].gripper_closed=b.model_controllers[side].gripper_closed
            b.last_pair=dict(tokens);b.history.append(dict(tokens));b.pairs_executed+=1;b._capture()
            return dict(ok=True,raw_tokens=tokens,execution_tokens=tokens,before_pose=payload['before_pose'],
                        after_pose={s:np.asarray(b.session.robots[s].get_ee_pose()).tolist() for s in SIDES},auto_released=False)
        finally:
            for robot in b.session.robots.values():robot.motion_deadline=float('inf')
            b.busy=False
