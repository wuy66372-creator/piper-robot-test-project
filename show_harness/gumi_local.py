"""Human-operated GUMI dual rig using local CAN and native wrist RGB streams."""
from pathlib import Path
import argparse
import json
import sys
import threading
import time
import math
import struct
import signal
from urllib.parse import urlparse,parse_qs

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'Show-Harness'),str(Path(__file__).parent)]
import cv2
import numpy as np
import yaml
from continuous import LiveLease
from browser_camera import BrowserFrontStore
from model_control import ModelRun, execution_mapping
from model_switch import ModelSwitch
from interpreters.real_atomic_controller import AtomicStepResult
from local_backend import LocalPiperInterface, LocalRGBCamera, install, GRIPPER_MAX_WIDTH_M
from interpreters.piper_atomic_controller import PiperAtomicController
from gumi.web_teleop_dual.dual_backend import DualTeleopBackend
from gumi.web_teleop_dual import server
from gumi.web_teleop.server import TeleopServer
from collection import RecordingMixin,DemonstrationRecorder
from dataset_store import DatasetStore,DATA
from training_manager import TrainingManager


def check_adoption_state(feedback):
    import piper_control as pc
    feedback.healthy(time.monotonic(),enabled=True)
    status=feedback.frames[0x2A1][0]
    if status[0] not in (0,1,2):raise RuntimeError('unsupported mode for in-place takeover')
    if status[0]==2 and status[3] not in (0,2):raise RuntimeError('finish button teaching before takeover')
    if any(cid in feedback.counts for cid in range(0x150,0x160)):
        raise RuntimeError('another controller is sending motion frames')


def takeover_in_place(robot,expected):
    with robot.mutex:
        f=robot._feedback();check_adoption_state(f)
        baseline=f.joints()
        if len(expected)!=6 or max(abs(a-b) for a,b in zip(baseline,expected))>200:
            raise RuntimeError('current joints differ from saved init pose by more than 0.2 degree')
        f.validate_target(baseline)
    time.sleep(.2)
    with robot.mutex:
        f=robot._feedback();check_adoption_state(f)
        if max(abs(a-b) for a,b in zip(f.joints(),baseline))>100:
            raise RuntimeError('arm moving/being dragged; takeover refused')
        before=f.snapshot()
        if f.frames[0x2A1][0][0]!=1:
            robot.sdk.MotionCtrl_2(0,1,0)
    # Every transition must be acknowledged by fresh feedback; no Reset/Enable fallback.
    time.sleep(.15)
    with robot.mutex:
        f=robot._feedback();f.healthy(time.monotonic(),enabled=True)
        mode=f.frames[0x2A1][0][0]
        if mode not in (0,1):raise RuntimeError('firmware rejected standby; no automatic Reset')
        if max(abs(a-b) for a,b in zip(f.joints(),baseline))>200:
            raise RuntimeError('pose moved during standby transition')
        current=f.joints();f.validate_target(current)
        robot.sdk.JointCtrl(*current)  # seed measured target before enabling CAN motion
        robot.sdk.MotionCtrl_2(1,1,0)
    time.sleep(.15)
    with robot.mutex:
        f=robot._feedback();f.healthy(time.monotonic(),enabled=True)
        if f.frames[0x2A1][0][0]!=1:raise RuntimeError('firmware rejected CAN takeover; no automatic Reset')
        if max(abs(a-b) for a,b in zip(f.joints(),baseline))>200:
            raise RuntimeError('pose moved during zero-speed takeover')
        current=f.joints();f.validate_target(current)
        robot.sdk.JointCtrl(*current);robot.sdk.MotionCtrl_2(1,1,5)
        robot.last_command=np.radians(np.array(current)/1000)
    time.sleep(.2)
    with robot.mutex:
        f=robot._feedback();f.healthy(time.monotonic(),enabled=True)
        if f.frames[0x2A1][0][0]!=1 or max(abs(a-b) for a,b in zip(f.joints(),baseline))>200:
            raise RuntimeError('in-place takeover did not preserve CAN mode/pose')
        return {'before':before,'after':f.snapshot(),'baseline_mdeg':baseline}


