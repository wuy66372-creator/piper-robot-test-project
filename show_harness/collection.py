"""Upstream-compatible, action-aligned local demonstration recording."""
import json
import time
from pathlib import Path

import numpy as np

from core.teleop.dual import DualRolloutRecorder
from core.record.images import prepare_view, save_png

VIEWS=('agentview','wrist_left','wrist_right')
TOKENS={'MV_FWD','MV_BACK','MV_LEFT','MV_RIGHT','MV_UP','MV_DOWN','GRASP','RELEASE','STILL'}


def write_json(path, data):
    path=Path(path)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)


class DemonstrationRecorder(DualRolloutRecorder):
    def __init__(self, root):
        super().__init__(root,verbose=False)
        self.session_meta={}

    def start(self):
        from gripper_context import ScopedCommands
        path=super().start()
        self.commands=ScopedCommands()
        self.session_meta.update(status='recording',success=False,execution_errors=[],
                                 contract='piper-physical-2cm',step_m=.02,up_step_m=.02,
                                 image_size=256,observation_before_action=True,hardware_synchronized=False)
        self.session_meta['gripper_command_scope']='episode'
        write_json(path/'metadata.json',self.session_meta)
        return path

    def add_step(self,tokens,kinds,obs,grippers_closed):
        if not self.active:return
        if not obs.get('front_camera_available'):
            raise RuntimeError('Recording requires fresh front and both wrist images')
        if any(t not in TOKENS for t in tokens.values()):
            raise RuntimeError('Rotation is excluded from the inference-aligned action vocabulary')
        saved=dict(obs)
        for view in VIEWS:
            image=np.asarray(obs[view])
            if image.ndim!=3 or image.shape[2]!=3 or image.dtype!=np.uint8:
                raise RuntimeError('Invalid recording camera image: '+view)
            saved[view]=prepare_view(image,square_size=256)
        super().add_step(tokens,kinds,saved,grippers_closed)
        self.steps[-1]['front_camera']=obs.get('front_camera',{})
        for side in ('left','right'):
            self.steps[-1][side]['gripper_width']=float(obs[side]['gripper_width'])
            self.steps[-1][side]['last_gripper_command']=self.commands.last[side]
        self.steps[-1]['execution_status']='pending'
        self._flush_actions()

    def _flush_actions(self):
        path=self.dir/'actions.jsonl'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(''.join(json.dumps(s,ensure_ascii=False)+'\n' for s in self.steps))
        temporary.replace(path)

    def finish_step(self,result,measurements):
        if not self.steps:return
        self.steps[-1].update(execution_status='succeeded' if result.get('ok') else 'failed',
                              execution_result=result,measured_translations_m=measurements)
        self.commands.completed({s:self.steps[-1][s]['token'] for s in ('left','right')},success=bool(result.get('ok')))
        if not result.get('ok'):
            self.session_meta['execution_errors'].append(result.get('message','Action interrupted'))
        self._flush_actions()

    def terminal(self,obs):
        if not obs.get('front_camera_available'):raise RuntimeError('Fresh terminal observation required')
        (self.dir/'terminal').mkdir(exist_ok=True)
        for view in VIEWS:
            save_png(self.dir/'terminal'/f'{view}.png',prepare_view(obs[view],square_size=256))
        self.session_meta['terminal_observation']=True
        self.session_meta['terminal_gripper_state']={s:self.commands.observe(s,obs[s]) for s in ('left','right')}

    def stop(self):
        if self.active and self.session_meta.get('status')=='recording':
            self.session_meta.update(status='interrupted',success=False)
        return super().stop()


class MeasuredController:
    def __init__(self,controller,side,measurements):
        self.controller,self.side,self.measurements=controller,side,measurements
    def __getattr__(self,key):return getattr(self.controller,key)
    def step(self,token):
        before=np.asarray(self.controller.robot.get_ee_pose())[:3]
        result=self.controller.step(token)
        after=np.asarray(self.controller.robot.get_ee_pose())[:3]
        distance=float(np.linalg.norm(after-before))
        self.measurements[self.side]=distance
        if token.startswith('MV_') and abs(distance-.02)>.003:
            raise RuntimeError(f'{self.side}: expected 20 mm, measured {distance*1000:.1f} mm; sample failed')
        return result


class ExplicitReleaseController:
    """Bypass only the pinned upstream's first, cached-state RELEASE filter.

    That filter reads gripper_closed once before recording or executing. All
    later reads (including the recorded pre-state) use the actual controller.
    No robot method, safety check, or gripper command is replaced.
    """
    def __init__(self,controller):
        self.controller=controller
        self._filter_pending=True
    def __getattr__(self,name):return getattr(self.controller,name)
    @property
    def gripper_closed(self):
        if self._filter_pending:
            self._filter_pending=False
            return True
        return self.controller.gripper_closed
    @gripper_closed.setter
    def gripper_closed(self,value):self.controller.gripper_closed=value