class TeleopRobot(LocalPiperInterface):
    allow_teaching_capture=False
    tracking_wait_s=2.
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.paused=threading.Event()

    def check_connect_mode(self,feedback):
        if self.allow_teaching_capture:
            check_adoption_state(feedback)
        else:
            # Connecting only reads state and queries limits. Keep the camera UI
            # available during button teaching; every motion path still requires
            # CAN mode. No mode switch is implied by starting the web service.
            feedback.healthy(time.monotonic())

    def stream_joints(self,positions):
        values=np.asarray(positions,dtype=float)
        if values.shape!=(6,) or not np.isfinite(values).all():
            raise ValueError('six finite joint angles in radians required')
        target=[round(math.degrees(v)*1000) for v in values]
        with self.mutex:
            f=self._feedback()
            f.validate_target(target)  # Validate the full IK solution before any subdivision.
            previous=f.joints() if self.last_command is None else [round(math.degrees(v)*1000) for v in self.last_command]
            delta=max(abs(a-b) for a,b in zip(target,previous))
        # Accept any hardware-valid IK waypoint for manual and model control.
        # Send <=1 degree intermediate targets, gated by measured tracking.
        count=max(1,math.ceil(delta/1000))
        origin=np.radians(np.array(previous)/1000)
        for index in range(1,count+1):
            self._stream_bounded_waypoint(origin+(values-origin)*index/count)

    def _stream_bounded_waypoint(self,positions):
        import piper_control as pc
        values=np.asarray(positions,dtype=float)
        if values.shape!=(6,) or not np.isfinite(values).all():
            raise ValueError('six finite joint angles in radians required')
        target=[round(math.degrees(v)*1000) for v in values]
        deadline=min(time.monotonic()+self.tracking_wait_s,
                     getattr(self,'motion_deadline',float('inf')))
        while True:
            with self.mutex:
                if self.paused.is_set():raise RuntimeError('Motion paused by operator')
                if not self.allow_motion:raise RuntimeError('read-only interface')
                if time.monotonic()>getattr(self,'motion_deadline',float('inf')):
                    raise RuntimeError('Model motion exceeded 20 second deadline')
                f=self._feedback();f.healthy(time.monotonic(),enabled=True)
                if self._frame(0x2A1)[0]!=1:raise RuntimeError('requires CAN control mode')
                pc.check_mode_transition(f)
                f.validate_target(target)
                measured=f.joints()
                previous=measured if self.last_command is None else [round(math.degrees(v)*1000) for v in self.last_command]
                if max(abs(a-b) for a,b in zip(target,previous))>1000:
                    raise RuntimeError('joint waypoint jump exceeds 1 degree')
                if max(abs(a-b) for a,b in zip(target,measured))<=2000:
                    self.sdk.JointCtrl(*target)
                    self.sdk.MotionCtrl_2(1,1,5)
                    self.last_command=values.copy()
                    return
                if time.monotonic()>=deadline:
                    raise RuntimeError('joint target exceeds 2 degree tracking lead after waiting; arm did not catch up')
            # Keep the previous bounded target. Release the CAN mutex so the
            # receiver and immediate operator hold can run while we wait.
            self.paused.wait(.02)

    def hold(self):
        import time
        import piper_control as pc
        self.paused.set()
        with self.mutex:
            f=self._feedback();f.healthy(time.monotonic(),enabled=True)
            pc.check_mode_transition(f)
            if self._frame(0x2A1)[0]!=1:
                raise RuntimeError('arm no longer in CAN control mode')
            current=f.joints();f.validate_target(current)
            # Position hold; never de-energize the arm or gripper.
            self.sdk.JointCtrl(*current)
            self.sdk.MotionCtrl_2(1,1,5)
            self.last_command=np.radians(np.array(current)/1000)
            data=self._frame(0x2A8)
            if data[6]&64 and not data[6]&63:
                self.sdk.GripperCtrl(struct.unpack('>i',data[:4])[0],500,1,0)

    def get_gripper_status(self):
        with self.mutex:
            data=self._frame(0x2A8)
            return {'width_m':struct.unpack('>i',data[:4])[0]/1e6,
                    'effort_nm':struct.unpack('>h',data[4:6])[0]/1000,
                    'enabled':bool(data[6]&64),'fault_bits':data[6]&63}

    def set_gripper_position(self,pos):
        import piper_control as pc
        if not math.isfinite(pos) or not 0<=pos<=GRIPPER_MAX_WIDTH_M:
            raise ValueError('gripper width must be finite and in 0..100 mm')
        if not self.allow_motion:raise RuntimeError('read-only interface')
        start=float(self.get_gripper_position()[0])
        count=max(1,math.ceil(abs(pos-start)/.001))
        for i in range(1,count+1):
            with self.mutex:
                if self.paused.is_set():raise RuntimeError('Gripper command interrupted')
                f=self._feedback();f.healthy(time.monotonic(),enabled=True)
                pc.check_mode_transition(f)
                status=self.get_gripper_status()
                if status['fault_bits']:raise RuntimeError('gripper driver fault; no automatic fault clear')
                if self._frame(0x2A1)[0]!=1:raise RuntimeError('requires CAN control mode')
                width=start+(pos-start)*i/count
                self.last_gripper_command='unknown'
                self.sdk.GripperCtrl(round(width*1e6),500,1,0)
            time.sleep(.05)
        deadline=time.monotonic()+2.
        while time.monotonic()<deadline:
            if self.paused.is_set():raise RuntimeError('Gripper command interrupted')
            status=self.get_gripper_status()
            if status['fault_bits']:raise RuntimeError('gripper driver fault')
            if not status['enabled']:raise RuntimeError('gripper did not enable')
            if abs(status['width_m']-pos)<.001:
                self.last_gripper_command='GRASP' if pos==0 else ('RELEASE' if pos==self.open_width_m else 'unknown')
                return
            # A close can stop on an object; report actual width without auto-opening.
            if pos==0 and abs(status['effort_nm'])>=.4:
                self.last_gripper_command='GRASP'
                return
            time.sleep(.05)
        raise RuntimeError('gripper command did not settle')

    def get_gripper_prev_cmd_success(self):
        return self.get_gripper_status()['fault_bits']==0


class TeleopController(PiperAtomicController):
    def step(self,token,*args,**kwargs):
        if token not in ('GRASP','RELEASE'):return super().step(token,*args,**kwargs)
        before=self.robot.get_ee_pose()
        self.robot.set_gripper_position(0. if token=='GRASP' else self.robot.open_width_m)
        self.gripper_closed=token=='GRASP'
        return AtomicStepResult(token=token,kind='gripper',pre_pose=before,
                                post_pose=self.robot.get_ee_pose(),gripper_closed=self.gripper_closed)


class WristRig:
    def __init__(self,front_source='browser',front_serial=None,front_depth=False):
        self.robots={};self.cameras={}
        if front_source=='realsense':
            from realsense_camera import RealSenseFrontStore
            self.front=RealSenseFrontStore(front_serial,depth_enabled=front_depth)
        else:
            self.front=BrowserFrontStore()
        self.placeholder=np.zeros((480,640,3),dtype=np.uint8)
        cv2.putText(self.placeholder,'NO FRONT CAMERA',(95,225),cv2.FONT_HERSHEY_SIMPLEX,1,(170,170,170),2)
        cv2.putText(self.placeholder,'Wrist-only human teleoperation',(65,270),cv2.FONT_HERSHEY_SIMPLEX,.7,(170,170,170),1)

    def connect(self):
        try:
            if hasattr(self.front,'connect'):self.front.connect()
            for side in ('left','right'):
                robot=TeleopRobot(side);self.robots[side]=robot;robot.connect()
                self.cameras[side]=LocalRGBCamera(f'/camera_{"l" if side=="left" else "r"}/color/image_raw')
            return self
        except BaseException:
            self.close();raise

    def get_observation(self):
        front,_,meta=self.front.frame()
        obs={'agentview':self.placeholder if front is None else front,
             'front_camera_available':meta['available'],'front_camera':meta}
        for side,robot in self.robots.items():
            ok,rgb,_=self.cameras[side].read()
            if not ok:raise RuntimeError(f'{side} wrist camera is stale/unavailable')
            obs['wrist_'+side]=rgb
            obs[side]={'ee_pose':robot.get_ee_pose(),'gripper_width':float(robot.get_gripper_position()[0])}
            obs[side]['last_gripper_command']=getattr(robot,'last_gripper_command','unknown')
        return obs

    def close(self):
        if hasattr(self.front,'close'):self.front.close()
        else:self.front.stop(self.front.token)
        for cam in self.cameras.values():cam.release()
        for robot in self.robots.values():robot.close()


class NoRecording:
    active=False;dir=None;steps=[]
    session_meta={'step_m':.003,'yaw_step_rad':.005,'front_camera_available':False}
    def add_step(self,*args):pass