class RecordingMixin:
    def _dispatch_pair(self,pair):
        # RELEASE is an absolute OPEN request, not a toggle. A large object or
        # restart can make the width-derived closed hint false while still held.
        saved=self.controllers
        self.controllers={side:ExplicitReleaseController(c) if pair[side]=='RELEASE' else c
                          for side,c in saved.items()}
        try:return super()._step_pair(pair)
        finally:self.controllers=saved

    def _cmd_set_task(self,text):
        if self.recorder.active:raise RuntimeError('录制过程中不能更改任务')
        if not 1<=len(text.strip())<=1000:raise ValueError('请输入1–1000字的采集任务')
        return super()._cmd_set_task(text)

    def _cmd_start(self):
        if self.recorder.active:raise RuntimeError('已经在录制')
        if self.lease.current() or self.model_run.active or self.busy:
            raise RuntimeError('请先停止持续控制和模型运行')
        if not self.task_text.strip():raise ValueError('请先填写并保存采集任务')
        if not self.model_controllers:raise RuntimeError('20 mm控制器尚未就绪')
        self._capture()
        if not self.latest_obs.get('front_camera_available'):raise RuntimeError('主视角图像不可用')
        for side,robot in self.session.robots.items():
            with robot.mutex:
                f=robot._feedback();f.healthy(time.monotonic(),enabled=True)
                if f.frames[0x2A1][0][0]!=1:raise RuntimeError(side+': 采集前需切回CAN控制')
                f.validate_target(f.joints())
            controller=self.model_controllers[side]
            if abs(controller.step_m-.02)>1e-9:raise RuntimeError('采集步长必须为20 mm')
            controller.gripper_closed=self.controllers[side].gripper_closed
        self.recorder.session_meta={'task':self.task_text,'task_text':self.task_text,
                                    'teleop':'web-dual','camera':self.session.front.state(),
                                    'source':'human_teleop','success':False}
        return super()._cmd_start()

    def _finish_recording(self,success=False):
        if not self.recorder.active:raise RuntimeError('当前没有录制')
        if self.busy or self.lease.current():raise RuntimeError('请等待当前动作结束')
        if success and (not self.recorder.steps or self.recorder.session_meta['execution_errors']):
            raise RuntimeError('空轨迹或包含失败动作的轨迹不能标记成功')
        if success:self.recorder.terminal(self.session.get_observation())
        self.recorder.session_meta.update(status='saved',success=bool(success))
        count=len(self.recorder.steps)
        path=self.recorder.stop()
        self.rollouts_saved+=1
        self.last_message=f'已保存 {path.name}：{count}步，'+('成功示教' if success else '未成功/中断轨迹')
        return {'ok':True,'message':self.last_message,'episode':path.name,'steps':count}

    def _cmd_stop(self):return self._finish_recording(False)

    def _cmd_cancel(self):
        if self.recorder.active:return self._finish_recording(False)
        for controller in self.controllers.values():controller.sync_from_robot()
        return {'ok':True,'message':'已重新同步当前位置（不回位）'}

    def _step_pair(self,pair):
        if not self.recorder.active:return self._dispatch_pair(pair)
        if any(t not in TOKENS for t in pair.values()):raise ValueError('采集仅接受推理动作词表')
        self._capture()
        saved=self.controllers
        measurements={}
        start=len(self.recorder.steps)
        for side,controller in self.model_controllers.items():
            controller.gripper_closed=saved[side].gripper_closed
            controller.sync_from_robot()
        self.controllers={side:MeasuredController(c,side,measurements) for side,c in self.model_controllers.items()}
        for robot in self.session.robots.values():robot.motion_deadline=time.monotonic()+20.
        try:
            result=self._dispatch_pair(pair)
        except Exception as exc:
            result={'ok':False,'message':str(exc)}
            raise
        finally:
            for side,controller in self.model_controllers.items():
                saved[side].gripper_closed=controller.gripper_closed
            self.controllers=saved
            for robot in self.session.robots.values():robot.motion_deadline=float('inf')
            if len(self.recorder.steps)>start:
                self.recorder.finish_step(locals().get('result',{'ok':False,'message':'Interrupted'}),measurements)
        if not result.get('ok'):
            self.paused=True
            for robot in self.session.robots.values():robot.paused.set()
        return result