class LocalBackend(RecordingMixin,DualTeleopBackend):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.busy=False
        self.paused=False
        self.lease=LiveLease()
        self.live_stop=threading.Event()
        self.live_thread=None;self.watch_thread=None
        self.inference_lock=threading.Lock()
        self.model_controllers={}
        self.model_run=ModelRun(self)
        self.model_service=ModelSwitch(self)

    def start(self):
        super().start()
        self.live_thread=threading.Thread(target=self._live_loop,daemon=True)
        self.watch_thread=threading.Thread(target=self._watch_loop,daemon=True)
        self.live_thread.start();self.watch_thread.start()

    def shutdown(self):
        self.model_run.cancel('服务正在关闭')
        self.live_stop.set();self.lease.cancel()
        for robot in self.session.robots.values():robot.paused.set()
        if self.live_thread:self.live_thread.join(timeout=2)
        if self.watch_thread:self.watch_thread.join(timeout=2)
        super().shutdown()

    def _interrupt(self):
        for robot in self.session.robots.values():robot.paused.set()
        for robot in self.session.robots.values():robot.hold()

    def _watch_loop(self):
        while not self.live_stop.wait(.02):
            self.model_run.watchdog()
            with self.lease.lock:
                if self.lease.expire():
                    if self.recorder.active:self.paused=True
                    try:self._interrupt()
                    except Exception as exc:self.paused=True;self.last_message=str(exc)

    def _live_loop(self):
        while not self.live_stop.wait(.02):
            current=self.lease.current()
            if current and not self.paused:
                generation,pair=current
                result=super().submit('live_tick',(generation,pair))
                if not result.get('ok'):
                    with self.lease.lock:
                        if self.lease.generation!=generation:continue
                        self.paused=True;self.last_message=result.get('message','continuous control stopped')
                        self.lease.cancel()
                        try:self._interrupt()
                        except Exception as exc:self.last_message=str(exc)

    def _execute(self,kind,arg):
        training=getattr(self,'training',None)
        if training and training.reserved:
            return {'ok':False,'status':409,'message':'LoRA训练中，机械臂控制保持暂停'}
        if self.recorder.active and kind in ('model_start','can_takeover'):
            return {'ok':False,'status':409,'message':'请先结束采集，再切换模型或CAN状态'}
        if kind=='record_save':return self._finish_recording(bool(arg.get('success')))
        if self.model_service.info['phase']=='loading' and kind not in ('model_step',):
            return {'ok':False,'status':409,'message':'模型正在加载，请等待完成'}
        if kind=='can_takeover':return self._cmd_can_takeover(arg)
        if kind=='model_start':return self.model_run.start(arg)
        if kind=='model_step':return self.model_run.execute(arg)
        if self.model_run.active and kind in ('steps','cancel','task','start','resume'):
            return {'ok':False,'status':409,'message':'模型运行中；先暂停再切换手动控制'}
        if kind=='live_tick':
            generation,pair=arg
            if not self.lease.valid(generation):return {'ok':False,'message':'continuous command expired/cancelled'}
            return self._cmd_steps([pair],live=True,generation=generation)
        if kind=='steps' and self.lease.current():
            return {'ok':False,'status':409,'message':'Release continuous controls before COMMIT'}
        return super()._execute(kind,arg)

    def _cmd_can_takeover(self,body):
        side=body.get('arm')
        if side not in ('left','right'):raise ValueError('Choose left or right arm')
        if (self.busy or self.lease.current() or self.model_run.active
                or (self.model_run.thread and self.model_run.thread.is_alive())):
            return {'ok':False,'status':409,'message':'请先停止模型和持续控制，再切回 CAN'}
        robot=self.session.robots[side]
        with robot.mutex:
            feedback=robot._feedback();check_adoption_state(feedback)
            expected=feedback.joints();feedback.validate_target(expected)
        self.busy=True;self.paused=True
        for arm in self.session.robots.values():arm.paused.set()
        try:
            result=takeover_in_place(robot,expected)
            self.controllers[side].sync_from_robot()
            if not self.controllers[side]._joint_stream_ok:
                raise RuntimeError('CAN 已接管，但 IK 验证失败，保持暂停')
            self._capture()
            destination=ROOT/'results/show_harness/can-takeover'/time.strftime('%Y%m%d-%H%M%S')/(side+'-web.json')
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_text(json.dumps(result,indent=2)+'\n')
            self.last_message=f'{"左" if side=="left" else "右"}臂已原位切回 CAN，点击恢复控制后操作'
            return {'ok':True,'message':self.last_message,'arm':side,'takeover':result}
        finally:self.busy=False

    def continuous(self,body):
        pair=(body.get('left','STILL'),body.get('right','STILL'))
        tokens=None if pair==('STILL','STILL') else pair
        if tokens and (getattr(self,'training',None) and self.training.reserved):
            raise RuntimeError('训练时禁用持续控制')
        if tokens and self.recorder.active and any(t not in ('STILL','MV_FWD','MV_BACK','MV_LEFT','MV_RIGHT','MV_UP','MV_DOWN') for t in tokens):
            raise ValueError('采集持续控制仅接受2 cm平移动作')
        if self.model_run.active:
            if tokens:raise RuntimeError('模型运行中；先暂停再手动控制')
            return {'ok':True,'message':'Manual controls released'}
        with self.lease.lock:
            if self.paused and tokens is not None:raise RuntimeError('Paused: resume first')
            changed=self.lease.update(body.get('client'),body.get('sequence'),tokens)
            # Recording uses atomic inference-aligned 20 mm steps. A normal
            # release/direction change revokes the NEXT tick, not half a label.
            # HOLD and heartbeat expiry still interrupt the current movement.
            if changed and not self.recorder.active:self._interrupt()
        return {'ok':True,'message':'continuous control active' if self.lease.current() else 'continuous control released'}

    def _cmd_start(self):
        return super()._cmd_start()

    def model_preview(self,body):
        if self.recorder.active:raise RuntimeError('请先结束采集再进行模型推理')
        # Read observations and call the VLM directly; no command queue/controller step.
        if not self.inference_lock.acquire(blocking=False):
            raise RuntimeError('Model inference already in progress')
        try:
            self.model_service.require(body.get('profile','qwen3_5_2b'))
            from model_profiles import resolve
            if resolve(body.get('profile','qwen3_5_2b'))['arm']=='dual':
                from dual_model_control import DualPolicy,views
                resolve(body['profile'],body.get('arm'))
                if body.get('arm')!='dual':raise ValueError('双臂v4 LoRA必须选择双臂模式')
                task=body.get('task','')
                if not isinstance(task,str) or not 1<=len(task.strip())<=1000:raise ValueError('Enter a task')
                obs=self.session.get_observation()
                images=views(obs,self.model_run.settings)
                policy=DualPolicy(body['profile']);policy.client.timeout_s=120;started=time.monotonic()
                from gripper_context import ScopedCommands
                commands=ScopedCommands()
                kwargs={'gripper_state':{s:commands.observe(s,obs[s]) for s in ('left','right')}} if getattr(policy,'use_gripper_state',False) else {}
                decision=policy.decide(task.strip(),{'left':[],'right':[]},*images,**kwargs)
                return dict(ok=True,inference_only=True,arm='dual',model=body['profile']+'_showharness_ft',
                    token=decision['token'],execution_token=decision['token'],raw_text=decision['raw_text'],
                    elapsed_s=time.monotonic()-started,input_front_camera=obs['front_camera'],
                    observation_synchronized=False,message='双臂v4三视角预览，未发送运动或夹爪命令')
            from core.config import load_yaml,resolve_vlm_config
            from core.record.images import prepare_view
            from core.vlm.mvtoken_roles import MvTokenController
            from core.vlm.vlm_client import VLMClient
            side=body.get('arm','left')
            if side not in ('left','right'):raise ValueError('Choose left or right arm')
            task=body.get('task','')
            if not isinstance(task,str) or not 1<=len(task.strip())<=1000:
                raise ValueError('Enter a task, at most 1000 characters')
            from model_profiles import config_for
            config=config_for(load_yaml(Path(__file__).parent/'vlm_local.yaml'),body.get('profile','qwen3_5_2b'),side)
            mapping=execution_mapping(config)
            client=VLMClient(**{key:config[key] for key in ('base_url','model','api_key','timeout_s',
                'max_tokens','temperature','chat_template_kwargs','provider','max_retries')})
            client.health_check()
            obs=self.session.get_observation()
            if not obs['front_camera_available']:raise RuntimeError('Front camera is unavailable or stale')
            front=prepare_view(obs['agentview'],square_size=256)
            wrist=prepare_view(obs['wrist_'+side],square_size=256)
            prompt=(ROOT/'Show-Harness/prompts/v3/mvtoken_generator_lite.txt').read_text()
            if config.get('gripper_state'):
                from gripper_context import ScopedCommands,add_context
                prompt=add_context(prompt,ScopedCommands().observe(side,obs[side]))
            agent=MvTokenController(client,prompt)
            started=time.monotonic()
            result=agent.decide(task.strip(),'unknown','none',front,wrist,debug=True)
            if result.raw_text.strip() not in agent.allowed_tokens:
                raise RuntimeError('Model did not emit a single exact action token')
            return {'ok':True,'inference_only':True,'arm':side,'model':config['model'],
                    'token':result.token,'execution_token':mapping.get(result.token,result.token),
                    'raw_text':result.raw_text,'elapsed_s':time.monotonic()-started,
                    'input_front_camera':obs['front_camera'],'observation_synchronized':False,
                    'message':'仅推理预览，未发送机械臂或夹爪命令'}
        finally:self.inference_lock.release()

    def _cmd_steps(self,pairs,live=False,generation=None):
        if self.model_run.active:
            return {'ok':False,'status':409,'message':'Model run owns the arm; pause first'}
        if self.paused:
            return {'ok':False,'status':409,'message':'Paused: click RESUME before commanding another step'}
        if len(pairs)!=1:
            return {'ok':False,'status':400,'message':'Only one pair per COMMIT is enabled'}
        # Require live images/states at the time the operator command is executed.
        self._capture()
        with self.lease.lock:
            if self.paused:return {'ok':False,'message':'Paused by operator'}
            if live and not self.lease.valid(generation):return {'ok':False,'message':'continuous command expired'}
            self.busy=True
            for robot in self.session.robots.values():robot.paused.clear()
        try:
            for controller in self.controllers.values():controller.sync_from_robot()
            return super()._cmd_steps(pairs)
        finally:self.busy=False

    def submit(self,kind,arg=None,timeout=60.):
        if kind=='hold':
            self.model_run.cancel()
            with self.lease.lock:
                self.lease.cancel()
                self.paused=True
            for robot in self.session.robots.values():robot.paused.set()
            errors=[]
            for side,robot in self.session.robots.items():
                try:robot.hold()
                except Exception as exc:errors.append(f'{side}: {exc}')
            return {'ok':not errors,'message':'Motion paused; position hold requested'+('; '.join(errors) if errors else '')}
        if kind=='resume':
            if getattr(self,'training',None) and self.training.reserved:return {'ok':False,'message':'训练中不能恢复控制'}
            if self.model_service.state()['phase']=='loading':return {'ok':False,'message':'Wait for model loading to finish'}
            if self.model_run.active:return {'ok':False,'message':'Pause the model run first'}
            if self.busy:return {'ok':False,'message':'Wait for the interrupted step to finish'}
            for controller in self.controllers.values():controller.sync_from_robot()
            for robot in self.session.robots.values():robot.paused.clear()
            self.paused=False
            return {'ok':True,'message':'Ready for a new COMMIT'}
        return super().submit(kind,arg,timeout)

    def state(self):
        state=super().state()
        # Arm poses are in different base frames; their raw distance is not a physical gap.
        front=self.session.front.state()
        state.update(gap_m=None,gap_warn=False,paused=self.paused,front_camera_available=front['available'],front_camera=front,
                     model_run=self.model_run.state(),model_service=self.model_service.state(),
                     recording_available=True,gripper_control_available=True,continuous_active=self.lease.current() is not None,
                     gripper_open_width_m=GRIPPER_MAX_WIDTH_M,
                     training_reserved=bool(getattr(self,'training',None) and self.training.reserved),
                     joint_envelope_deg=None,joint_waypoint_max_delta_deg=None,joint_tracking_lead_deg=2,
                     step_m=.02 if self.recorder.active else .003,yaw_step_rad=.005)
        return state


def handler(backend):
    Base=server.build_handler(backend)
    class Handler(Base):
        def do_GET(self):
            path=urlparse(self.path).path
            query=parse_qs(urlparse(self.path).query)
            if path=='/api/model/catalog':
                from model_profiles import catalog
                self._send_json({'ok':True,'models':catalog()});return
            if path in ('/training','/training.html'):
                self._send_file(Path(__file__).parent/'training.html','text/html; charset=utf-8');return
            if path=='/training.js':
                self._send_file(Path(__file__).parent/'training.js','text/javascript; charset=utf-8');return
            if path.startswith('/api/training/'):
                try:
                    if path=='/api/training/state':result=backend.training.state()
                    elif path=='/api/training/episode':result={'ok':True,'episode':backend.datasets.detail(query.get('id',[''])[0])}
                    elif path=='/api/training/log':result={'ok':True,'log':backend.training.log(query.get('id',[''])[0])}
                    elif path=='/api/training/image':
                        file=backend.datasets.image(query.get('id',[''])[0],query.get('view',[''])[0],query.get('step',['0'])[0])
                        self._send_file(file,'image/png');return
                    elif path=='/api/training/artifact':
                        self._send_file(backend.training.artifact(query.get('id',[''])[0]),'application/zip');return
                    else:raise ValueError('Unknown training endpoint')
                    self._send_json(result)
                except (ValueError,OSError) as exc:self._send_json({'ok':False,'message':str(exc)},400)
                return
            if path in ('/','/index.html'):
                self._send_file(Path(__file__).parent/'gumi_index.html','text/html; charset=utf-8');return
            if path=='/webcam.js':
                self._send_file(Path(__file__).parent/'webcam.js','text/javascript; charset=utf-8');return
            if path=='/model_preview.js':
                self._send_file(Path(__file__).parent/'model_preview.js','text/javascript; charset=utf-8');return
            if path=='/model_catalog.js':
                self._send_file(Path(__file__).parent/'model_catalog.js','text/javascript; charset=utf-8');return
            if path in ('/api/webcam/frame','/snapshot/agentview'):
                _,jpeg,meta=backend.session.front.frame()
                if jpeg is None:
                    self._send_json({'ok':False,'message':'No fresh front camera'},503);return
                self.send_response(200)
                self.send_header('Content-Type','image/jpeg')
                self.send_header('Content-Length',str(len(jpeg)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Frame-Sequence',str(meta['sequence']))
                self.send_header('X-Frame-Age-Upper-Bound-S',str(meta['age_upper_bound_s']))
                self.end_headers();self.wfile.write(jpeg);return
            super().do_GET()
        def do_POST(self):
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc!=self.headers.get('Host'):
                self.close_connection=True  # unread POST body must not corrupt the next request
                self._send_json({'ok':False,'message':'Cross-origin control denied'},403);return
            path=urlparse(self.path).path
            if path in ('/api/record/save','/api/training/start','/api/training/cancel','/api/training/episode'):
                body=self._read_json_body()
                if body is None:return
                try:
                    if path=='/api/record/save':
                        if type(body.get('success')) is not bool:raise ValueError('success must be boolean')
                        result=backend.submit('record_save',body)
                    elif path=='/api/training/start':result=backend.training.start(body)
                    elif path=='/api/training/cancel':result=backend.training.cancel(body.get('id'))
                    else:result={'ok':True,'episode':backend.datasets.update(body.get('id'),body)}
                    result['state']=backend.state()
                    self._send_json(result,200 if result.get('ok') else 409)
                except ValueError as exc:self._send_json({'ok':False,'message':str(exc)},400)
                except (RuntimeError,OSError) as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            if path=='/api/model/switch':
                body=self._read_json_body()
                if body is None:return
                try:
                    if backend.recorder.active or (getattr(backend,'training',None) and backend.training.reserved):raise RuntimeError('采集/训练时不能切换推理模型')
                    self._send_json(backend.model_service.start(body.get('profile')))
                except ValueError as exc:self._send_json({'ok':False,'message':str(exc)},400)
                except Exception as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            if path=='/api/can/takeover':
                body=self._read_json_body()
                if body is None:return
                try:
                    result=backend.submit('can_takeover',body,timeout=10.)
                    result['state']=backend.state()
                    self._send_json(result,200 if result.get('ok') else 409)
                except Exception as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            if path in ('/api/model/start','/api/model/heartbeat','/api/model/stop'):
                body=self._read_json_body()
                if body is None:return
                try:
                    if path.endswith('/start'):result=backend.submit('model_start',body,timeout=15.)
                    elif path.endswith('/heartbeat'):result=backend.model_run.heartbeat(body)
                    else:
                        with backend.model_run.lock:
                            if body.get('run_id') and body['run_id']!=backend.model_run.run_id:
                                result={'ok':True,'message':'Previous model run is already stopped'}
                            else:result=backend.submit('hold')
                    self._send_json(result,200 if result.get('ok') else 409)
                except ValueError as exc:self._send_json({'ok':False,'message':str(exc)},400)
                except Exception as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            if path=='/api/model/preview':
                body=self._read_json_body()
                if body is None:return
                try:self._send_json(backend.model_preview(body))
                except ValueError as exc:self._send_json({'ok':False,'message':str(exc)},400)
                except Exception as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            if path=='/api/webcam/frame':
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=backend.session.front.max_bytes:
                        self.close_connection=True
                        self._send_json({'ok':False,'message':'Webcam payload must be 1..800000 bytes'},413);return
                    if self.headers.get('Content-Type')!='image/jpeg':
                        self.close_connection=True
                        self._send_json({'ok':False,'message':'JPEG required'},415);return
                    result=backend.session.front.put(self.rfile.read(length),
                        self.headers.get('X-Webcam-Token'),self.headers.get('X-Webcam-Ticket'),
                        int(self.headers.get('X-Webcam-Sequence','-1')),
                        float(self.headers.get('X-Webcam-Media-Time','nan')),
                        int(self.headers['X-Webcam-Frame']) if 'X-Webcam-Frame' in self.headers else None)
                    self._send_json(result)
                except ValueError as exc:self._send_json({'ok':False,'message':str(exc)},400)
                except RuntimeError as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            if path in ('/api/webcam/start','/api/webcam/stop'):
                body=self._read_json_body()
                if body is None:return
                try:
                    result=(backend.session.front.start(body.get('client')) if path.endswith('/start')
                            else {'ok':True,'stopped':backend.session.front.stop(body.get('token'))})
                    self._send_json(result)
                except ValueError as exc:self._send_json({'ok':False,'message':str(exc)},400)
                except RuntimeError as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            if path=='/api/continuous':
                body=self._read_json_body()
                if body is None:return
                try:self._send_json(backend.continuous(body),200)
                except Exception as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            if path in ('/api/hold','/api/resume'):
                try:
                    result=backend.submit(path.split('/')[-1]);result['state']=backend.state()
                    self._send_json(result,200 if result['ok'] else 409)
                except Exception as exc:self._send_json({'ok':False,'message':str(exc)},409)
                return
            super().do_POST()
    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8620)
    parser.add_argument('--host',default='127.0.0.1',help='use 0.0.0.0 for LAN access')
    parser.add_argument('--front-source',choices=('realsense','browser'),default='realsense')
    parser.add_argument('--front-serial',help='D435i serial number (auto-detect one camera if omitted)')
    parser.add_argument('--front-depth',action='store_true',help='also capture depth aligned to color; model uses RGB')
    parser.add_argument('--floor-left',type=float,default=.05)
    parser.add_argument('--floor-right',type=float,default=.05)
    parser.add_argument('--adopt-init',action='store_true',help='explicit in-place takeover from ended teaching using saved init pose')
    args=parser.parse_args()
    install(allow_motion=True)
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    TeleopRobot.allow_teaching_capture=args.adopt_init
    rig=WristRig(args.front_source,args.front_serial,args.front_depth).connect()
    backend=None
    try:
        if args.adopt_init:
            saved=json.loads((Path(__file__).parent/'init_pose.json').read_text())
            adoption={}
            for side,robot in rig.robots.items():
                expected=[round(v*1000) for v in saved['arms'][side]['joints_deg']]
                adoption[side]=takeover_in_place(robot,expected)
            destination=ROOT/'results/show_harness/init-pose/takeover.json'
            destination.write_text(json.dumps(adoption,indent=2)+'\n')
        cfg=yaml.safe_load((ROOT/'Show-Harness/configs/primitives_piper.yaml').read_text())
        from core.config import load_yaml
        model_cfg=load_yaml(Path(__file__).with_name('live_piper.yaml'))
        controllers={}
        for side,robot in rig.robots.items():
            # Apply the configured per-arm Cartesian floor; both defaults are 0.05 m.
            controller=TeleopController.from_primitives_config(
                robot,cfg,step_m=.003,yaw_step_rad=.005,motion_frame='wrist',
                level_orientation=False,z_floor_m=(getattr(args,'floor_'+side) if getattr(args,'floor_'+side) is not None else model_cfg['arms'][side]['z_floor_m']),
                settle_steps=4,settle_dt_s=.05,verbose=False)
            controller.sync_from_robot()
            if not controller._joint_stream_ok:raise RuntimeError(f'{side}: IK model validation failed')
            controllers[side]=controller
        datasets=DatasetStore()
        datasets.recover_interrupted()
        recorder=DemonstrationRecorder(DATA)
        backend=LocalBackend(rig,controllers,recorder,home_fn=None,
            task_text='',
            require_task_done=False,capture_fps=15.)
        backend.datasets=datasets
        datasets.active_id=lambda:getattr(recorder.dir,'name',None)
        backend.training=TrainingManager(backend,datasets)
        from plugins.smooth import SmoothPlugin
        for side,robot in rig.robots.items():
            # Model control follows the upstream Piper FT token scale and interpolation.
            # Share robot transports; the model applies its configured mapping at execution.
            # Manual controls retain their direct primitive directions.
            backend.model_controllers[side]=TeleopController.from_primitives_config(
                robot,cfg,step_m=model_cfg['fine_step_m'],up_step_m=model_cfg['up_step_m'],
                yaw_step_rad=.15,motion_frame=model_cfg['motion_frame'],level_orientation=model_cfg['level_orientation'],
                z_floor_m=controllers[side].z_floor_m,settle_steps=model_cfg['robot']['settle_steps'],
                settle_dt_s=model_cfg['robot']['settle_dt_s'],
                smooth_plugin=SmoothPlugin(enabled=model_cfg['plugins']['smooth'],
                    substeps=model_cfg['smooth_substeps'],dt_s=model_cfg['smooth_dt_s']),verbose=False)
        backend.start()
        httpd=TeleopServer((args.host,args.port),handler(backend))
        print(f'GUMI READY http://{args.host}:{args.port}/ front={rig.front.state()["source"]}',flush=True)
        httpd.serve_forever()
    finally:
        if backend:backend.shutdown()
        rig.close()

if __name__=='__main__':main()
